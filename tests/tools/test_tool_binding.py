"""Tool-binding-per-state matrix + L-005/AC-... integration coverage
(CR-004 Task 12): `turn_logic._tools_for_state` binds a fixed,
state-specific tool set to the model. Asserts the matrix directly, then
cross-checks it end-to-end through `run_turn` for representative states,
proving the model actually receives that exact tool set. Also confirms the
reply reflects the tool's REAL return value (L-005) and that
`get_next_content`'s `TAP_LMS_TOKEN` auth requirement carries forward.

Retargeted from tap-buddy/tests/test_tool_binding.py against
app.core.turn_logic / app.tools.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest
import respx

from app.core.state_machine import SessionState, StudentIdentity, new_session, transition
from app.core.turn_logic import _tools_for_state, run_turn
from app.llm.types import ModelResponse, ToolCall
from app.tools.definitions import (
    GET_NEXT_CONTENT_TOOL,
    GET_QUIZ_QUESTION_DETAILS_TOOL,
    SAVE_SUBMISSION_TOOL,
)
from app.tools.lms_client import GET_NEXT_CONTENT_METHOD, TapLmsClient
from app.tools.lms_mock import MockTapLmsClient

EXPECTED_MATRIX: dict[SessionState, list[dict]] = {
    SessionState.IDENTITY_CONFIRMATION: [],
    # CR-007: LANGUAGE_CHOICE is handled entirely deterministically in
    # turn_processor.py (never reaches run_turn/_tools_for_state) — no
    # tools bound, same as every other pre-content-intro state.
    SessionState.LANGUAGE_CHOICE: [],
    SessionState.GREETING: [],
    SessionState.MODE_CHOICE: [],
    SessionState.CONTENT_INTRO: [GET_NEXT_CONTENT_TOOL],
    SessionState.ACTIVITY_IN_PROGRESS: [SAVE_SUBMISSION_TOOL],
    SessionState.DOUBT_CLEARING: [GET_QUIZ_QUESTION_DETAILS_TOOL],
    SessionState.REDIRECT: [SAVE_SUBMISSION_TOOL],
    SessionState.CLOSE: [],
    SessionState.ESCALATED: [],
}


@pytest.mark.parametrize("state,expected_tools", list(EXPECTED_MATRIX.items()))
def test_tools_for_state_matches_the_documented_matrix(state, expected_tools):
    assert _tools_for_state(state) == expected_tools


def test_every_session_state_is_covered_by_the_matrix():
    assert set(EXPECTED_MATRIX.keys()) == set(SessionState)


def test_each_lms_tool_is_bound_to_exactly_the_states_the_matrix_says():
    tool_to_states = {
        GET_NEXT_CONTENT_TOOL["name"]: set(),
        GET_QUIZ_QUESTION_DETAILS_TOOL["name"]: set(),
        SAVE_SUBMISSION_TOOL["name"]: set(),
    }
    for state, tools in EXPECTED_MATRIX.items():
        for tool in tools:
            tool_to_states[tool["name"]].add(state)

    assert tool_to_states[GET_NEXT_CONTENT_TOOL["name"]] == {SessionState.CONTENT_INTRO}
    assert tool_to_states[GET_QUIZ_QUESTION_DETAILS_TOOL["name"]] == {SessionState.DOUBT_CLEARING}
    assert tool_to_states[SAVE_SUBMISSION_TOOL["name"]] == {
        SessionState.ACTIVITY_IN_PROGRESS,
        SessionState.REDIRECT,
    }
    for state, tools in EXPECTED_MATRIX.items():
        assert len(tools) <= 1, f"{state} offers more than one tool: {tools}"


@dataclass
class RecordingModelRouter:
    scripted_result: ModelResponse
    last_tools: list[dict] | None = field(default=None, init=False)

    async def generate(self, route_name, messages, tools=None) -> ModelResponse:
        self.last_tools = tools or []
        return self.scripted_result


def _session_at(state: SessionState):
    session = new_session("contact-matrix", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(
        student_id="STU-001", child_name="Aarav", grade="5", tap_language="English"
    )
    path = {
        SessionState.GREETING: [],
        SessionState.CONTENT_INTRO: [SessionState.MODE_CHOICE, SessionState.CONTENT_INTRO],
        SessionState.ACTIVITY_IN_PROGRESS: [
            SessionState.MODE_CHOICE,
            SessionState.CONTENT_INTRO,
            SessionState.ACTIVITY_IN_PROGRESS,
        ],
    }[state]
    for target in path:
        transition(session, target)
    return session


@pytest.mark.asyncio
async def test_run_turn_offers_no_tools_to_the_model_at_greeting_end_to_end():
    session = _session_at(SessionState.GREETING)
    router = RecordingModelRouter(scripted_result=ModelResponse(text="Hi Aarav!"))
    lms = MockTapLmsClient()

    await run_turn(session=session, user_input="I'm okay", model_router=router, lms_client=lms)
    assert router.last_tools == []


@pytest.mark.asyncio
async def test_run_turn_offers_only_get_next_content_at_content_intro_end_to_end():
    session = _session_at(SessionState.CONTENT_INTRO)
    router = RecordingModelRouter(scripted_result=ModelResponse(text="Let's start!"))
    lms = MockTapLmsClient()

    await run_turn(session=session, user_input="yes", model_router=router, lms_client=lms)
    assert router.last_tools == [GET_NEXT_CONTENT_TOOL]


@pytest.mark.asyncio
async def test_run_turn_offers_only_save_submission_at_activity_in_progress_end_to_end():
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = RecordingModelRouter(scripted_result=ModelResponse(text="Great!"))
    lms = MockTapLmsClient()

    await run_turn(
        session=session, user_input="what happens next?", model_router=router, lms_client=lms
    )
    assert router.last_tools == [SAVE_SUBMISSION_TOOL]


# ---- L-005: reply reflects the tool's REAL return value, not an invented one ---


@pytest.mark.asyncio
async def test_get_next_content_reply_reflects_real_tool_return_value() -> None:
    """L-005: the model calls get_next_content and the resulting reply's
    session state carries the ACTUAL content returned by the (mocked) LMS
    client, not a value the model invented — MockTapLmsClient's
    DEFAULT_FIXTURE_CONTENT is a real, fixed, known value we assert against.
    """
    session = _session_at(SessionState.CONTENT_INTRO)

    class ToolCallingRouter:
        async def generate(self, route_name, messages, tools=None) -> ModelResponse:
            return ModelResponse(
                text="Let's start!",
                tool_calls=[ToolCall(name="get_next_content", arguments={"student_id": "STU-001"})],
            )

    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="yes", model_router=ToolCallingRouter(), lms_client=lms
    )

    assert outcome.session.content_reference is not None
    assert outcome.session.content_reference["content_id"] == "CONTENT-101"
    assert outcome.session.content_reference["title"] == "Creative problem-solving"
    assert outcome.session.content_reference["content_type"] == "video"


@pytest.mark.asyncio
async def test_save_submission_reply_reflects_real_tool_return_value() -> None:
    """L-005: save_submission's REAL recorded call (via the mock's
    `.submissions` list) proves the tool ran with the model's arguments,
    not that the model merely claimed it did.
    """
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)

    class ToolCallingRouter:
        async def generate(self, route_name, messages, tools=None) -> ModelResponse:
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

    lms = MockTapLmsClient()

    await run_turn(
        session=session, user_input="I finished!", model_router=ToolCallingRouter(), lms_client=lms
    )

    assert len(lms.submissions) == 1
    assert lms.submissions[0]["student_id"] == "STU-001"
    assert lms.submissions[0]["content_id"] == "CONTENT-101"
    assert lms.submissions[0]["submission"] == "done"


@dataclass
class SequencedRouter:
    """Two-step double for run_turn's "empty text -> follow-up round" path
    (turn_logic.py, "The model frequently returns ONLY the function call
    with empty text"): first call returns a tool call with no text, second
    call receives the REAL tool result folded into its prompt. Distinct from
    RecordingModelRouter (single-shot) — mirrors
    tests/core/test_turn_logic.py::SequencedFakeModelRouter, kept local so
    this module doesn't reach into another test module's internals.
    """

    scripted_results: list[ModelResponse]
    calls: list[list[dict[str, str]]] = field(default_factory=list)

    async def generate(self, route_name, messages, tools=None) -> ModelResponse:
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.scripted_results) - 1)
        return self.scripted_results[index]


@pytest.mark.asyncio
async def test_save_submission_invalid_content_id_surfaces_rejection_not_success() -> None:
    """F-030: `MockTapLmsClient.save_submission` now mirrors the live CR-005
    contract (unknown content_id -> success=False/status=invalid_assignment,
    no Submission recorded), matching `app/tools/lms_client.py`'s real
    parsing of that shape. This proves the REJECTION reaches the model (the
    follow-up round's prompt carries the real success=False/invalid_assignment
    result, not an invented success) and the session never advances as if
    the save succeeded — the turn-logic path surfaces the rejection to the
    student, it does not claim success.
    """
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = SequencedRouter(
        scripted_results=[
            ModelResponse(
                text="",
                tool_calls=[
                    ToolCall(
                        name="save_submission",
                        arguments={
                            "student_id": "STU-001",
                            "content_id": "CONTENT-999",  # never vended by the mock
                            "submission": "done",
                        },
                    )
                ],
            ),
            ModelResponse(
                text="Hmm, that didn't go through — let's check with your teacher."
            ),
        ]
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session, user_input="I finished!", model_router=router, lms_client=lms
    )

    # No Submission recorded for the rejected content id (CR-005's "no
    # Submission created" behavior for an invalid_assignment).
    assert lms.submissions == []
    # The follow-up round's prompt (what the model actually saw) carries the
    # REAL rejection, not a fabricated success.
    assert len(router.calls) == 2
    followup_prompt = router.calls[1][-1]["content"]
    assert "invalid_assignment" in followup_prompt
    assert "'success': False" in followup_prompt
    # The session must not advance as though the save succeeded.
    assert outcome.advanced_to is None
    assert outcome.session.state == SessionState.ACTIVITY_IN_PROGRESS
    assert outcome.reply_text == "Hmm, that didn't go through — let's check with your teacher."


# ---- TAP_LMS_TOKEN auth requirement for get_next_content, carried forward ---


@pytest.mark.asyncio
@respx.mock
async def test_get_next_content_sends_tap_lms_token_when_configured() -> None:
    """Live-verified fact (tap-buddy/CLAUDE.md, 2026-07-03): get_next_content
    is allow_guest=False on the live server and requires Frappe token auth.
    This test confirms the ported client still sends it when configured —
    live re-verification against THIS stack is Phase 7's job (L-009).
    """
    base_url = "https://lms.test.example"
    client = TapLmsClient(base_url, "test-api-key", frappe_token="real-frappe-token")
    route = respx.post(f"{base_url}/api/method/{GET_NEXT_CONTENT_METHOD}").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "content_id": "CONTENT-101",
                    "title": "Creative problem-solving",
                    "content_type": "video",
                }
            },
        )
    )

    await client.get_next_content("STU-001")

    assert route.calls.last.request.headers["authorization"] == "token real-frappe-token"
