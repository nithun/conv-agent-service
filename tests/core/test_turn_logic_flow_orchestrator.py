"""CR-014/ADR-015: `start_glific_flow` tool wiring inside `run_turn`/
`_tools_for_state` — the "decide" half (turn_logic.py must stay
Glific-import-free; the "execute" half is
tests/worker/test_turn_processor_flow_orchestrator.py).

Covers:
- AC-1: the tool is offered only when BOTH flow_orchestrator_enabled-style
  gating (a non-None `flow_registry`) AND the registry resolves to >=1
  entry are true — tested via `_tools_for_state` directly for the 3 cases
  (flag-equivalent None, empty registry, populated registry).
- AC-2: a valid `flow_name` sets `TurnOutcome.pending_flow_start` and never
  imports/calls anything Glific-shaped from this module.
- AC-3: an invalid `flow_name` is refused, logs a warning, and triggers
  exactly one corrective followup round.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.config.flow_registry import FlowRegistry, FlowRegistryEntry
from app.core.state_machine import SessionState, StudentIdentity, new_session, transition
from app.core.turn_logic import _tools_for_state, run_turn
from app.llm.types import ModelResponse, ToolCall
from app.tools.lms_mock import MockTapLmsClient

REGISTRY = FlowRegistry(
    entries={
        "TAP Buddy": FlowRegistryEntry(
            name="TAP Buddy", flow_id="25", when_to_use="voice handoff"
        ),
        "Registration Workflow": FlowRegistryEntry(
            name="Registration Workflow", flow_id="6", when_to_use="not a student"
        ),
    }
)
EMPTY_REGISTRY = FlowRegistry(entries={})


@dataclass
class SequencedFakeModelRouter:
    scripted_results: list[ModelResponse]
    calls: list[list[dict[str, str]]] = field(default_factory=list)
    calls_tools: list[list[dict[str, Any]] | None] = field(default_factory=list)

    async def generate(
        self,
        route_name: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        self.calls.append(messages)
        self.calls_tools.append(tools)
        index = min(len(self.calls) - 1, len(self.scripted_results) - 1)
        return self.scripted_results[index]


def _session_at(state: SessionState) -> Any:
    session = new_session("contact-x", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(
        student_id="STU-001", child_name="Aarav", grade="5", tap_language="English"
    )
    path = {
        SessionState.CONTENT_INTRO: [SessionState.MODE_CHOICE, SessionState.CONTENT_INTRO],
        SessionState.ACTIVITY_IN_PROGRESS: [
            SessionState.MODE_CHOICE,
            SessionState.CONTENT_INTRO,
            SessionState.ACTIVITY_IN_PROGRESS,
        ],
        SessionState.GREETING: [],
    }[state]
    for target in path:
        transition(session, target)
    return session


# ---- AC-1: _tools_for_state offering -----------------------------------


def test_tool_absent_when_flow_registry_is_none() -> None:
    """`None` stands in for `Settings.flow_orchestrator_enabled=False` at
    this layer (the caller decides whether to pass a registry at all)."""
    tools = _tools_for_state(SessionState.ACTIVITY_IN_PROGRESS, None)
    assert all(t["name"] != "start_glific_flow" for t in tools)


def test_tool_absent_when_registry_resolves_to_zero_entries() -> None:
    tools = _tools_for_state(SessionState.ACTIVITY_IN_PROGRESS, EMPTY_REGISTRY)
    assert all(t["name"] != "start_glific_flow" for t in tools)


def test_tool_present_with_current_registry_names_as_enum_when_registry_non_empty() -> None:
    tools = _tools_for_state(SessionState.ACTIVITY_IN_PROGRESS, REGISTRY)
    flow_tools = [t for t in tools if t["name"] == "start_glific_flow"]
    assert len(flow_tools) == 1
    assert set(flow_tools[0]["parameters"]["properties"]["flow_name"]["enum"]) == {
        "TAP Buddy",
        "Registration Workflow",
    }


def test_tool_offered_even_at_a_state_with_no_other_bound_tool() -> None:
    """"Every run_turn-reachable state" (CR-014) — GREETING binds zero LMS
    tools today but the orchestrator tool must still be offered there when
    the registry is active, since run_turn itself never special-cases which
    state this tool is valid in.
    """
    tools = _tools_for_state(SessionState.GREETING, REGISTRY)
    assert any(t["name"] == "start_glific_flow" for t in tools)


def test_existing_lms_tools_still_bound_alongside_the_new_tool() -> None:
    tools = _tools_for_state(SessionState.CONTENT_INTRO, REGISTRY)
    names = {t["name"] for t in tools}
    assert names == {"get_next_content", "start_glific_flow"}


# ---- AC-2: valid flow_name sets pending_flow_start, no Glific call -------


@pytest.mark.asyncio
async def test_valid_flow_name_sets_pending_flow_start() -> None:
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = SequencedFakeModelRouter(
        scripted_results=[
            ModelResponse(
                text="Sure, let's get you set up with TAP Buddy!",
                tool_calls=[
                    ToolCall(name="start_glific_flow", arguments={"flow_name": "TAP Buddy"})
                ],
            )
        ]
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session,
        user_input="I want to talk to buddy",
        model_router=router,
        lms_client=lms,
        flow_registry=REGISTRY,
    )

    assert outcome.pending_flow_start == "TAP Buddy"
    assert "start_glific_flow" in (outcome.tool_calls_made or [])
    assert len(router.calls) == 1  # no corrective followup needed on a hit


@pytest.mark.asyncio
async def test_valid_flow_name_does_not_advance_state() -> None:
    """Starting a flow is orthogonal to the LMS-driven state-advance
    policy (`_ADVANCE_ON_TOOL` has no `start_glific_flow` entry) — the
    session's conversational state is untouched by this tool."""
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = SequencedFakeModelRouter(
        scripted_results=[
            ModelResponse(
                text="One sec!",
                tool_calls=[
                    ToolCall(name="start_glific_flow", arguments={"flow_name": "TAP Buddy"})
                ],
            )
        ]
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session,
        user_input="voice please",
        model_router=router,
        lms_client=lms,
        flow_registry=REGISTRY,
    )

    assert outcome.advanced_to is None
    assert outcome.session.state == SessionState.ACTIVITY_IN_PROGRESS


# ---- Isolation: no Glific import/call from this module for ANY outcome ---


def test_turn_logic_module_has_no_glific_delivery_reference() -> None:
    """Static/import-level guard (belt-and-suspenders alongside
    tests/core/test_isolation.py's AST walk, which already lists
    turn_logic.py in CORE_MODULES): the new tool-handling code introduced
    by this CR must not add any `app.delivery`/Glific reference.
    """
    import app.core.turn_logic as turn_logic_module

    source = turn_logic_module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as f:
        text = f.read()
    assert "app.delivery" not in text
    assert "GlificClient" not in text
    assert "start_contact_flow" not in text


# ---- AC-3: unregistered flow_name is refused + exactly one corrective ----
# ---- followup round -------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_flow_name_is_refused_and_logs_a_warning(caplog) -> None:
    import logging

    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = SequencedFakeModelRouter(
        scripted_results=[
            ModelResponse(
                text="",
                tool_calls=[
                    ToolCall(
                        name="start_glific_flow", arguments={"flow_name": "Not A Real Flow"}
                    )
                ],
            ),
            ModelResponse(text="Sure, how can I help?"),
        ]
    )
    lms = MockTapLmsClient()

    with caplog.at_level(logging.WARNING, logger="app.core.turn_logic"):
        outcome = await run_turn(
            session=session,
            user_input="do something weird",
            model_router=router,
            lms_client=lms,
            flow_registry=REGISTRY,
        )

    assert outcome.pending_flow_start is None
    assert any("start_glific_flow" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_invalid_flow_name_triggers_exactly_one_corrective_followup_round() -> None:
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = SequencedFakeModelRouter(
        scripted_results=[
            ModelResponse(
                text="",
                tool_calls=[
                    ToolCall(
                        name="start_glific_flow", arguments={"flow_name": "Not A Real Flow"}
                    )
                ],
            ),
            ModelResponse(text="Got it, let's continue with today's activity."),
        ]
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session,
        user_input="do something weird",
        model_router=router,
        lms_client=lms,
        flow_registry=REGISTRY,
    )

    assert len(router.calls) == 2  # exactly one corrective round
    assert outcome.pending_flow_start is None
    assert outcome.reply_text == "Got it, let's continue with today's activity."
    # The corrective round tells the model the valid names.
    corrective_prompt = router.calls[1][-1]["content"]
    assert "TAP Buddy" in corrective_prompt
    assert "Registration Workflow" in corrective_prompt


@pytest.mark.asyncio
async def test_corrective_followup_round_can_still_resolve_to_a_valid_flow() -> None:
    """If the model corrects itself on the followup round (valid name this
    time), pending_flow_start is still set — the corrective round is a real
    retry opportunity, not just a scripted refusal."""
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = SequencedFakeModelRouter(
        scripted_results=[
            ModelResponse(
                text="",
                tool_calls=[
                    ToolCall(name="start_glific_flow", arguments={"flow_name": "TAP buddy"}),
                ],
            ),
            ModelResponse(
                text="On it!",
                tool_calls=[
                    ToolCall(name="start_glific_flow", arguments={"flow_name": "TAP Buddy"}),
                ],
            ),
        ]
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session,
        user_input="voice buddy please",
        model_router=router,
        lms_client=lms,
        flow_registry=REGISTRY,
    )

    assert len(router.calls) == 2
    assert outcome.pending_flow_start == "TAP Buddy"


@pytest.mark.asyncio
async def test_a_second_invalid_attempt_in_the_corrective_round_is_not_retried_again() -> None:
    """AC-3's "exactly one" cap is about the FLOW-NAME corrective round
    specifically — a still-invalid `flow_name` on that round must not
    trigger a SECOND corrective round. The corrective round's own text is
    non-empty here so the pre-existing, unrelated "empty text + tool
    attempted" recovery followup (further down in run_turn, covering every
    tool, not just start_glific_flow) does not also fire and confound the
    call count this test is pinning.
    """
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = SequencedFakeModelRouter(
        scripted_results=[
            ModelResponse(
                text="",
                tool_calls=[ToolCall(name="start_glific_flow", arguments={"flow_name": "X"})],
            ),
            ModelResponse(
                text="Let's just continue with today's activity.",
                tool_calls=[ToolCall(name="start_glific_flow", arguments={"flow_name": "Y"})],
            ),
        ]
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session,
        user_input="hmm",
        model_router=router,
        lms_client=lms,
        flow_registry=REGISTRY,
    )

    assert len(router.calls) == 2  # never a 3rd corrective round
    assert outcome.pending_flow_start is None
    assert outcome.reply_text == "Let's just continue with today's activity."


# ---- Model-invented flow name never reaches "resolved" without a hit -----


@pytest.mark.asyncio
async def test_flow_name_never_forwarded_when_flag_equivalent_registry_is_none() -> None:
    """Mirrors L-014's identifier-provenance discipline: even if the model
    somehow emits a start_glific_flow tool call (it should never see the
    tool at all when flow_registry is None, since the tool isn't offered —
    but this pins the defense-in-depth server-side behavior too)."""
    session = _session_at(SessionState.ACTIVITY_IN_PROGRESS)
    router = SequencedFakeModelRouter(
        scripted_results=[
            ModelResponse(
                text="",
                tool_calls=[
                    ToolCall(name="start_glific_flow", arguments={"flow_name": "TAP Buddy"})
                ],
            ),
            ModelResponse(text="Let's keep going."),
        ]
    )
    lms = MockTapLmsClient()

    outcome = await run_turn(
        session=session,
        user_input="voice buddy please",
        model_router=router,
        lms_client=lms,
        flow_registry=None,
    )

    assert outcome.pending_flow_start is None
