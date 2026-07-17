"""Worker turn-processing entrypoint tests (CR-004 Task 7): the ported
decision tree from tap-buddy's `_process_turn` — first-message name ask,
identity resolution (single/multi-match), readiness capture, mode choice,
and the run_turn handoff — re-platformed onto RedisSessionStore/ModelRouter.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import fakeredis.aioredis
import pytest
import respx
from httpx import Response

from app.config.language_map import all_entries
from app.config.settings import Settings
from app.core.state_machine import EscalationReason, SessionState, new_session
from app.ingress.glific_payload import GlificWebhookPayload
from app.llm.types import ModelResponse
from app.tools.lms_mock import MockTapLmsClient
from app.tools.speech_client import AsrResult, TtsResult
from app.worker.session_store import RedisSessionStore
from app.worker.turn_processor import (
    TurnProcessingResult,
    maybe_rearm_turn_flow,
    process_turn,
)


@dataclass
class FakeGlificSender:
    sent_texts: list[tuple[str, str]] = field(default_factory=list)
    created_tickets: list[tuple[str, str, str]] = field(default_factory=list)
    sent_audio: list[tuple[str, str]] = field(default_factory=list)

    async def send_text(self, receiver_id: str, body_text: str) -> dict[str, str]:
        self.sent_texts.append((receiver_id, body_text))
        return {"id": "fake-message-id"}

    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> dict[str, str]:
        self.created_tickets.append((contact_id, topic, body_text))
        return {"id": "fake-ticket-id"}

    async def send_audio(
        self,
        receiver_id: str,
        media_url: str,
        caption: str | None = None,
        sender_id: str | None = None,
    ) -> dict[str, str]:
        # Mirrors the real GlificClient.send_audio, which internally calls
        # register_media before creating the AUDIO message — recorded here
        # as one entry so tests can assert on the (receiver, media_url)
        # pair without needing a second fake method.
        self.sent_audio.append((receiver_id, media_url))
        return {"id": "fake-audio-message-id"}


@dataclass
class FakeModelRouter:
    scripted_text: str = "Great job!"

    async def generate(self, route_name, messages, tools=None) -> ModelResponse:
        return ModelResponse(text=self.scripted_text)


@pytest.fixture
def session_store():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = Settings(_env_file=None)
    return RedisSessionStore(redis, settings)


def _payload(**kwargs) -> GlificWebhookPayload:
    defaults = {
        "contact_id": "1",
        "phone": "917834811114",
        "message_id": "m1",
        "text": "hello",
    }
    defaults.update(kwargs)
    return GlificWebhookPayload(**defaults)


AUDIO_MEDIA_URL = "http://glific.test/media/note.ogg"


def _audio_payload(**kwargs) -> GlificWebhookPayload:
    defaults = {
        "contact_id": "1",
        "phone": "917834811114",
        "message_id": "m1",
        "message_type": "audio",
        "media_url": AUDIO_MEDIA_URL,
        "text": None,
    }
    defaults.update(kwargs)
    return GlificWebhookPayload(**defaults)


@dataclass
class ScriptedSpeechClient:
    """Fake `SpeechClient` (BR-004 Tasks 2-4): `transcribe()` returns the
    next scripted `AsrResult` on each call (so a test can script "low
    confidence, then high confidence" across consecutive turns);
    `synthesize()` either returns the scripted `TtsResult` or raises, to
    exercise Task 4's synthesis-failure fallback.
    """

    transcribe_results: list[AsrResult] = field(default_factory=list)
    synthesize_result: TtsResult | None = None
    synthesize_should_raise: bool = False
    transcribe_calls: list[tuple[bytes, object]] = field(default_factory=list)
    synthesize_calls: list[tuple[str, object]] = field(default_factory=list)
    _transcribe_index: int = 0

    def transcribe(self, audio_bytes: bytes, locale: object) -> AsrResult:
        self.transcribe_calls.append((audio_bytes, locale))
        result = self.transcribe_results[self._transcribe_index]
        self._transcribe_index += 1
        return result

    def synthesize(self, text: str, locale: object) -> TtsResult:
        self.synthesize_calls.append((text, locale))
        if self.synthesize_should_raise:
            raise RuntimeError("simulated TTS backend outage")
        assert self.synthesize_result is not None
        return self.synthesize_result


@pytest.mark.asyncio
async def test_first_message_from_new_contact_asks_for_name(session_store) -> None:
    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    payload = _payload(contact_id="new-1", phone="917834811114", text="hi")
    result = await process_turn(
        payload,
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.handled is True
    assert len(glific.sent_texts) == 1
    assert "name" in glific.sent_texts[0][1].lower()

    loaded = await session_store.load_session("new-1")
    assert loaded is not None
    assert loaded.state == SessionState.IDENTITY_CONFIRMATION


@pytest.mark.asyncio
async def test_single_match_identity_resolves_and_greets(session_store) -> None:
    # CR-007 AC-6 (self-retiring skip): the DEFAULT_FIXTURE_STUDENTS fixture
    # gives Aarav tap_language="Hindi" (a real, LMS-supplied language) — so
    # identity resolution should skip LANGUAGE_CHOICE entirely and go
    # straight to GREETING with the Hindi greeting_direct reply, exactly
    # like today's single-match path already does.
    from app.config.scripted_replies import get_scripted_reply

    glific = FakeGlificSender()
    lms = MockTapLmsClient()  # 917834811114 -> single candidate "Aarav" (Hindi)
    router = FakeModelRouter()

    # First turn: bootstraps identity_confirmation.
    await process_turn(
        _payload(contact_id="c1", phone="917834811114", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    # Second turn: the child's name reply triggers the LMS lookup.
    result = await process_turn(
        _payload(contact_id="c1", phone="917834811114", text="Aarav"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.session is not None
    assert result.session.identity.child_name == "Aarav"
    assert result.session.identity.tap_language == "Hindi"
    assert result.session.state == SessionState.GREETING
    expected_reply = get_scripted_reply("greeting_direct", "Hindi").format(child_name="Aarav")
    assert glific.sent_texts[-1][1] == expected_reply


@pytest.mark.asyncio
async def test_single_match_with_no_lms_language_enters_language_choice_then_resolves(
    session_store,
) -> None:
    # CR-007 AC-5 (the language-choice turn, today's live-real case: the
    # LMS returns no language at all). A dedicated fixture student with
    # tap_language=None (distinct from the DEFAULT_FIXTURE_STUDENTS' Aarav,
    # who already carries a real language) drives the full happy path:
    # ask-turn -> matched reply -> per-language greeting.
    from app.config.scripted_replies import get_scripted_reply
    from app.tools.lms_client import StudentCandidate

    glific = FakeGlificSender()
    lms = MockTapLmsClient(
        students={
            "911111111111": [
                StudentCandidate(
                    student_id="STU-100", child_name="Priya", grade="3", tap_language=None
                )
            ]
        }
    )
    router = FakeModelRouter()

    await process_turn(
        _payload(contact_id="lc1", phone="911111111111", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    result = await process_turn(
        _payload(contact_id="lc1", phone="911111111111", text="Priya"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.session is not None
    assert result.session.state == SessionState.LANGUAGE_CHOICE
    assert result.session.identity.tap_language is None
    from app.config.scripted_replies import language_choice_prompt

    assert glific.sent_texts[-1][1] == language_choice_prompt()

    # Child answers with a native-script/romanized language name.
    result2 = await process_turn(
        _payload(contact_id="lc1", phone="911111111111", text="हिंदी"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result2.session is not None
    assert result2.session.identity.tap_language == "Hindi"
    assert result2.session.state == SessionState.GREETING
    expected_reply = get_scripted_reply("greeting_direct", "Hindi").format(child_name="Priya")
    assert glific.sent_texts[-1][1] == expected_reply


@pytest.mark.asyncio
async def test_language_choice_reasks_once_then_defaults_to_english(session_store) -> None:
    from app.config.scripted_replies import get_scripted_reply, language_choice_reask
    from app.tools.lms_client import StudentCandidate

    glific = FakeGlificSender()
    lms = MockTapLmsClient(
        students={
            "922222222222": [
                StudentCandidate(
                    student_id="STU-101", child_name="Rahim", grade="3", tap_language=None
                )
            ]
        }
    )
    router = FakeModelRouter()

    await process_turn(
        _payload(contact_id="lc2", phone="922222222222", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    await process_turn(
        _payload(contact_id="lc2", phone="922222222222", text="Rahim"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    # First unmatched reply -> one scripted re-ask, stays in LANGUAGE_CHOICE.
    result1 = await process_turn(
        _payload(contact_id="lc2", phone="922222222222", text="banana"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result1.session is not None
    assert result1.session.state == SessionState.LANGUAGE_CHOICE
    assert result1.session.language_choice_attempts == 1
    assert glific.sent_texts[-1][1] == language_choice_reask()

    # Second unmatched reply -> defaults to English, proceeds to GREETING,
    # never stalls the conversation (AC-5).
    result2 = await process_turn(
        _payload(contact_id="lc2", phone="922222222222", text="still not a language"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result2.session is not None
    assert result2.session.identity.tap_language == "English"
    assert result2.session.state == SessionState.GREETING
    expected_reply = get_scripted_reply("greeting_direct", "English").format(child_name="Rahim")
    assert glific.sent_texts[-1][1] == expected_reply


@pytest.mark.asyncio
async def test_multi_candidate_confirm_with_language_sets_tap_language_and_skips_language_choice(
    session_store,
) -> None:
    # CR-007 AC-6/AC-7 through the REAL process_turn pipeline: Kavya's
    # fixture candidate carries tap_language="Tamil" — confirm_identity's
    # in-scope fix should populate session.identity.tap_language and the
    # self-retiring check should skip LANGUAGE_CHOICE.
    from app.config.scripted_replies import get_scripted_reply

    glific = FakeGlificSender()
    lms = MockTapLmsClient()  # 919999999999 -> Meera (English), Kavya (Tamil)
    router = FakeModelRouter()

    await process_turn(
        _payload(contact_id="mc1", phone="919999999999", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    await process_turn(
        _payload(contact_id="mc1", phone="919999999999", text="Meera"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    result = await process_turn(
        _payload(contact_id="mc1", phone="919999999999", text="Kavya"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.session is not None
    assert result.session.identity.tap_language == "Tamil"
    assert result.session.state == SessionState.GREETING
    expected_reply = get_scripted_reply("greeting_candidate_confirm", "Tamil").format(
        child_name="Kavya"
    )
    assert glific.sent_texts[-1][1] == expected_reply


@pytest.mark.asyncio
async def test_multi_match_identity_asks_for_confirmation(session_store) -> None:
    glific = FakeGlificSender()
    lms = MockTapLmsClient()  # 919999999999 -> Meera, Kavya
    router = FakeModelRouter()

    await process_turn(
        _payload(contact_id="c2", phone="919999999999", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    result = await process_turn(
        _payload(contact_id="c2", phone="919999999999", text="Meera"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.session is not None
    assert result.session.state == SessionState.IDENTITY_CONFIRMATION
    assert "Meera" in glific.sent_texts[-1][1]
    assert "Kavya" in glific.sent_texts[-1][1]

    # Third turn: child picks one of the candidates.
    result2 = await process_turn(
        _payload(contact_id="c2", phone="919999999999", text="Kavya"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result2.session is not None
    assert result2.session.state == SessionState.GREETING
    assert result2.session.identity.child_name == "Kavya"


@pytest.mark.asyncio
async def test_greeting_captures_readiness_and_advances_to_mode_choice(session_store) -> None:
    from app.core.state_machine import StudentIdentity, new_session

    session = new_session("c3", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    await session_store.save_session(session)

    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    result = await process_turn(
        _payload(contact_id="c3", phone="917834811114", text="excited"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.session is not None
    assert result.session.state == SessionState.MODE_CHOICE
    assert result.session.readiness_response == "excited"
    assert "voice" in glific.sent_texts[-1][1].lower()


@pytest.mark.asyncio
async def test_mode_choice_captures_voice_and_advances_to_content_intro(session_store) -> None:
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session("c4", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    transition(session, SessionState.MODE_CHOICE)
    await session_store.save_session(session)

    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter(scripted_text="Let's start today's video!")

    result = await process_turn(
        _payload(contact_id="c4", phone="917834811114", text="voice please"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.session is not None
    assert result.session.mode == "voice"
    # content_intro's deterministic prefetch runs inside run_turn and may
    # auto-advance to activity_in_progress once get_next_content succeeds.
    assert result.session.state in (SessionState.CONTENT_INTRO, SessionState.ACTIVITY_IN_PROGRESS)


@pytest.mark.asyncio
async def test_mode_choice_unrecognized_reply_reasks_without_advancing(session_store) -> None:
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session("c5", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    transition(session, SessionState.MODE_CHOICE)
    await session_store.save_session(session)

    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    result = await process_turn(
        _payload(contact_id="c5", phone="917834811114", text="banana"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.session is not None
    assert result.session.state == SessionState.MODE_CHOICE
    assert result.session.mode is None
    assert "typing" in glific.sent_texts[-1][1].lower()


@pytest.mark.asyncio
async def test_activity_in_progress_completion_calls_save_submission_via_run_turn(
    session_store,
) -> None:
    from app.core.state_machine import StudentIdentity, new_session, transition
    from app.llm.types import ToolCall

    session = new_session("c6", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    session.content_reference = {
        "content_id": "CONTENT-101",
        "title": "Creative problem-solving",
        "content_type": "video",
    }
    transition(session, SessionState.MODE_CHOICE)
    transition(session, SessionState.CONTENT_INTRO)
    transition(session, SessionState.ACTIVITY_IN_PROGRESS)
    await session_store.save_session(session)

    glific = FakeGlificSender()
    lms = MockTapLmsClient()

    class ToolCallingRouter:
        async def generate(self, route_name, messages, tools=None):
            return ModelResponse(
                text="Great work!",
                tool_calls=[
                    ToolCall(
                        name="save_submission",
                        arguments={
                            "student_id": "STU-001",
                            "content_id": "CONTENT-101",
                            "submission": "done",
                        },
                    )
                ],
            )

    result = await process_turn(
        _payload(contact_id="c6", phone="917834811114", text="I finished!"),
        session_store=session_store,
        lms_client=lms,
        model_router=ToolCallingRouter(),
        glific_client=glific,
    )

    assert result.session is not None
    assert result.session.state == SessionState.CLOSE
    assert len(lms.submissions) == 1
    assert result.reply_sent is True


# ---- maybe_rearm_turn_flow (F-019 cutover fix, 2026-07-16) ----------------
#
# The CR-004 port shipped GLIFIC_TURN_FLOW_ID and
# GlificClient.start_contact_flow with NO call site — without the re-arm,
# the contact's second message never reaches this service through a real
# Glific flow (found live when the F-019 cutover smoke stalled after turn
# 1). These tests pin the ported semantics from tap-buddy's
# `_maybe_rearm_turn_flow`.


@dataclass
class FakeFlowStarter:
    started: list[tuple[str, str]] = field(default_factory=list)
    raise_error: bool = False

    async def start_contact_flow(self, flow_id: str, contact_id: str) -> bool:
        if self.raise_error:
            raise RuntimeError("glific unreachable")
        self.started.append((flow_id, contact_id))
        return True


def _result_in_state(state: SessionState) -> TurnProcessingResult:
    session = new_session("c1", "917834811114", needs_identity_confirmation=True)
    session.state = state
    return TurnProcessingResult(handled=True, session=session, reply_sent=True)


@pytest.mark.asyncio
async def test_rearm_fires_after_a_normal_turn() -> None:
    starter = FakeFlowStarter()
    rearmed = await maybe_rearm_turn_flow(
        _result_in_state(SessionState.IDENTITY_CONFIRMATION),
        _payload(contact_id="c1"),
        turn_flow_id="26",
        glific_client=starter,
    )
    assert rearmed is True
    assert starter.started == [("26", "c1")]


@pytest.mark.asyncio
async def test_rearm_skipped_when_turn_flow_id_unset() -> None:
    starter = FakeFlowStarter()
    rearmed = await maybe_rearm_turn_flow(
        _result_in_state(SessionState.GREETING),
        _payload(contact_id="c1"),
        turn_flow_id="",
        glific_client=starter,
    )
    assert rearmed is False
    assert starter.started == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [SessionState.CLOSE, SessionState.ESCALATED])
async def test_rearm_skipped_after_terminal_states(state: SessionState) -> None:
    # Conversation over — the keyword flow is the way back in, never a
    # re-armed turn flow (ported tap-buddy semantics).
    starter = FakeFlowStarter()
    rearmed = await maybe_rearm_turn_flow(
        _result_in_state(state),
        _payload(contact_id="c1"),
        turn_flow_id="26",
        glific_client=starter,
    )
    assert rearmed is False
    assert starter.started == []


@pytest.mark.asyncio
async def test_rearm_failure_is_swallowed_not_raised() -> None:
    # The turn already succeeded (reply sent) — a re-arm failure must not
    # bubble into the consumer loop's retry/DLQ path.
    starter = FakeFlowStarter(raise_error=True)
    rearmed = await maybe_rearm_turn_flow(
        _result_in_state(SessionState.GREETING),
        _payload(contact_id="c1"),
        turn_flow_id="26",
        glific_client=starter,
    )
    assert rearmed is False


@pytest.mark.asyncio
async def test_rearm_skipped_for_an_ORGANICALLY_reached_close_state(session_store) -> None:
    # Code-review finding 2 (2026-07-16): the CLOSE skip must hold against a
    # result produced by the REAL process_turn/run_turn pipeline reaching
    # CLOSE via a successful save_submission — not only against a synthetic
    # _result_in_state() — the same organic-reachability standard whose
    # absence let the re-arm call-site gap itself ship (L-011).
    from app.core.state_machine import StudentIdentity, transition
    from app.llm.types import ToolCall

    session = new_session("c9", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    session.content_reference = {
        "content_id": "CONTENT-101",
        "title": "Creative problem-solving",
        "content_type": "video",
    }
    transition(session, SessionState.MODE_CHOICE)
    transition(session, SessionState.CONTENT_INTRO)
    transition(session, SessionState.ACTIVITY_IN_PROGRESS)
    await session_store.save_session(session)

    class ToolCallingRouter:
        async def generate(self, route_name, messages, tools=None):
            return ModelResponse(
                text="Great work!",
                tool_calls=[
                    ToolCall(
                        name="save_submission",
                        arguments={
                            "student_id": "STU-001",
                            "content_id": "CONTENT-101",
                            "submission": "done",
                        },
                    )
                ],
            )

    result = await process_turn(
        _payload(contact_id="c9", phone="917834811114", text="I finished!"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=ToolCallingRouter(),
        glific_client=FakeGlificSender(),
    )
    assert result.session is not None
    assert result.session.state == SessionState.CLOSE  # organically reached

    starter = FakeFlowStarter()
    rearmed = await maybe_rearm_turn_flow(
        result, _payload(contact_id="c9"), turn_flow_id="26", glific_client=starter
    )
    assert rearmed is False
    assert starter.started == []


# ---- BR-004 Task 2: worker-side transcription + ASR-failure handling -----
#
# Prior to this task, grep confirmed ZERO callers of
# `app.core.turn_logic.handle_low_confidence_asr` anywhere in `app/` — the
# counter could never reset and the clarification/fallback UX never fired.
# These tests drive the REAL `process_turn` entrypoint (not the unit under
# test in isolation, per L-008) with a mocked `SpeechClient`/`glific_client`.


@pytest.mark.asyncio
@respx.mock
async def test_audio_turn_transcript_flows_identically_to_text_through_the_decision_tree(
    session_store,
) -> None:
    respx.get(AUDIO_MEDIA_URL).mock(return_value=Response(200, content=b"fake-ogg-bytes"))
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session("a1", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    transition(session, SessionState.MODE_CHOICE)
    await session_store.save_session(session)

    glific = FakeGlificSender()
    speech = ScriptedSpeechClient(
        transcribe_results=[AsrResult(transcript="voice please", confidence=0.95)]
    )

    result = await process_turn(
        _audio_payload(contact_id="a1"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(),
        glific_client=glific,
        speech_client=speech,
    )

    # The transcript ("voice please") is used exactly like a text payload's
    # payload.text would be for the mode_choice branch — no separate
    # "audio" code path duplicated through the decision tree.
    assert result.session is not None
    assert result.session.mode == "voice"
    assert len(speech.transcribe_calls) == 1


@pytest.mark.asyncio
@respx.mock
async def test_first_low_confidence_audio_turn_sends_clarification_reply(session_store) -> None:
    respx.get(AUDIO_MEDIA_URL).mock(return_value=Response(200, content=b"mumble-bytes"))
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session("a2", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    transition(session, SessionState.MODE_CHOICE)
    session.mode = "voice"
    await session_store.save_session(session)

    glific = FakeGlificSender()
    speech = ScriptedSpeechClient(transcribe_results=[AsrResult(transcript="", confidence=0.1)])

    result = await process_turn(
        _audio_payload(contact_id="a2"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(),
        glific_client=glific,
        speech_client=speech,
    )

    assert result.handled is True
    assert result.reply_sent is True
    assert result.escalated is False
    assert len(glific.sent_texts) == 1
    assert "again" in glific.sent_texts[0][1].lower()
    assert glific.created_tickets == []

    loaded = await session_store.load_session("a2")
    assert loaded is not None
    assert loaded.asr_failure_count == 1
    assert loaded.state != SessionState.ESCALATED


@pytest.mark.asyncio
@respx.mock
async def test_second_consecutive_low_confidence_audio_turn_escalates_asr_failure(
    session_store,
) -> None:
    respx.get(AUDIO_MEDIA_URL).mock(return_value=Response(200, content=b"mumble-bytes"))
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session("a3", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    transition(session, SessionState.MODE_CHOICE)
    session.mode = "voice"
    session.asr_failure_count = 1  # already failed once, consecutively
    await session_store.save_session(session)

    glific = FakeGlificSender()
    speech = ScriptedSpeechClient(transcribe_results=[AsrResult(transcript="", confidence=0.1)])

    result = await process_turn(
        _audio_payload(contact_id="a3", message_id="m2"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(),
        glific_client=glific,
        speech_client=speech,
    )

    assert result.escalated is True
    assert result.escalation_reason == EscalationReason.ASR_FAILURE
    assert result.session is not None
    assert result.session.mode == "text"
    assert result.session.state == SessionState.ESCALATED
    assert len(glific.sent_texts) == 1
    assert "typing" in glific.sent_texts[0][1].lower()
    assert len(glific.created_tickets) == 1
    assert glific.created_tickets[0][1] == "ASR failure escalation"


@pytest.mark.asyncio
@respx.mock
async def test_high_confidence_transcription_resets_consecutive_asr_failure_count(
    session_store,
) -> None:
    respx.get(AUDIO_MEDIA_URL).mock(return_value=Response(200, content=b"clear-audio-bytes"))
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session("a4", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    transition(session, SessionState.MODE_CHOICE)
    session.mode = "voice"
    session.asr_failure_count = 1
    await session_store.save_session(session)

    glific = FakeGlificSender()
    speech = ScriptedSpeechClient(
        transcribe_results=[AsrResult(transcript="type please", confidence=0.9)]
    )

    await process_turn(
        _audio_payload(contact_id="a4"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(),
        glific_client=glific,
        speech_client=speech,
    )

    loaded = await session_store.load_session("a4")
    assert loaded is not None
    assert loaded.asr_failure_count == 0


@pytest.mark.asyncio
async def test_audio_turn_with_no_speech_client_degrades_gracefully(session_store) -> None:
    # Task 1's gate: credentials absent -> speech_client is None. Must never
    # crash the worker; the turn falls through as if it carried no
    # resolvable text (same as a low-confidence transcript).
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session("a5", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    transition(session, SessionState.MODE_CHOICE)
    await session_store.save_session(session)

    glific = FakeGlificSender()

    result = await process_turn(
        _audio_payload(contact_id="a5"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(),
        glific_client=glific,
        speech_client=None,
    )

    assert result.handled is True
    assert result.session is not None
    assert result.session.state == SessionState.MODE_CHOICE
    assert result.session.mode is None


# ---- BR-004 Task 3: worker-side distress check on audio transcripts ------
#
# Child-safety/BLOCKING tier. Reuses the SAME escalation surface as the
# ingress-level gate (`distress_gate._escalate_for_distress`) — verified by
# asserting on the mocked glific_client's send_text/create_ticket calls,
# not a new path.


@pytest.mark.asyncio
@respx.mock
async def test_distress_bearing_audio_transcript_escalates(session_store) -> None:
    respx.get(AUDIO_MEDIA_URL).mock(return_value=Response(200, content=b"distress-audio-bytes"))
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session("d1", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    transition(session, SessionState.MODE_CHOICE)
    await session_store.save_session(session)

    glific = FakeGlificSender()
    speech = ScriptedSpeechClient(
        transcribe_results=[AsrResult(transcript="I want to kill myself", confidence=0.95)]
    )

    result = await process_turn(
        _audio_payload(contact_id="d1"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(),
        glific_client=glific,
        speech_client=speech,
    )

    assert result.escalated is True
    assert result.escalation_reason == EscalationReason.DISTRESS
    assert result.session is not None
    assert result.session.state == SessionState.ESCALATED
    assert len(glific.sent_texts) == 1
    assert len(glific.created_tickets) == 1
    assert glific.created_tickets[0][1] == "Distress escalation"


@pytest.mark.asyncio
@respx.mock
async def test_distress_bearing_audio_from_brand_new_contact_with_no_session_still_escalates(
    session_store,
) -> None:
    # Mirrors tap-buddy's
    # test_distress_on_brand_new_contact_with_no_session_still_escalates
    # shape — the check runs BEFORE the `session is None` branch.
    respx.get(AUDIO_MEDIA_URL).mock(return_value=Response(200, content=b"distress-audio-bytes"))
    glific = FakeGlificSender()
    speech = ScriptedSpeechClient(
        transcribe_results=[AsrResult(transcript="my dad keeps hitting me", confidence=0.95)]
    )

    result = await process_turn(
        _audio_payload(contact_id="d2"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(),
        glific_client=glific,
        speech_client=speech,
    )

    assert result.escalated is True
    assert result.escalation_reason == EscalationReason.DISTRESS
    assert result.session is not None
    assert result.session.state == SessionState.ESCALATED
    assert len(glific.sent_texts) == 1
    assert len(glific.created_tickets) == 1


@pytest.mark.asyncio
async def test_text_payload_is_not_double_checked_by_the_new_worker_side_distress_gate(
    session_store,
) -> None:
    # A distress-bearing TEXT payload would never actually reach
    # process_turn in production (the ingress-level gate intercepts it
    # before enqueue) — this drives process_turn directly to prove Task 3's
    # NEW call site (gated on message_type == "audio") does not fire a
    # second time for text; session/reply behavior is exactly what it was
    # before this task (unaffected — GREETING returns before run_turn's own
    # backstop would ever run).
    from app.core.state_machine import StudentIdentity, new_session

    session = new_session("d3", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    await session_store.save_session(session)

    glific = FakeGlificSender()

    result = await process_turn(
        _payload(contact_id="d3", text="I want to kill myself"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(),
        glific_client=glific,
    )

    assert result.escalated is False
    assert glific.created_tickets == []


# ---- BR-004 Task 4: voice-mode TTS reply ----------------------------------
#
# Prior to this task, grep confirmed ZERO callers of
# `app.ingress.media.synthesize_and_store` and `GlificClient.send_audio`
# anywhere in `app/`.


def _voice_session_ready_for_run_turn(contact_id: str):
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session(contact_id, "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    session.mode = "voice"
    session.content_reference = {
        "content_id": "CONTENT-101",
        "title": "Creative problem-solving",
        "content_type": "video",
    }
    transition(session, SessionState.MODE_CHOICE)
    transition(session, SessionState.CONTENT_INTRO)
    transition(session, SessionState.ACTIVITY_IN_PROGRESS)
    return session


@pytest.mark.asyncio
async def test_voice_mode_reply_synthesizes_and_sends_audio(session_store, tmp_path) -> None:
    session = _voice_session_ready_for_run_turn("v1")
    await session_store.save_session(session)

    glific = FakeGlificSender()
    speech = ScriptedSpeechClient(synthesize_result=TtsResult(audio_bytes=b"fake-mp3"))
    settings = Settings(
        media_dir=str(tmp_path), public_base_url="http://localhost:8001", _env_file=None
    )

    result = await process_turn(
        _payload(contact_id="v1", text="I finished the video"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(scripted_text="Great job today!"),
        glific_client=glific,
        speech_client=speech,
        settings=settings,
    )

    assert result.reply_sent is True
    assert glific.sent_texts == []  # voice-mode reply went out as audio, not text
    assert len(glific.sent_audio) == 1
    assert glific.sent_audio[0][0] == "v1"
    assert glific.sent_audio[0][1].startswith("http://localhost:8001/media/")
    assert glific.sent_audio[0][1].endswith(".mp3")  # CR-007 GAP 3
    assert len(speech.synthesize_calls) == 1
    assert speech.synthesize_calls[0][0] == "Great job today!"


@pytest.mark.asyncio
async def test_voice_mode_with_no_speech_client_falls_back_to_text(session_store) -> None:
    session = _voice_session_ready_for_run_turn("v2")
    await session_store.save_session(session)

    glific = FakeGlificSender()

    result = await process_turn(
        _payload(contact_id="v2", text="I finished the video"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(scripted_text="Great job!"),
        glific_client=glific,
        speech_client=None,
        settings=Settings(_env_file=None),
    )

    assert result.reply_sent is True
    assert glific.sent_audio == []
    assert len(glific.sent_texts) == 1
    assert glific.sent_texts[0][1] == "Great job!"


@pytest.mark.asyncio
async def test_voice_mode_synthesis_failure_falls_back_to_text(session_store, tmp_path) -> None:
    session = _voice_session_ready_for_run_turn("v3")
    await session_store.save_session(session)

    glific = FakeGlificSender()
    speech = ScriptedSpeechClient(synthesize_should_raise=True)
    settings = Settings(
        media_dir=str(tmp_path), public_base_url="http://localhost:8001", _env_file=None
    )

    result = await process_turn(
        _payload(contact_id="v3", text="I finished the video"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(scripted_text="Great job!"),
        glific_client=glific,
        speech_client=speech,
        settings=settings,
    )

    assert result.reply_sent is True
    assert glific.sent_audio == []
    assert len(glific.sent_texts) == 1
    assert glific.sent_texts[0][1] == "Great job!"


@pytest.mark.asyncio
async def test_voice_mode_send_audio_failure_falls_back_to_text(session_store, tmp_path) -> None:
    session = _voice_session_ready_for_run_turn("v4")
    await session_store.save_session(session)

    @dataclass
    class FailingSendAudioGlificSender(FakeGlificSender):
        async def send_audio(  # type: ignore[override]
            self,
            receiver_id: str,
            media_url: str,
            caption: str | None = None,
            sender_id: str | None = None,
        ) -> dict[str, str]:
            raise RuntimeError("simulated Glific outage")

    glific = FailingSendAudioGlificSender()
    speech = ScriptedSpeechClient(synthesize_result=TtsResult(audio_bytes=b"fake-mp3"))
    settings = Settings(
        media_dir=str(tmp_path), public_base_url="http://localhost:8001", _env_file=None
    )

    result = await process_turn(
        _payload(contact_id="v4", text="I finished the video"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(scripted_text="Great job!"),
        glific_client=glific,
        speech_client=speech,
        settings=settings,
    )

    assert result.reply_sent is True
    assert len(glific.sent_texts) == 1
    assert glific.sent_texts[0][1] == "Great job!"


@pytest.mark.asyncio
async def test_text_mode_reply_unaffected_no_synthesis_attempted(session_store) -> None:
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session("v5", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    session.mode = "text"
    session.content_reference = {
        "content_id": "CONTENT-101",
        "title": "Creative problem-solving",
        "content_type": "video",
    }
    transition(session, SessionState.MODE_CHOICE)
    transition(session, SessionState.CONTENT_INTRO)
    transition(session, SessionState.ACTIVITY_IN_PROGRESS)
    await session_store.save_session(session)

    glific = FakeGlificSender()
    speech = ScriptedSpeechClient(synthesize_result=TtsResult(audio_bytes=b"fake-mp3"))

    result = await process_turn(
        _payload(contact_id="v5", text="I finished the video"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(scripted_text="Great job!"),
        glific_client=glific,
        speech_client=speech,
        settings=Settings(_env_file=None),
    )

    assert result.reply_sent is True
    assert len(glific.sent_texts) == 1
    assert glific.sent_audio == []
    assert speech.synthesize_calls == []


@pytest.mark.asyncio
async def test_voice_mode_public_base_url_unset_propagates_loudly(session_store) -> None:
    # Distinct failure class from synthesis/send failures (code-review
    # note, Task 4): a config bug must propagate loudly, never be silently
    # swallowed into a text fallback.
    session = _voice_session_ready_for_run_turn("v6")
    await session_store.save_session(session)

    glific = FakeGlificSender()
    speech = ScriptedSpeechClient(synthesize_result=TtsResult(audio_bytes=b"fake-mp3"))
    settings = Settings(public_base_url="", _env_file=None)

    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        await process_turn(
            _payload(contact_id="v6", text="I finished the video"),
            session_store=session_store,
            lms_client=MockTapLmsClient(),
            model_router=FakeModelRouter(scripted_text="Great job!"),
            glific_client=glific,
            speech_client=speech,
            settings=settings,
        )

    assert glific.sent_texts == []
    assert glific.sent_audio == []


# ---- CR-007 AC-4: per-language mode-choice matching through process_turn --
#
# Test input is DERIVED from scripted_replies.yaml's own per-language
# keyword lists (via get_mode_choice_keywords) rather than hand-composed
# native-script sentences — this proves the wiring (the RIGHT list is
# consulted for the session's language) without any transliteration/
# encoding risk in the test itself.


def _mode_choice_session(contact_id: str, tap_language: str | None):
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session(contact_id, "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(
        student_id="STU-001", child_name="Aarav", tap_language=tap_language
    )
    transition(session, SessionState.MODE_CHOICE)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("tap_language", list(all_entries().keys()))
async def test_mode_choice_matches_native_keywords_per_language(
    session_store, tap_language
) -> None:
    # Reads the RAW per-language block (not the English-unioned
    # get_mode_choice_keywords() result) so the input is guaranteed to be
    # the language's OWN keyword, not English's "voice"/"text" sneaking in
    # via the union at index 0 — that would make this test pass trivially
    # without ever exercising the per-language list.
    import app.config.scripted_replies as scripted_replies_module

    raw_block = scripted_replies_module._load()[tap_language]
    native_voice_keyword = raw_block["mode_voice_keywords"][0]
    native_text_keyword = raw_block["mode_text_keywords"][0]

    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    voice_session = _mode_choice_session(f"mv-{tap_language}", tap_language)
    await session_store.save_session(voice_session)
    result = await process_turn(
        _payload(contact_id=f"mv-{tap_language}", text=native_voice_keyword),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result.session is not None
    assert result.session.mode == "voice"

    text_session = _mode_choice_session(f"mt-{tap_language}", tap_language)
    await session_store.save_session(text_session)
    result2 = await process_turn(
        _payload(contact_id=f"mt-{tap_language}", text=native_text_keyword),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result2.session is not None
    assert result2.session.mode == "text"


@pytest.mark.asyncio
@pytest.mark.parametrize("tap_language", ["Hindi", "Tamil", "Punjabi", "Urdu", None])
async def test_mode_choice_english_keywords_always_work_regardless_of_session_language(
    session_store, tap_language
) -> None:
    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    session = _mode_choice_session(f"me-{tap_language}", tap_language)
    await session_store.save_session(session)
    result = await process_turn(
        _payload(contact_id=f"me-{tap_language}", text="voice"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result.session is not None
    assert result.session.mode == "voice"


# ---- L-011/P-005 static reachability check --------------------------------
#
# Mirrors tests/core/test_isolation.py's AST-walk idiom: parses
# turn_processor.py's own source and asserts every BR-004 call site is
# actually referenced in its call graph — supplements (does not replace)
# the functional/behavioral tests above, which are the stronger evidence
# (L-008: a unit test exercising a function directly does not prove the
# live pipeline reaches it).

TURN_PROCESSOR_PATH = Path(__file__).parent.parent.parent / "app" / "worker" / "turn_processor.py"


def _called_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_process_turn_module_call_graph_reaches_every_br_004_call_site() -> None:
    """Before this task, grep confirmed ZERO callers of
    `transcribe_inbound_media`, `handle_low_confidence_asr`,
    `synthesize_and_store`, and `GlificClient.send_audio` anywhere in
    `app/` — this is the exact gap BR-004 closes.
    """
    called = _called_names(TURN_PROCESSOR_PATH)
    for symbol in (
        "transcribe_inbound_media",
        "handle_low_confidence_asr",
        "detect_distress",
        "synthesize_and_store",
        "send_audio",
    ):
        assert symbol in called, f"{symbol} is not called anywhere in turn_processor.py"


def test_process_turn_module_call_graph_reaches_every_cr_007_call_site() -> None:
    """CR-007: the LANGUAGE_CHOICE branch and every per-language scripted
    reply call site are actually present in turn_processor.py's call
    graph — not just unit-tested in isolation (P-005/L-011).
    """
    called = _called_names(TURN_PROCESSOR_PATH)
    for symbol in (
        "get_scripted_reply",
        "get_mode_choice_keywords",
        "get_language_choice_keywords",
        "record_language_choice_attempt",
        "language_choice_prompt",
        "language_choice_reask",
    ):
        assert symbol in called, f"{symbol} is not called anywhere in turn_processor.py"


@pytest.mark.asyncio
@respx.mock
async def test_LOW_CONFIDENCE_distress_bearing_transcript_still_escalates(session_store) -> None:
    # THE live-caught case (2026-07-17, BR-004 Task 10 first run): the real
    # distress clip transcribed at confidence 0.49 — one hundredth under the
    # 0.5 bar — and the transcript was discarded before the distress gate
    # ever saw it ("say it again" instead of escalation). Safety-first
    # contract: distress runs on the RAW transcript even when
    # low-confidence; the ASR-failure clarification path only handles
    # non-distress low-confidence turns.
    respx.get(AUDIO_MEDIA_URL).mock(return_value=Response(200, content=b"real-ogg-bytes"))
    from app.core.state_machine import StudentIdentity, new_session

    session = new_session("a9", "917834811114", needs_identity_confirmation=True)
    session.identity = StudentIdentity(tap_language="English")
    await session_store.save_session(session)

    glific = FakeGlificSender()
    speech = ScriptedSpeechClient(
        transcribe_results=[
            AsrResult(transcript="someone at home keeps hitting me", confidence=0.49)
        ]
    )

    result = await process_turn(
        _audio_payload(contact_id="a9"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(),
        glific_client=glific,
        speech_client=speech,
    )

    assert result.escalated is True
    assert result.escalation_reason == EscalationReason.DISTRESS
    # It must be the DISTRESS surface, not the ASR clarification reply.
    assert result.session is not None
    assert result.session.asr_failure_count == 0  # not counted as an ASR failure
    assert glific.sent_texts, "comfort reply must be sent"
    assert "catch that" not in glific.sent_texts[-1][1]  # not the ASR clarification
