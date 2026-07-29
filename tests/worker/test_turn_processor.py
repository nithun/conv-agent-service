"""Worker turn-processing entrypoint tests (CR-004 Task 7): the ported
decision tree from tap-buddy's `_process_turn` — first-message name ask,
identity resolution (single/multi-match), readiness capture, mode choice,
and the run_turn handoff — re-platformed onto RedisSessionStore/ModelRouter.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import fakeredis.aioredis
import pytest
import respx
from httpx import Response

from app.config.language_map import all_entries
from app.config.settings import Settings
from app.core.state_machine import EscalationReason, SessionState, new_session
from app.ingress.glific_payload import GlificWebhookPayload
from app.llm.types import ModelResponse
from app.tools.lms_client import StudentCandidate
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
class FlakyGlificSender(FakeGlificSender):
    """`FakeGlificSender` that raises on `send_text` for the next
    `fail_remaining` calls — simulates the transient send failure the
    2026-07-17 code review's High finding reproduced (redelivery of a job
    whose reply-send raised mid-turn).
    """

    fail_remaining: int = 0

    async def send_text(self, receiver_id: str, body_text: str) -> dict[str, str]:
        if self.fail_remaining > 0:
            self.fail_remaining -= 1
            raise RuntimeError("simulated transient send_text failure")
        return await super().send_text(receiver_id, body_text)


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
        # Code review fix (2026-07-17): a unique default per call, not a
        # fixed "m1", mirroring real traffic (every distinct inbound Glific
        # message carries its own message_id — see app/ingress/glific_payload.py's
        # own uuid4 fallback) and the real behavior now that
        # language_choice_last_message_id makes the LANGUAGE_CHOICE branch
        # message_id-sensitive. A test simulating an actual retry/redelivery
        # passes an explicit `message_id=` to reuse the same one on purpose.
        "message_id": uuid4().hex,
        "text": "hello",
    }
    defaults.update(kwargs)
    return GlificWebhookPayload(**defaults)


AUDIO_MEDIA_URL = "http://glific.test/media/note.ogg"


def _audio_payload(**kwargs) -> GlificWebhookPayload:
    defaults = {
        "contact_id": "1",
        "phone": "917834811114",
        "message_id": uuid4().hex,
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
async def test_language_choice_reask_send_failure_then_redelivery_keeps_attempts_at_one(
    session_store,
) -> None:
    """Regression for the 2026-07-17 code review's High finding:
    reproduces the reviewer's fakeredis repro (a transient `send_text`
    failure on the LANGUAGE_CHOICE re-ask, followed by job redelivery of
    the SAME stream entry) and asserts `language_choice_attempts` stays at
    1 instead of double-counting — the promised one-time re-ask must
    survive a retry.
    """
    from app.config.scripted_replies import get_scripted_reply, language_choice_reask
    from app.tools.lms_client import StudentCandidate

    glific = FlakyGlificSender()
    lms = MockTapLmsClient(
        students={
            "933333333333": [
                StudentCandidate(
                    student_id="STU-102", child_name="Zara", grade="3", tap_language=None
                )
            ]
        }
    )
    router = FakeModelRouter()

    await process_turn(
        _payload(contact_id="lc3", phone="933333333333", text="hi", message_id="m1"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    await process_turn(
        _payload(contact_id="lc3", phone="933333333333", text="Zara", message_id="m2"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    # Arm the simulated transient failure only for the NEXT send_text call
    # (the language-choice re-ask below) — the setup turns above must
    # succeed normally.
    glific.fail_remaining = 1

    # First unmatched reply -> send_text fails (simulated transient error).
    # `process_turn` propagates the exception, mirroring
    # `app/worker/consumer_loop.py::process_one_entry`'s failure path (no
    # ack, no send-guard mark — the entry stays pending for redelivery).
    with pytest.raises(RuntimeError):
        await process_turn(
            _payload(contact_id="lc3", phone="933333333333", text="banana", message_id="m3"),
            session_store=session_store,
            lms_client=lms,
            model_router=router,
            glific_client=glific,
        )
    session_after_failure = await session_store.load_session("lc3")
    assert session_after_failure is not None
    assert session_after_failure.state == SessionState.LANGUAGE_CHOICE
    assert session_after_failure.language_choice_attempts == 1
    assert session_after_failure.language_choice_last_message_id == "m3"

    # Redelivery: the consumer group re-reads the SAME still-pending stream
    # entry (identical message_id "m3", identical text) after the
    # send_text failure above. This must NOT count as a second unmatched
    # reply — attempts stays at 1, session stays in LANGUAGE_CHOICE.
    result = await process_turn(
        _payload(contact_id="lc3", phone="933333333333", text="banana", message_id="m3"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result.session is not None
    assert result.session.language_choice_attempts == 1
    assert result.session.state == SessionState.LANGUAGE_CHOICE
    assert glific.sent_texts[-1][1] == language_choice_reask()

    # A genuinely NEW unmatched reply (a different message_id — the
    # child's actual next message) still counts normally and hits the cap,
    # defaulting to English exactly as today's happy-path test expects.
    result2 = await process_turn(
        _payload(
            contact_id="lc3", phone="933333333333", text="still not a language", message_id="m4"
        ),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result2.session is not None
    assert result2.session.identity.tap_language == "English"
    assert result2.session.state == SessionState.GREETING
    expected_reply = get_scripted_reply("greeting_direct", "English").format(child_name="Zara")
    assert glific.sent_texts[-1][1] == expected_reply


@pytest.mark.asyncio
async def test_language_choice_redelivery_of_same_message_id_does_not_consume_second_attempt(
    session_store,
) -> None:
    """Direct guard check (code review 2026-07-17 High finding), independent
    of how the redelivery happened: a LANGUAGE_CHOICE session that already
    recorded one attempt for message_id "m3" must not bump
    `language_choice_attempts` again on redelivery of that SAME message_id
    — only a genuinely different message_id may consume the next attempt.
    """
    from app.config.scripted_replies import get_scripted_reply, language_choice_reask
    from app.core.state_machine import StudentIdentity, new_session

    session = new_session("lc4", "944444444444", needs_identity_confirmation=False)
    session.state = SessionState.LANGUAGE_CHOICE
    session.identity = StudentIdentity(student_id="STU-103", child_name="Dev")
    session.language_choice_attempts = 1
    session.language_choice_last_message_id = "m3"
    await session_store.save_session(session)

    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    # Redelivery of the SAME message_id "m3" — attempts must stay at 1, not
    # bump to 2 and wrongly skip straight to the English default.
    result = await process_turn(
        _payload(contact_id="lc4", phone="944444444444", text="banana", message_id="m3"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result.session is not None
    assert result.session.language_choice_attempts == 1
    assert result.session.state == SessionState.LANGUAGE_CHOICE
    assert glific.sent_texts[-1][1] == language_choice_reask()

    # A genuinely NEW message_id ("m4") still counts normally and hits the
    # cap (MAX_LANGUAGE_CHOICE_ATTEMPTS=1), defaulting to English.
    result2 = await process_turn(
        _payload(
            contact_id="lc4",
            phone="944444444444",
            text="still not a language",
            message_id="m4",
        ),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result2.session is not None
    assert result2.session.identity.tap_language == "English"
    assert result2.session.state == SessionState.GREETING
    expected_reply = get_scripted_reply("greeting_direct", "English").format(child_name="Dev")
    assert glific.sent_texts[-1][1] == expected_reply


@pytest.mark.asyncio
async def test_single_match_language_choice_entry_uses_transition_and_bumps_turn_count(
    session_store,
) -> None:
    """Code review 2026-07-17 Medium finding: the single-match identity
    branch entering LANGUAGE_CHOICE now goes through transition() — like
    the sibling multi-candidate-confirm branch — instead of assigning
    session.state directly, which validates the edge and bumps turn_count.
    """
    from app.tools.lms_client import StudentCandidate

    glific = FakeGlificSender()
    lms = MockTapLmsClient(
        students={
            "955555555555": [
                StudentCandidate(
                    student_id="STU-104", child_name="Ishaan", grade="3", tap_language=None
                )
            ]
        }
    )
    router = FakeModelRouter()

    await process_turn(
        _payload(contact_id="lc5", phone="955555555555", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    result = await process_turn(
        _payload(contact_id="lc5", phone="955555555555", text="Ishaan"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.session is not None
    assert result.session.state == SessionState.LANGUAGE_CHOICE
    assert result.session.turn_count == 1


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


# ---- CR-011/ADR-013 Task 7: auto-provision on not-found identity ----------
#
# 2026-07-27 CONTRACT UPDATE (F-071 reply): `register_test_student` is a
# SINGLE call — creates Student+ProgramEnrollment in a server-hardcoded test
# batch and returns `student_id` directly. No two-step register-then-
# re-resolve.


@pytest.mark.asyncio
async def test_not_found_identity_auto_registers_and_reaches_language_choice(
    session_store,
) -> None:
    from app.config.scripted_replies import language_choice_prompt

    glific = FakeGlificSender()
    lms = MockTapLmsClient()  # phone below is not in DEFAULT_FIXTURE_STUDENTS
    router = FakeModelRouter()

    await process_turn(
        _payload(contact_id="reg-1", phone="955555555555", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    result = await process_turn(
        _payload(contact_id="reg-1", phone="955555555555", text="New Tester"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.session is not None
    # Never dead-ends in IDENTITY_CONFIRMATION for a genuinely new tester —
    # register_test_student never returns a tap_language, so the same
    # self-retiring GAP-0 check sends this contact to LANGUAGE_CHOICE.
    assert result.session.state == SessionState.LANGUAGE_CHOICE
    assert result.session.identity.child_name == "New Tester"
    assert result.session.identity.student_id  # a real id was assigned
    assert result.session.identity.tap_language is None
    assert glific.sent_texts[-1][1] == language_choice_prompt()

    assert len(lms.register_test_student_calls) == 1
    call = lms.register_test_student_calls[0]
    assert call["name1"] == "New Tester"
    assert call["phone"] == "955555555555"
    assert call["glific_id"] == "reg-1"


@pytest.mark.asyncio
async def test_not_found_identity_register_test_student_repeat_call_is_idempotent(
    session_store,
) -> None:
    """Same (phone, name1) resolving to the SAME student_id on a second,
    independent contact_id session — proves the mock (and, per the F-071
    reply, the live endpoint) genuinely treats registration as idempotent,
    not merely "doesn't crash on a repeat."
    """
    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    await process_turn(
        _payload(contact_id="reg-2a", phone="955555555556", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    first = await process_turn(
        _payload(contact_id="reg-2a", phone="955555555556", text="Repeat Tester"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    await process_turn(
        _payload(contact_id="reg-2b", phone="955555555556", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    second = await process_turn(
        _payload(contact_id="reg-2b", phone="955555555556", text="Repeat Tester"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert first.session is not None and second.session is not None
    assert first.session.identity.student_id == second.session.identity.student_id


@pytest.mark.asyncio
async def test_register_test_student_lms_api_error_falls_back_to_couldnt_find_reply(
    session_store,
) -> None:
    from app.tools.lms_client import LmsApiError

    class FailingRegisterLmsClient(MockTapLmsClient):
        async def register_test_student(self, name1, phone, *, glific_id=None, grade=None, gender=None):  # noqa: E501
            self.register_test_student_calls.append({"name1": name1, "phone": phone})
            raise LmsApiError("simulated register_test_student outage")

    glific = FakeGlificSender()
    lms = FailingRegisterLmsClient()
    router = FakeModelRouter()

    await process_turn(
        _payload(contact_id="reg-3", phone="955555555557", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    result = await process_turn(
        _payload(contact_id="reg-3", phone="955555555557", text="Doomed Tester"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.session is not None
    assert result.session.state == SessionState.IDENTITY_CONFIRMATION
    assert result.session.identity.student_id is None
    assert glific.sent_texts[-1][1] == (
        "I couldn't find that name for this phone number. Can you check the spelling?"
    )
    # Called exactly once this turn — no automatic retry (would create
    # ANOTHER duplicate-shaped record on the live endpoint's idempotency key).
    assert len(lms.register_test_student_calls) == 1


@pytest.mark.asyncio
async def test_register_test_student_success_false_falls_back_and_not_retried(
    session_store,
) -> None:
    from app.tools.lms_client import RegisterTestStudentResult

    class RejectingRegisterLmsClient(MockTapLmsClient):
        async def register_test_student(self, name1, phone, *, glific_id=None, grade=None, gender=None):  # noqa: E501
            self.register_test_student_calls.append({"name1": name1, "phone": phone})
            return RegisterTestStudentResult(success=False, student_id="")

    glific = FakeGlificSender()
    lms = RejectingRegisterLmsClient()
    router = FakeModelRouter()

    await process_turn(
        _payload(contact_id="reg-4", phone="955555555558", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    result = await process_turn(
        _payload(contact_id="reg-4", phone="955555555558", text="Rejected Tester"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.session is not None
    assert result.session.state == SessionState.IDENTITY_CONFIRMATION
    assert glific.sent_texts[-1][1] == (
        "I couldn't find that name for this phone number. Can you check the spelling?"
    )
    assert len(lms.register_test_student_calls) == 1


# ---- CR-011/ADR-013 Task 6: TESTAIRESET dispatch ---------------------------


@pytest.mark.asyncio
async def test_testairreset_wipes_session_and_calls_reset_with_test_registered_shaped_id(
    session_store,
) -> None:
    from app.config.scripted_replies import get_scripted_reply
    from app.core.state_machine import StudentIdentity, new_session

    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    session = new_session("reset-1", "9000000002", needs_identity_confirmation=False)
    session.identity = StudentIdentity(
        student_id="TEST-STU-042", child_name="Tester", tap_language="English"
    )
    await session_store.save_session(session)

    result = await process_turn(
        _payload(contact_id="reset-1", phone="9000000002", text="  testaireset  "),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.handled is True
    assert await session_store.load_session("reset-1") is None
    assert lms.reset_test_student_calls == [{"student_id": "TEST-STU-042", "push_to_glific": False}]
    assert glific.sent_texts[-1][1] == get_scripted_reply("reset_confirmation", "English")


@pytest.mark.asyncio
async def test_testairreset_fires_identically_for_a_real_shaped_student_id_no_guard(
    session_store,
) -> None:
    """L-017/Option B: a plausible REAL id (not an obviously-fake
    placeholder — same style as `ST00491459` used elsewhere in this repo's
    live-verified fixtures) gets reset with NO difference in behavior from
    the test-registered-shaped id above — proving there is no eligibility
    guard, rather than merely assuming one doesn't exist.
    """
    from app.config.scripted_replies import get_scripted_reply
    from app.core.state_machine import StudentIdentity, new_session

    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    session = new_session("reset-2", "9000000003", needs_identity_confirmation=False)
    session.identity = StudentIdentity(
        student_id="ST00491459", child_name="Real Kid", tap_language="Hindi"
    )
    await session_store.save_session(session)

    result = await process_turn(
        _payload(contact_id="reset-2", phone="9000000003", text="TestAiReset"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.handled is True
    assert await session_store.load_session("reset-2") is None
    assert lms.reset_test_student_calls == [{"student_id": "ST00491459", "push_to_glific": False}]
    assert glific.sent_texts[-1][1] == get_scripted_reply("reset_confirmation", "Hindi")


@pytest.mark.asyncio
async def test_testairreset_fires_regardless_of_current_conversation_state(session_store) -> None:
    from app.core.state_machine import StudentIdentity, new_session, transition

    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    session = new_session("reset-3", "9000000004", needs_identity_confirmation=False)
    session.identity = StudentIdentity(
        student_id="ST00499999", child_name="Tester", tap_language="English"
    )
    session.mode = "text"
    transition(session, SessionState.MODE_CHOICE)
    transition(session, SessionState.CONTENT_INTRO)
    transition(session, SessionState.ACTIVITY_IN_PROGRESS)
    await session_store.save_session(session)

    result = await process_turn(
        _payload(contact_id="reset-3", phone="9000000004", text="TESTAIRESET"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.handled is True
    assert lms.reset_test_student_calls == [{"student_id": "ST00499999", "push_to_glific": False}]
    assert await session_store.load_session("reset-3") is None


@pytest.mark.asyncio
async def test_testairreset_with_no_session_is_a_noop(session_store) -> None:
    from app.config.scripted_replies import get_scripted_reply

    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    result = await process_turn(
        _payload(contact_id="reset-none", phone="9000000005", text="TESTAIRESET"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.handled is True
    assert lms.reset_test_student_calls == []
    assert glific.sent_texts[-1][1] == get_scripted_reply("fallback", None)
    assert await session_store.load_session("reset-none") is None


@pytest.mark.asyncio
async def test_testairreset_with_session_but_no_student_id_is_a_noop(session_store) -> None:
    from app.config.scripted_replies import get_scripted_reply
    from app.core.state_machine import new_session

    glific = FakeGlificSender()
    lms = MockTapLmsClient()
    router = FakeModelRouter()

    session = new_session("reset-4", "9000000006", needs_identity_confirmation=True)
    await session_store.save_session(session)

    result = await process_turn(
        _payload(contact_id="reset-4", phone="9000000006", text="TESTAIRESET"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert result.handled is True
    assert lms.reset_test_student_calls == []
    assert glific.sent_texts[-1][1] == get_scripted_reply("fallback", None)
    # Distinct from the "session is None" no-op: the (unresolved) session
    # itself is untouched, not deleted, since there was nothing to reset.
    reloaded = await session_store.load_session("reset-4")
    assert reloaded is not None
    assert reloaded.identity.student_id is None


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
async def test_asr_cap_reached_send_failure_then_redelivery_opens_exactly_one_ticket(
    session_store,
) -> None:
    """F-039 regression: a transient `send_text` failure on the CAP-REACHED
    audio path (should_fallback=True — escalate() already ran and the
    ESCALATED session was already saved before the send raised), followed
    by job redelivery of the SAME stream entry (identical message_id),
    must open exactly ONE Glific Ticket for the ASR-failure escalation —
    not a second one. Mirrors
    `test_language_choice_reask_send_failure_then_redelivery_keeps_attempts_at_one`'s
    fakeredis repro shape.
    """
    respx.get(AUDIO_MEDIA_URL).mock(return_value=Response(200, content=b"mumble-bytes"))
    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session("a5", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    transition(session, SessionState.MODE_CHOICE)
    session.mode = "voice"
    session.asr_failure_count = 1  # already failed once, consecutively
    await session_store.save_session(session)

    glific = FlakyGlificSender()
    # Two low-confidence transcriptions: one for the initial (failing)
    # attempt, one for the redelivery of the SAME stream entry.
    speech = ScriptedSpeechClient(
        transcribe_results=[
            AsrResult(transcript="", confidence=0.1),
            AsrResult(transcript="", confidence=0.1),
        ]
    )
    payload = _audio_payload(contact_id="a5", message_id="m2")

    # Arm the simulated transient failure for the next send_text call only
    # (the CAP-REACHED asr_fallback reply below).
    glific.fail_remaining = 1

    with pytest.raises(RuntimeError):
        await process_turn(
            payload,
            session_store=session_store,
            lms_client=MockTapLmsClient(),
            model_router=FakeModelRouter(),
            glific_client=glific,
            speech_client=speech,
        )

    session_after_failure = await session_store.load_session("a5")
    assert session_after_failure is not None
    assert session_after_failure.state == SessionState.ESCALATED
    assert session_after_failure.mode == "text"
    assert session_after_failure.asr_escalation_ticket_message_id is None
    assert glific.created_tickets == []

    # Redelivery: the consumer group re-reads the SAME still-pending stream
    # entry (identical message_id "m2", identical audio payload).
    result = await process_turn(
        payload,
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FakeModelRouter(),
        glific_client=glific,
        speech_client=speech,
    )

    assert result.escalated is True
    assert result.escalation_reason == EscalationReason.ASR_FAILURE
    assert result.session is not None
    assert result.session.asr_escalation_ticket_message_id == "m2"
    assert len(glific.sent_texts) == 1
    assert "typing" in glific.sent_texts[0][1].lower()
    # Exactly ONE ticket, not two — the idempotency guard held.
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


def test_process_turn_module_call_graph_reaches_every_cr_011_call_site() -> None:
    """CR-011/ADR-013 tasks 6/7: the TESTAIRESET dispatch branch and the
    auto-provision-on-not-found branch are actually present in
    turn_processor.py's call graph — not just unit-tested in isolation
    (P-005/L-011).
    """
    called = _called_names(TURN_PROCESSOR_PATH)
    for symbol in (
        "get_reset_keyword",
        "register_test_student",
        "reset_test_student",
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


# ---- CR-013/ADR-014 J2: returning-by-phone wire-up -------------------------
#
# The `session is None` branch now tries a phone-only lookup (scoped
# server-side to test batch BT00000025) BEFORE ever asking for a name.


@pytest.mark.asyncio
async def test_returning_by_phone_single_match_skips_name_ask_and_greets(session_store) -> None:
    from app.config.scripted_replies import get_scripted_reply

    lms = MockTapLmsClient(
        test_batch_students={
            "919037771988": [
                StudentCandidate(
                    student_id="ST00491462",
                    child_name="Nithu_Swift",
                    grade="5",
                    tap_language="English",
                )
            ]
        }
    )
    glific = FakeGlificSender()
    router = FakeModelRouter()

    result = await process_turn(
        _payload(contact_id="ret-1", phone="919037771988", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    # Exactly ONE turn, ONE reply — the name-ask never happened.
    assert len(glific.sent_texts) == 1
    assert "what's your name" not in glific.sent_texts[0][1].lower()
    assert result.session is not None
    assert result.session.state == SessionState.GREETING
    assert result.session.identity.student_id == "ST00491462"
    assert result.session.identity.child_name == "Nithu_Swift"
    expected_reply = get_scripted_reply("greeting_direct", "English").format(
        child_name="Nithu_Swift"
    )
    assert glific.sent_texts[0][1] == expected_reply
    assert lms.test_student_by_phone_calls == ["919037771988"]
    # register_test_student must never be called for a recognized returning
    # tester (CR-013 decision 2 — no duplicate registration).
    assert lms.register_test_student_calls == []


@pytest.mark.asyncio
async def test_returning_by_phone_single_match_null_language_enters_language_choice(
    session_store,
) -> None:
    """CR-013 decision 4 (post-TESTAIRESET): a null `language` from the
    phone lookup must still fire the EXISTING GAP-0 re-prompt, collapsed
    onto this SAME first inbound message (never a second ask-name turn).
    """
    from app.config.scripted_replies import language_choice_prompt

    lms = MockTapLmsClient(
        test_batch_students={
            "919037771989": [
                StudentCandidate(
                    student_id="ST-RESET", child_name="Reset Tester", tap_language=None
                )
            ]
        }
    )
    glific = FakeGlificSender()
    router = FakeModelRouter()

    result = await process_turn(
        _payload(contact_id="ret-2", phone="919037771989", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert len(glific.sent_texts) == 1
    assert result.session is not None
    assert result.session.state == SessionState.LANGUAGE_CHOICE
    assert result.session.identity.student_id == "ST-RESET"
    assert glific.sent_texts[0][1] == language_choice_prompt()


@pytest.mark.asyncio
async def test_returning_by_phone_zero_match_falls_back_to_ask_name_unchanged(
    session_store,
) -> None:
    """AC (CR-013): zero phone-batch matches still receive today's
    ask-name reply, and the existing register_test_student zero-candidate
    path is completely unchanged (regression pin, byte-for-byte reply).
    """
    lms = MockTapLmsClient()  # default test_batch_students is empty
    glific = FakeGlificSender()
    router = FakeModelRouter()

    result = await process_turn(
        _payload(contact_id="ret-3", phone="9000000099999", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert lms.test_student_by_phone_calls == ["9000000099999"]
    assert result.session is not None
    assert result.session.state == SessionState.IDENTITY_CONFIRMATION
    assert glific.sent_texts[-1][1] == "Hi! Before we start, what's your name?"

    # The subsequent zero-candidate register_test_student auto-provision
    # path is untouched by this CR.
    result2 = await process_turn(
        _payload(contact_id="ret-3", phone="9000000099999", text="Brand New Tester"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result2.session is not None
    assert result2.session.identity.student_id is not None
    assert len(lms.register_test_student_calls) == 1


@pytest.mark.asyncio
async def test_returning_by_phone_multi_match_enters_disambiguation_seam(session_store) -> None:
    """>1 test-batch match reuses the EXISTING IDENTITY_CONFIRMATION +
    candidates disambiguation branch (confirm_identity) verbatim — no new
    disambiguation code path (P-005).
    """
    lms = MockTapLmsClient(
        test_batch_students={
            "919037771990": [
                StudentCandidate(
                    student_id="ST-NEW", child_name="Most Recent", tap_language="Hindi"
                ),
                StudentCandidate(
                    student_id="ST-OLD", child_name="Older Sibling", tap_language="English"
                ),
            ]
        }
    )
    glific = FakeGlificSender()
    router = FakeModelRouter()

    result = await process_turn(
        _payload(contact_id="ret-4", phone="919037771990", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result.session is not None
    assert result.session.state == SessionState.IDENTITY_CONFIRMATION
    assert len(result.session.identity.candidates) == 2
    assert "I found a few names" in glific.sent_texts[-1][1]

    # The child's next reply is handled by the EXISTING confirm_identity
    # branch (line ~547 of turn_processor.py) — zero new code.
    result2 = await process_turn(
        _payload(contact_id="ret-4", phone="919037771990", text="Older Sibling"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result2.session is not None
    assert result2.session.identity.student_id == "ST-OLD"
    assert result2.session.identity.tap_language == "English"
    assert result2.session.state == SessionState.GREETING


@pytest.mark.asyncio
async def test_returning_by_phone_lookup_failure_falls_back_to_ask_name(session_store) -> None:
    """L-005 failure honesty: a phone-lookup LmsApiError (timeout/5xx) must
    never crash the turn or silently claim the student doesn't exist — it
    falls back to today's ask-name path exactly like a genuine zero-match.
    """
    from app.tools.lms_client import LmsApiError

    class FailingPhoneLookupLmsClient(MockTapLmsClient):
        async def get_test_student_by_phone(self, phone):
            self.test_student_by_phone_calls.append(phone)
            raise LmsApiError("simulated get_test_student_by_phone outage")

    lms = FailingPhoneLookupLmsClient()
    glific = FakeGlificSender()
    router = FakeModelRouter()

    result = await process_turn(
        _payload(contact_id="ret-5", phone="919037771991", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )

    assert lms.test_student_by_phone_calls == ["919037771991"]
    assert result.handled is True
    assert result.session is not None
    assert result.session.state == SessionState.IDENTITY_CONFIRMATION
    assert glific.sent_texts[-1][1] == "Hi! Before we start, what's your name?"


@pytest.mark.asyncio
async def test_existing_session_does_not_retrigger_phone_lookup(session_store) -> None:
    """Regression: the phone-only lookup is a `session is None`-only
    entrypoint. An in-progress conversation (session already resolved,
    mid-identity-confirmation by name) must never call
    `get_test_student_by_phone` on subsequent turns.
    """
    lms = MockTapLmsClient()  # DEFAULT_FIXTURE_STUDENTS: 917834811114 -> Aarav
    glific = FakeGlificSender()
    router = FakeModelRouter()

    # Turn 1: session is None -> phone lookup runs once (zero test-batch
    # match by default), falls back to ask-name.
    await process_turn(
        _payload(contact_id="ret-6", phone="917834811114", text="hi"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert lms.test_student_by_phone_calls == ["917834811114"]

    # Turn 2: session now exists (IDENTITY_CONFIRMATION) -> resolves by
    # name via the UNCHANGED by-name path. The phone lookup must not run
    # again.
    result = await process_turn(
        _payload(contact_id="ret-6", phone="917834811114", text="Aarav"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert result.session is not None
    assert result.session.identity.child_name == "Aarav"
    assert lms.test_student_by_phone_calls == ["917834811114"]  # still just the one call

    # Turn 3: session is now past identity resolution entirely (GREETING)
    # -> the phone lookup must still not run.
    await process_turn(
        _payload(contact_id="ret-6", phone="917834811114", text="I'm ready"),
        session_store=session_store,
        lms_client=lms,
        model_router=router,
        glific_client=glific,
    )
    assert lms.test_student_by_phone_calls == ["917834811114"]
    assert "catch that" not in glific.sent_texts[-1][1]  # not the ASR clarification
