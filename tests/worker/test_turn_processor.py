"""Worker turn-processing entrypoint tests (CR-004 Task 7): the ported
decision tree from tap-buddy's `_process_turn` — first-message name ask,
identity resolution (single/multi-match), readiness capture, mode choice,
and the run_turn handoff — re-platformed onto RedisSessionStore/ModelRouter.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import fakeredis.aioredis
import pytest

from app.config.settings import Settings
from app.core.state_machine import SessionState
from app.ingress.glific_payload import GlificWebhookPayload
from app.llm.types import ModelResponse
from app.tools.lms_mock import MockTapLmsClient
from app.worker.session_store import RedisSessionStore
from app.worker.turn_processor import process_turn


@dataclass
class FakeGlificSender:
    sent_texts: list[tuple[str, str]] = field(default_factory=list)

    async def send_text(self, receiver_id: str, body_text: str) -> dict[str, str]:
        self.sent_texts.append((receiver_id, body_text))
        return {"id": "fake-message-id"}


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
    glific = FakeGlificSender()
    lms = MockTapLmsClient()  # 917834811114 -> single candidate "Aarav"
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
    assert result.session.state == SessionState.GREETING
    assert "feeling" in glific.sent_texts[-1][1].lower()


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
