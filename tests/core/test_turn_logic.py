"""Turn orchestration acceptance tests: correct tool called at correct
state, no correct_option leakage, off-turn handling, distress short-
circuit, and a proof that the system prompt is templated with a non-
English language.

Retargeted from tap-buddy/tests/test_turn_logic.py against
app.core.turn_logic (CR-004 Task 7) — the fake LLM double is now a fake
ModelRouter (matches ModelRouterLike.generate(route_name, messages, tools),
AC-1's "call sites resolve via ModelRouter, never a hardcoded model
string/direct client").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

import app.core.turn_logic as turn_logic_module
from app.core.state_machine import (
    EscalationReason,
    SessionState,
    StudentIdentity,
    new_session,
    transition,
)
from app.core.turn_logic import TAP_BUDDY_TURN_ROUTE, handle_off_turn, run_turn
from app.llm.types import ModelResponse, ToolCall
from app.tools.lms_client import SubmissionResult
from app.tools.lms_mock import MockTapLmsClient


@dataclass
class FakeModelRouter:
    """Records the last call's route/messages/tools, returns a scripted
    result — the ModelRouter-shaped test double this module needs (never a
    raw LLM client, per ADR-011/AC-1).
    """

    scripted_result: ModelResponse
    last_route_name: str | None = field(default=None, init=False)
    last_messages: list[dict[str, str]] | None = field(default=None, init=False)
    last_tools: list[dict[str, Any]] | None = field(default=None, init=False)

    async def generate(
        self,
        route_name: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        self.last_route_name = route_name
        self.last_messages = messages
        self.last_tools = tools or []
        return self.scripted_result

    @property
    def last_system_prompt(self) -> str | None:
        if not self.last_messages:
            return None
        system_messages = [m["content"] for m in self.last_messages if m["role"] == "system"]
        return system_messages[0] if system_messages else None


@dataclass
class SequencedFakeModelRouter:
    """Returns a different scripted result on each successive `generate()`
    call — needed to exercise the guardrail regenerate-once path, which
    calls the model a second time with a different (corrective) prompt.
    """

    scripted_results: list[ModelResponse]
    calls: list[list[dict[str, str]]] = field(default_factory=list)

    async def generate(
        self,
        route_name: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.scripted_results) - 1)
        return self.scripted_results[index]


def _session_at(state: SessionState, tap_language: str = "English"):
    session = new_session("contact-x", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(
        student_id="STU-001", child_name="Aarav", grade="5", tap_language=tap_language
    )
    path = {
        SessionState.GREETING: [],
        SessionState.MODE_CHOICE: [SessionState.MODE_CHOICE],
        SessionState.CONTENT_INTRO: [SessionState.MODE_CHOICE, SessionState.CONTENT_INTRO],
        SessionState.ACTIVITY_IN_PROGRESS: [
            SessionState.MODE_CHOICE,
            SessionState.CONTENT_INTRO,
            SessionState.ACTIVITY_IN_PROGRESS,
        ],
        SessionState.DOUBT_CLEARING: [
            SessionState.MODE_CHOICE,
            SessionState.CONTENT_INTRO,
            SessionState.ACTIVITY_IN_PROGRESS,
            SessionState.DOUBT_CLEARING,
        ],
    }[state]
    for target in path:
        transition(session, target)
    return session


@pytest.mark.asyncio
async def test_get_next_content_called_at_content_intro():
    session = _session_at(SessionState.CONTENT_INTRO)
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="",
            tool_calls=[ToolCall(name="get_next_content", arguments={"student_id": "STU-001"})],
        )
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(session=session, user_input="yes", model_router=router, lms_client=lms)
    assert "get_next_content" in outcome.tool_calls_made
    assert outcome.session.content_reference is not None
    assert router.last_route_name == TAP_BUDDY_TURN_ROUTE


@pytest.mark.asyncio
async def test_get_quiz_question_details_only_called_during_doubt_clearing():
    session = _session_at(SessionState.DOUBT_CLEARING)
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="Here's a hint.",
            tool_calls=[
                ToolCall(
                    name="get_quiz_question_details",
                    arguments={"question_id": "Q-201", "language": "English"},
                )
            ],
        )
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="I don't understand", model_router=router, lms_client=lms
    )
    assert "get_quiz_question_details" in outcome.tool_calls_made
    assert [t["name"] for t in router.last_tools] == ["get_quiz_question_details"]


@pytest.mark.asyncio
async def test_save_submission_only_called_on_completion_signal():
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = FakeModelRouter(
        scripted_result=ModelResponse(
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
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="I finished!", model_router=router, lms_client=lms
    )
    assert "save_submission" in outcome.tool_calls_made
    assert len(lms.submissions) == 1


@pytest.mark.asyncio
async def test_quiz_hint_reply_leakage_guard_uses_type_level_protection_only():
    """F-011: the orchestration layer never has a correct_option to inject
    into guardrail checks (removed, not wired) — the LMS client type itself
    structurally prevents leakage (covered in test_lms_client.py).
    """
    session = _session_at(SessionState.DOUBT_CLEARING)
    router = FakeModelRouter(scripted_result=ModelResponse(text="The answer is 3/4, easy!"))
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="what's the answer", model_router=router, lms_client=lms
    )
    quiz_details = await lms.get_quiz_question_details("Q-201", "English")
    assert not hasattr(quiz_details, "correct_option")
    assert outcome.reply_text


@pytest.mark.asyncio
async def test_off_turn_input_acknowledges_and_redirects_not_repeat():
    session = _session_at(SessionState.CONTENT_INTRO)
    turn_count_before = session.turn_count
    prior_instruction = "Shall we start today's lesson?"
    reply = handle_off_turn(session)
    assert reply != prior_instruction
    assert "today" in reply.lower() or "continue" in reply.lower()
    assert session.turn_count == turn_count_before


@pytest.mark.asyncio
async def test_off_turn_style_network_complaint_via_run_turn_does_not_crash():
    session = _session_at(SessionState.CONTENT_INTRO)

    class UnusedModelRouter:
        async def generate(self, *a, **kw):
            raise AssertionError("model must not be called for a deterministically off-turn input")

    lms = MockTapLmsClient()
    outcome = await run_turn(
        session=session,
        user_input="my network is bad",
        model_router=UnusedModelRouter(),
        lms_client=lms,
    )
    assert "network" not in outcome.reply_text.lower() or "wait" in outcome.reply_text.lower()
    assert outcome.session.state == SessionState.CONTENT_INTRO


@pytest.mark.asyncio
async def test_off_turn_signal_via_run_turn_short_circuits_before_model_call():
    session = _session_at(SessionState.CONTENT_INTRO)
    turn_count_before = session.turn_count

    class UnusedModelRouter:
        async def generate(self, *a, **kw):
            raise AssertionError("model must not be called for an off-turn-flagged turn")

    lms = MockTapLmsClient()
    outcome = await run_turn(
        session=session,
        user_input="wait a minute, who is this?",
        model_router=UnusedModelRouter(),
        lms_client=lms,
    )
    assert outcome.escalated is False
    assert outcome.session.turn_count == turn_count_before
    assert outcome.session.state == SessionState.CONTENT_INTRO
    assert "continue" in outcome.reply_text.lower() or "today" in outcome.reply_text.lower()


@pytest.mark.asyncio
async def test_is_off_turn_flag_short_circuits_regardless_of_text_content():
    session = _session_at(SessionState.CONTENT_INTRO)

    class UnusedModelRouter:
        async def generate(self, *a, **kw):
            raise AssertionError("model must not be called when is_off_turn=True")

    lms = MockTapLmsClient()
    outcome = await run_turn(
        session=session,
        user_input="something ordinary",
        model_router=UnusedModelRouter(),
        lms_client=lms,
        is_off_turn=True,
    )
    assert outcome.escalated is False
    assert outcome.session.state == SessionState.CONTENT_INTRO


@pytest.mark.asyncio
async def test_distress_flagged_turn_short_circuits_to_escalated():
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = FakeModelRouter(scripted_result=ModelResponse(text="should never be reached"))
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="my dad keeps hitting me", model_router=router, lms_client=lms
    )
    assert outcome.escalated is True
    assert outcome.session.state == SessionState.ESCALATED
    assert outcome.session.escalation_reason == EscalationReason.DISTRESS
    assert len(lms.submissions) == 0
    assert router.last_system_prompt is None  # model never even called


@pytest.mark.asyncio
async def test_system_prompt_templated_with_non_english_language():
    session = _session_at(SessionState.GREETING, tap_language="Hindi")
    router = FakeModelRouter(scripted_result=ModelResponse(text="Namaste!"))
    lms = MockTapLmsClient()

    await run_turn(
        session=session, user_input="main theek hoon", model_router=router, lms_client=lms
    )
    assert router.last_system_prompt is not None
    assert "Hindi" in router.last_system_prompt
    assert "Respond in Hindi" in router.last_system_prompt


@pytest.mark.asyncio
async def test_no_tools_offered_at_greeting_state():
    session = _session_at(SessionState.GREETING)
    router = FakeModelRouter(scripted_result=ModelResponse(text="Hi Aarav!"))
    lms = MockTapLmsClient()

    await run_turn(session=session, user_input="I'm okay", model_router=router, lms_client=lms)
    assert router.last_tools == []


@pytest.mark.asyncio
async def test_get_next_content_tool_call_auto_advances_to_activity_in_progress():
    session = _session_at(SessionState.CONTENT_INTRO)
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="Let's start!",
            tool_calls=[ToolCall(name="get_next_content", arguments={"student_id": "STU-001"})],
        )
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(session=session, user_input="yes", model_router=router, lms_client=lms)
    assert outcome.advanced_to == SessionState.ACTIVITY_IN_PROGRESS
    assert outcome.session.state == SessionState.ACTIVITY_IN_PROGRESS


@pytest.mark.asyncio
async def test_save_submission_tool_call_auto_advances_to_close():
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = FakeModelRouter(
        scripted_result=ModelResponse(
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
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="done!", model_router=router, lms_client=lms
    )
    assert outcome.advanced_to == SessionState.CLOSE
    assert outcome.session.state == SessionState.CLOSE


@pytest.mark.asyncio
async def test_no_tool_call_does_not_advance_state():
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = FakeModelRouter(scripted_result=ModelResponse(text="Tell me more?"))
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="what happens next?", model_router=router, lms_client=lms
    )
    assert outcome.advanced_to is None
    assert outcome.session.state == SessionState.ACTIVITY_IN_PROGRESS


@pytest.mark.asyncio
async def test_doubt_signal_at_activity_in_progress_transitions_to_doubt_clearing():
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="Problem-solving means finding a way to fix something. Want me to continue?"
        )
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session,
        user_input="I don't understand this at all",
        model_router=router,
        lms_client=lms,
    )
    assert outcome.session.state == SessionState.DOUBT_CLEARING
    assert outcome.escalated is False
    assert outcome.reply_text
    assert outcome.session.reexplanation_count == 0


@pytest.mark.asyncio
async def test_get_quiz_question_details_tool_call_does_not_advance_state():
    session = _session_at(SessionState.DOUBT_CLEARING)
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="Here's a hint.",
            tool_calls=[
                ToolCall(
                    name="get_quiz_question_details",
                    arguments={"question_id": "Q-201", "language": "English"},
                )
            ],
        )
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session,
        user_input="can you explain again please",
        model_router=router,
        lms_client=lms,
    )
    assert outcome.advanced_to is None
    assert outcome.session.state == SessionState.DOUBT_CLEARING
    assert outcome.session.reexplanation_count == 1


@pytest.mark.asyncio
async def test_doubt_clearing_repeated_doubt_signal_bumps_reexplanation_count():
    session = _session_at(SessionState.DOUBT_CLEARING)
    router = FakeModelRouter(scripted_result=ModelResponse(text="Let me explain differently."))
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="I still don't understand", model_router=router, lms_client=lms
    )
    assert outcome.session.state == SessionState.DOUBT_CLEARING
    assert outcome.session.reexplanation_count == 1
    assert outcome.escalated is False


@pytest.mark.asyncio
async def test_doubt_clearing_no_doubt_signal_resolves_back_to_activity_in_progress():
    session = _session_at(SessionState.DOUBT_CLEARING)
    router = FakeModelRouter(scripted_result=ModelResponse(text="Great, let's continue!"))
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="ok got it, thanks", model_router=router, lms_client=lms
    )
    assert outcome.session.state == SessionState.ACTIVITY_IN_PROGRESS
    assert outcome.escalated is False


@pytest.mark.asyncio
async def test_doubt_clearing_third_unresolved_doubt_escalates_without_model_call():
    session = _session_at(SessionState.DOUBT_CLEARING)
    session.reexplanation_count = 2

    class UnusedModelRouter:
        async def generate(self, *a, **kw):
            raise AssertionError("model must not be called on the escalating doubt turn")

    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session,
        user_input="I really still don't get it",
        model_router=UnusedModelRouter(),
        lms_client=lms,
    )
    assert outcome.escalated is True
    assert outcome.session.state == SessionState.ESCALATED
    assert outcome.session.escalation_reason == EscalationReason.UNRESOLVED_DOUBT


# ---- CR-007: scripted replies resolve per-tap_language at every call site -
#
# Each of the 5 former module-level string constants (FALLBACK_REPLY,
# DISTRESS_CLOSE_REPLY, CLARIFICATION_REPLY, ASR_FALLBACK_REPLY,
# UNRESOLVED_DOUBT_ESCALATION_REPLY) plus handle_off_turn now resolve via
# get_scripted_reply(key, session.identity.tap_language) — these tests pin
# a NON-English (Hindi) session at each existing call site and assert the
# Hindi text ships, not a silent English default.


@pytest.mark.asyncio
async def test_distress_short_circuit_replies_in_the_sessions_language():
    # detect_distress resolves patterns PER language (get_distress_patterns)
    # — a Hindi session is checked against the Hindi pattern list, not the
    # English one, so the distress-bearing input here must itself be Hindi
    # (mirrors distress_keywords.yaml's own Hindi abuse-category pattern).
    from app.config.scripted_replies import get_scripted_reply

    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS, tap_language="Hindi")
    router = FakeModelRouter(scripted_result=ModelResponse(text="should never be reached"))
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="mujhe maar rahe hain", model_router=router, lms_client=lms
    )
    assert outcome.reply_text == get_scripted_reply("distress_close", "Hindi")


@pytest.mark.asyncio
async def test_unresolved_doubt_escalation_replies_in_the_sessions_language():
    from app.config.scripted_replies import get_scripted_reply

    session = _session_at(SessionState.DOUBT_CLEARING, tap_language="Hindi")
    session.reexplanation_count = 2

    class UnusedModelRouter:
        async def generate(self, *a, **kw):
            raise AssertionError("model must not be called on the escalating doubt turn")

    lms = MockTapLmsClient()
    outcome = await run_turn(
        session=session,
        user_input="I really still don't get it",
        model_router=UnusedModelRouter(),
        lms_client=lms,
    )
    assert outcome.reply_text == get_scripted_reply("unresolved_doubt_escalation", "Hindi")


def test_handle_off_turn_replies_in_the_sessions_language():
    from app.config.scripted_replies import get_scripted_reply

    session = _session_at(SessionState.CONTENT_INTRO, tap_language="Hindi")
    assert handle_off_turn(session) == get_scripted_reply("off_turn_nudge", "Hindi")


def test_handle_low_confidence_asr_replies_in_the_sessions_language():
    from app.config.scripted_replies import get_scripted_reply
    from app.core.turn_logic import handle_low_confidence_asr

    session = _session_at(SessionState.MODE_CHOICE, tap_language="Hindi")
    reply, should_fallback = handle_low_confidence_asr(session)
    assert should_fallback is False
    assert reply == get_scripted_reply("clarification", "Hindi")

    reply2, should_fallback2 = handle_low_confidence_asr(session)
    assert should_fallback2 is True
    assert reply2 == get_scripted_reply("asr_fallback", "Hindi")


@pytest.mark.asyncio
async def test_content_prefetch_fallback_replies_in_the_sessions_language():
    from app.config.scripted_replies import get_scripted_reply
    from app.tools.lms_client import LmsApiError

    session = _session_at(SessionState.CONTENT_INTRO, tap_language="Hindi")
    router = FakeModelRouter(scripted_result=ModelResponse(text="unused"))

    class FailingLms(MockTapLmsClient):
        async def get_next_content(self, student_id):
            raise LmsApiError("simulated LMS outage")

    outcome = await run_turn(
        session=session, user_input="ready", model_router=router, lms_client=FailingLms()
    )
    assert outcome.reply_text == get_scripted_reply("fallback", "Hindi")


# ---- guardrail pass wiring (code-review fix, 2026-07-05, Finding 2) --------
#
# Confirms `run_turn` is actually wired to `app.guardrails.validators
# .run_all_validators` (the Guardrails AI validator classes, R-5/P-004),
# not `app.core.guardrails_checks.run_guardrails` (the bespoke aggregate
# that was previously the call site here despite having its own,
# independently-passing tests — L-008: a unit test exercising a function
# directly does not prove it's reached by the live pipeline).


@pytest.mark.asyncio
async def test_run_turn_calls_run_all_validators_not_the_bespoke_aggregate(monkeypatch) -> None:
    """Traces the call site itself (L-008): patches
    `app.core.turn_logic.run_all_validators` and asserts it is actually
    invoked by `run_turn` for a normal (non-tool, non-escalating) turn.
    """
    calls: list[str] = []
    real_run_all_validators = turn_logic_module.run_all_validators

    def _spy_run_all_validators(reply, *, allowed_content_fields=None):
        calls.append(reply)
        return real_run_all_validators(reply, allowed_content_fields=allowed_content_fields)

    monkeypatch.setattr(turn_logic_module, "run_all_validators", _spy_run_all_validators)

    session = _session_at(SessionState.GREETING)
    router = FakeModelRouter(scripted_result=ModelResponse(text="Hi Aarav!"))
    lms = MockTapLmsClient()

    await run_turn(session=session, user_input="I'm okay", model_router=router, lms_client=lms)

    assert calls == ["Hi Aarav!"]


@pytest.mark.asyncio
async def test_run_turn_regenerates_once_on_a_guardrail_violation_then_ships_the_rewrite() -> None:
    """A too-long first draft (fails `run_all_validators`' ReplyLength
    validator) triggers exactly one corrective regeneration; a compliant
    rewrite ships as the final reply — proves the real Guardrails AI
    validator pass drives this behavior end-to-end through run_turn, not
    just in validators.py's own isolated tests.
    """
    session = _session_at(SessionState.GREETING)
    too_long_reply = "This is one. This is two. This is three. This is four."
    compliant_rewrite = "Great, thanks for sharing!"
    router = SequencedFakeModelRouter(
        scripted_results=[
            ModelResponse(text=too_long_reply),
            ModelResponse(text=compliant_rewrite),
        ]
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="I'm okay", model_router=router, lms_client=lms
    )

    assert len(router.calls) == 2  # exactly one regeneration round
    assert outcome.reply_text == compliant_rewrite


@pytest.mark.asyncio
async def test_run_turn_falls_back_when_the_rewrite_still_violates() -> None:
    """If the corrective regeneration STILL violates a validator, run_turn
    ships the scripted "fallback" reply rather than a known-violating
    reply — never ship a violating reply, even after one regeneration
    attempt. CR-007: FALLBACK_REPLY is no longer a module constant — the
    English text now lives solely in scripted_replies.yaml, reached via
    get_scripted_reply("fallback", tap_language).
    """
    from app.config.scripted_replies import get_scripted_reply

    session = _session_at(SessionState.GREETING)
    still_too_long = "One. Two. Three. Four. Five sentences here now."
    router = SequencedFakeModelRouter(
        scripted_results=[
            ModelResponse(text="This is one. This is two. This is three. This is four."),
            ModelResponse(text=still_too_long),
        ]
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="I'm okay", model_router=router, lms_client=lms
    )

    assert len(router.calls) == 2
    assert outcome.reply_text == get_scripted_reply("fallback", session.identity.tap_language)


@pytest.mark.asyncio
async def test_run_turn_ships_reply_unmodified_when_it_passes_all_validators() -> None:
    """A clean, compliant reply is never regenerated — run_all_validators
    returning zero failures means exactly one model call for the turn.
    """
    session = _session_at(SessionState.GREETING)
    router = SequencedFakeModelRouter(scripted_results=[ModelResponse(text="Hi there!")])
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="I'm okay", model_router=router, lms_client=lms
    )

    assert len(router.calls) == 1
    assert outcome.reply_text == "Hi there!"


# -- state advance requires tool SUCCESS, not just a tool CALL ------------
# Live-caught 2026-07-08 (CR-004 360° round-2 live pass): a real
# save_submission business failure (success=False from the live LMS) still
# advanced the session to close — the child was told the save failed
# (L-005 held) while the session terminated anyway. AD-002's deterministic
# policy says "a SUCCESSFUL <tool> call advances"; these pin that.


class _FailingSubmissionLms(MockTapLmsClient):
    async def save_submission(self, student_id, content_id, submission):
        await super().save_submission(student_id, content_id, submission)
        return SubmissionResult(success=False, status="validation_error")


class _NoContentLms(MockTapLmsClient):
    async def get_next_content(self, student_id):
        return None


@pytest.mark.asyncio
async def test_failed_save_submission_does_not_advance_to_close():
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="Hmm, I couldn't save that yet.",
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
    )

    outcome = await run_turn(
        session=session,
        user_input="I'm done!",
        model_router=router,
        lms_client=_FailingSubmissionLms(),
    )

    assert outcome.advanced_to is None
    assert outcome.session.state == SessionState.ACTIVITY_IN_PROGRESS
    # The attempt is still recorded — only the ADVANCE is gated on success.
    assert "save_submission" in outcome.tool_calls_made


@pytest.mark.asyncio
async def test_get_next_content_returning_none_does_not_advance():
    session = _session_at(SessionState.CONTENT_INTRO)
    # Session must already carry a content_reference so the prefetch path
    # (which is separately success-gated) is skipped and the MODEL-driven
    # tool call is the one under test.
    session.content_reference = {"content_id": "C-0", "title": "t", "content_type": "video"}
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="Let me check what's next.",
            tool_calls=[ToolCall(name="get_next_content", arguments={"student_id": "STU-001"})],
        )
    )

    outcome = await run_turn(
        session=session, user_input="yes", model_router=router, lms_client=_NoContentLms()
    )

    assert outcome.advanced_to is None
    assert outcome.session.state == SessionState.CONTENT_INTRO


# ---- Identifier authority for LMS tool calls (F-026 root-cause fix) -------
#
# The real student_id/content_id are NOT in the system prompt, so a
# model-supplied value is by construction invented or empty — before this
# fix every live save_submission went out with bogus ids and was rejected
# by the LMS (the "business validation failure" F-026 originally blamed on
# seed data). The SESSION is the identifier authority; the model only owns
# `submission` (the child's answer).


@pytest.mark.asyncio
async def test_save_submission_uses_session_ids_not_model_supplied_ones():
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    # F-030: MockTapLmsClient.save_submission now validates content_id
    # against ids it has actually vended (mirrors the live CR-005 contract)
    # — use one of those (CONTENT-101, from DEFAULT_FIXTURE_CONTENT) so this
    # test keeps exercising identifier AUTHORITY, not content-id validity.
    session.content_reference = {
        "content_id": "CONTENT-101",
        "title": "Real lesson",
        "content_type": "VideoClass",
    }
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="Saving now!",
            tool_calls=[
                ToolCall(
                    name="save_submission",
                    arguments={
                        # The model invents ids it cannot know — must be ignored.
                        "student_id": "HALLUCINATED-ID",
                        "content_id": "made-up-content",
                        "submission": "my real answer",
                    },
                )
            ],
        )
    )
    lms = MockTapLmsClient()

    await run_turn(
        session=session, user_input="I finished!", model_router=router, lms_client=lms
    )

    assert lms.submissions == [
        {
            "student_id": "STU-001",  # from session.identity, not the model
            "content_id": "CONTENT-101",  # from session.content_reference
            "submission": "my real answer",  # the model's to provide
        }
    ]


@pytest.mark.asyncio
async def test_get_next_content_tool_call_uses_session_student_id():
    session = _session_at(SessionState.CONTENT_INTRO)
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="",
            tool_calls=[
                ToolCall(
                    name="get_next_content",
                    arguments={"student_id": "HALLUCINATED-ID"},
                )
            ],
        )
    )
    requested: list[str] = []

    class RecordingLms(MockTapLmsClient):
        async def get_next_content(self, student_id: str):
            requested.append(student_id)
            return await super().get_next_content(student_id)

    await run_turn(
        session=session, user_input="ready!", model_router=router, lms_client=RecordingLms()
    )

    # content_intro fires TWO get_next_content calls — the deterministic
    # prefetch and the model's tool call. The invariant: every one of them
    # uses the session's student_id; the model's invented id never reaches
    # the LMS.
    assert requested and all(sid == "STU-001" for sid in requested)
    assert "HALLUCINATED-ID" not in requested


@pytest.mark.asyncio
async def test_save_submission_fallback_to_model_ids_is_loud_not_silent(caplog):
    # Code-review H-1 (2026-07-16): if a state-machine regression ever
    # leaves the session without its authoritative ids, the fallback to the
    # model's (invented) arguments must WARN — a silent fallback reproduces
    # F-026's symptom with no signal pointing at the cause.
    import logging

    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    session.identity = StudentIdentity(student_id="", child_name="Aarav")  # regression sim
    session.content_reference = None
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="Saving!",
            tool_calls=[
                ToolCall(
                    name="save_submission",
                    arguments={
                        "student_id": "MODEL-GUESS",
                        # F-030: MockTapLmsClient now validates content_id
                        # against vended ids (live CR-005 contract) — use a
                        # real one (CONTENT-101) so this test keeps
                        # exercising the FALLBACK-IS-LOUD behavior, not
                        # content-id validity (covered separately by
                        # tests/tools/test_tool_binding.py's
                        # test_save_submission_invalid_content_id_surfaces_rejection_not_success).
                        "content_id": "CONTENT-101",
                        "submission": "answer",
                    },
                )
            ],
        )
    )
    lms = MockTapLmsClient()

    with caplog.at_level(logging.WARNING, logger="app.core.turn_logic"):
        await run_turn(
            session=session, user_input="done!", model_router=router, lms_client=lms
        )

    # Fallback used (model ids reached the mock)...
    assert lms.submissions == [
        {
            "student_id": "MODEL-GUESS",
            "content_id": "CONTENT-101",
            "submission": "answer",
        }
    ]
    # ...and it was LOUD: one warning per missing authoritative id.
    fallback_warnings = [
        r for r in caplog.records if "no authoritative" in r.getMessage()
    ]
    assert len(fallback_warnings) == 2


# ---- F-028: deterministic quiz-question sourcing via start_quiz -----------


def _quiz_session():
    session = _session_at(SessionState.DOUBT_CLEARING)
    session.content_reference = {
        "content_id": "Can a loan start your business? \U0001f4bc-Basic",
        "title": "",
        "content_type": "Quiz",
        "course_level": "Smart Savings-Financial Literacy-Level 2",
    }
    return session


@pytest.mark.asyncio
async def test_doubt_on_quiz_content_starts_quiz_once_and_stashes_question_id():
    session = _quiz_session()
    router = FakeModelRouter(scripted_result=ModelResponse(text="Here's a hint!"))
    lms = MockTapLmsClient()

    await run_turn(
        session=session, user_input="I don't get it", model_router=router, lms_client=lms
    )

    assert lms.quiz_starts == [
        {
            "student_id": "STU-001",  # session-sourced, never model-sourced
            "course_level": "Smart Savings-Financial Literacy-Level 2",
            "quiz_id": "Can a loan start your business? \U0001f4bc-Basic",
        }
    ]
    assert session.content_reference["active_question_id"] == "Q-201"
    assert session.content_reference["quiz_attempt_id"] == "ATTEMPT-001"

    # Second doubt turn: start_quiz must NOT be re-called (not a pure read —
    # it creates/resumes the server-side attempt).
    await run_turn(
        session=session, user_input="still confused", model_router=router, lms_client=lms
    )
    assert len(lms.quiz_starts) == 1


@pytest.mark.asyncio
async def test_quiz_tool_call_uses_stashed_question_id_not_model_supplied():
    session = _quiz_session()
    session.content_reference["active_question_id"] = "Q-201"  # already sourced
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="Hint coming.",
            tool_calls=[
                ToolCall(
                    name="get_quiz_question_details",
                    arguments={"question_id": "HALLUCINATED-QN", "language": "Klingon"},
                )
            ],
        )
    )
    requested: list[tuple[str, str]] = []

    class RecordingLms(MockTapLmsClient):
        async def get_quiz_question_details(self, question_id: str, language: str):
            requested.append((question_id, language))
            return await super().get_quiz_question_details(question_id, language)

    await run_turn(
        session=session, user_input="what's the answer", model_router=router,
        lms_client=RecordingLms(),
    )

    assert requested == [("Q-201", "English")]  # session id + session language won


@pytest.mark.asyncio
async def test_non_quiz_content_never_calls_start_quiz():
    session = _session_at(SessionState.DOUBT_CLEARING)
    session.content_reference = {
        "content_id": "CONTENT-101",
        "title": "Video lesson",
        "content_type": "VideoClass",
        "course_level": "CL-1",
    }
    lms = MockTapLmsClient()
    router = FakeModelRouter(scripted_result=ModelResponse(text="Let me explain again."))

    await run_turn(session=session, user_input="I'm confused", model_router=router, lms_client=lms)

    assert lms.quiz_starts == []


@pytest.mark.asyncio
async def test_quiz_tool_call_REFUSED_when_no_session_stashed_question_id():
    # Code-review H2 (2026-07-17): when start_quiz failed (or content isn't
    # a quiz) there is no stashed active_question_id — the tool call must be
    # REFUSED, never forwarded with the model's invented id (a question id
    # has no legitimate model-supplied case, unlike student_id's legacy
    # fallback).
    session = _session_at(SessionState.DOUBT_CLEARING)
    session.content_reference = {
        "content_id": "CONTENT-101",
        "title": "Video lesson",
        "content_type": "VideoClass",  # non-quiz: sourcing block never ran
        "course_level": "CL-1",
    }
    router = FakeModelRouter(
        scripted_result=ModelResponse(
            text="Let me check that question.",
            tool_calls=[
                ToolCall(
                    name="get_quiz_question_details",
                    arguments={"question_id": "HALLUCINATED-QN", "language": "English"},
                )
            ],
        )
    )
    reached_lms: list[str] = []

    class RecordingLms(MockTapLmsClient):
        async def get_quiz_question_details(self, question_id: str, language: str):
            reached_lms.append(question_id)
            return await super().get_quiz_question_details(question_id, language)

    outcome = await run_turn(
        session=session, user_input="what's the answer to question 5",
        model_router=router, lms_client=RecordingLms(),
    )

    assert reached_lms == []  # the invented id NEVER reached the LMS
    assert outcome.reply_text  # turn still replies (fails soft)
