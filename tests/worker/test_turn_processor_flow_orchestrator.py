"""CR-014/ADR-015: the worker-side "execute" half of the conv-AI flow
orchestrator — `process_turn`'s actual `start_contact_flow` dispatch
(AC-4) and `maybe_rearm_turn_flow`'s re-arm-suppression fix (AC-5, ADR-015
BC-2). Mirrors `tests/worker/test_turn_processor.py`'s fixtures/idioms.

Also carries the AC-7 reachability test (P-005 shape): an AST walk of
`turn_processor.py` proving the `glific_client.start_contact_flow` call for
`pending_flow_start` is actually present in the live call graph, not just
exercised against a mock in isolation.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import fakeredis.aioredis
import pytest

from app.config.flow_registry import FlowRegistry, FlowRegistryEntry
from app.config.settings import Settings
from app.core.state_machine import SessionState, StudentIdentity, new_session, transition
from app.ingress.glific_payload import GlificWebhookPayload
from app.llm.types import ModelResponse, ToolCall
from app.tools.lms_mock import MockTapLmsClient
from app.worker.session_store import RedisSessionStore
from app.worker.turn_processor import (
    TurnProcessingResult,
    maybe_rearm_turn_flow,
    process_turn,
)

REGISTRY = FlowRegistry(
    entries={
        "TAP Buddy": FlowRegistryEntry(name="TAP Buddy", flow_id="25", when_to_use="voice"),
    }
)


@dataclass
class FakeGlificSender:
    sent_texts: list[tuple[str, str]] = field(default_factory=list)
    started_flows: list[tuple[str, str]] = field(default_factory=list)
    raise_on_start_flow: bool = False
    call_order: list[str] = field(default_factory=list)

    async def send_text(self, receiver_id: str, body_text: str) -> dict[str, str]:
        self.call_order.append("send_text")
        self.sent_texts.append((receiver_id, body_text))
        return {"id": "fake-message-id"}

    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> dict[str, str]:
        return {"id": "fake-ticket-id"}

    async def send_audio(
        self,
        receiver_id: str,
        media_url: str,
        caption: str | None = None,
        sender_id: str | None = None,
    ) -> dict[str, str]:
        return {"id": "fake-audio-message-id"}

    async def start_contact_flow(self, flow_id: str, contact_id: str) -> bool:
        self.call_order.append("start_contact_flow")
        if self.raise_on_start_flow:
            raise RuntimeError("simulated glific outage")
        self.started_flows.append((flow_id, contact_id))
        return True


@pytest.fixture
def session_store():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = Settings(_env_file=None)
    return RedisSessionStore(redis, settings)


def _payload(**kwargs) -> GlificWebhookPayload:
    defaults = {
        "contact_id": "1",
        "phone": "917834811114",
        "message_id": uuid4().hex,
        "text": "hello",
    }
    defaults.update(kwargs)
    return GlificWebhookPayload(**defaults)


class FlowStartingRouter:
    async def generate(self, route_name, messages, tools=None) -> ModelResponse:
        return ModelResponse(
            text="Sure, let's get you set up with TAP Buddy!",
            tool_calls=[ToolCall(name="start_glific_flow", arguments={"flow_name": "TAP Buddy"})],
        )


async def _seeded_activity_in_progress_session(session_store, contact_id: str = "c-flow"):
    session = new_session(contact_id, "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    session.content_reference = {
        "content_id": "CONTENT-101",
        "title": "Video lesson",
        "content_type": "video",
    }
    transition(session, SessionState.MODE_CHOICE)
    transition(session, SessionState.CONTENT_INTRO)
    transition(session, SessionState.ACTIVITY_IN_PROGRESS)
    await session_store.save_session(session)
    return session


# ---- AC-4: process_turn dispatches start_contact_flow AFTER _deliver_reply -


@pytest.mark.asyncio
async def test_process_turn_dispatches_start_contact_flow_for_pending_flow_start(
    session_store,
) -> None:
    await _seeded_activity_in_progress_session(session_store, "c-flow")
    glific = FakeGlificSender()

    result = await process_turn(
        _payload(contact_id="c-flow", text="I want to talk to buddy"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FlowStartingRouter(),
        glific_client=glific,
        flow_registry=REGISTRY,
    )

    assert result.pending_flow_start == "TAP Buddy"
    assert glific.started_flows == [("25", "c-flow")]  # resolved flow_id, not the name
    # AC-4's ordering requirement: the reply is sent BEFORE the flow starts.
    assert glific.call_order == ["send_text", "start_contact_flow"]
    assert result.handled is True


@pytest.mark.asyncio
async def test_process_turn_does_not_dispatch_when_no_flow_registry_passed(session_store) -> None:
    """flow_registry=None (the default -- mirrors flag-off) means the tool
    was never offered by run_turn, so pending_flow_start is structurally
    None and no start_contact_flow call happens."""
    await _seeded_activity_in_progress_session(session_store, "c-noflag")
    glific = FakeGlificSender()

    result = await process_turn(
        _payload(contact_id="c-noflag", text="I want to talk to buddy"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FlowStartingRouter(),
        glific_client=glific,
        flow_registry=None,
    )

    assert result.pending_flow_start is None
    assert glific.started_flows == []


@pytest.mark.asyncio
async def test_process_turn_dispatch_failure_never_raises_and_still_returns_handled(
    session_store,
) -> None:
    """AC-4: a start_contact_flow failure (stale flow_id, Glific outage) is
    logged and does not crash the worker or mark the turn unhandled — a
    retry would re-run run_turn's LLM call and side effects a second time.
    """
    await _seeded_activity_in_progress_session(session_store, "c-fails")
    glific = FakeGlificSender(raise_on_start_flow=True)

    result = await process_turn(
        _payload(contact_id="c-fails", text="I want to talk to buddy"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FlowStartingRouter(),
        glific_client=glific,
        flow_registry=REGISTRY,
    )

    assert result.handled is True
    assert result.pending_flow_start == "TAP Buddy"  # still recorded on the result
    assert glific.sent_texts  # the reply itself still went out


@pytest.mark.asyncio
async def test_process_turn_empty_registry_never_offers_the_tool_at_all(session_store) -> None:
    """An empty flow_registry means `_tools_for_state` never even offers
    `start_glific_flow`, so pending_flow_start stays structurally None
    end-to-end (AC-1's structural kill switch #2, exercised through the
    real process_turn/run_turn pipeline, not just _tools_for_state
    directly)."""
    await _seeded_activity_in_progress_session(session_store, "c-empty-registry")
    glific = FakeGlificSender()

    result = await process_turn(
        _payload(contact_id="c-empty-registry", text="I want to talk to buddy"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FlowStartingRouter(),
        glific_client=glific,
        flow_registry=FlowRegistry(entries={}),
    )

    assert result.pending_flow_start is None
    assert glific.started_flows == []


@pytest.mark.asyncio
async def test_dispatch_helper_logs_and_no_ops_on_a_registry_reload_race(caplog) -> None:
    """Defense-in-depth unit test, direct against
    `_dispatch_pending_flow_start`: if `pending_flow_start` somehow names an
    entry no longer present in the `flow_registry` given to it (a
    theoretical reload race between `turn_logic.py`'s decision and this
    dispatch), it must not KeyError/crash — it logs and no-ops, never
    calling Glific."""
    import logging

    from app.worker.turn_processor import _dispatch_pending_flow_start

    glific = FakeGlificSender()

    with caplog.at_level(logging.WARNING, logger="app.worker.turn_processor"):
        await _dispatch_pending_flow_start(
            "c-race",
            "TAP Buddy",
            flow_registry=FlowRegistry(entries={}),  # TAP Buddy no longer resolves
            glific_client=glific,
        )

    assert glific.started_flows == []
    assert any("not found in the flow registry" in r.getMessage() for r in caplog.records)


# ---- AC-5 / ADR-015 BC-2: maybe_rearm_turn_flow suppression --------------


def _result_with_pending_flow_start(
    state: SessionState = SessionState.ACTIVITY_IN_PROGRESS,
) -> TurnProcessingResult:
    session = new_session("c1", "917834811114", needs_identity_confirmation=True)
    session.state = state
    return TurnProcessingResult(
        handled=True, session=session, reply_sent=True, pending_flow_start="TAP Buddy"
    )


@dataclass
class FakeFlowStarter:
    started: list[tuple[str, str]] = field(default_factory=list)

    async def start_contact_flow(self, flow_id: str, contact_id: str) -> bool:
        self.started.append((flow_id, contact_id))
        return True


@pytest.mark.asyncio
async def test_rearm_suppressed_when_pending_flow_start_is_set() -> None:
    starter = FakeFlowStarter()

    rearmed = await maybe_rearm_turn_flow(
        _result_with_pending_flow_start(),
        _payload(contact_id="c1"),
        turn_flow_id="80",
        glific_client=starter,
    )

    assert rearmed is False
    assert starter.started == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        SessionState.ACTIVITY_IN_PROGRESS,
        SessionState.CONTENT_INTRO,
        SessionState.GREETING,
        SessionState.CLOSE,
        SessionState.ESCALATED,
    ],
)
async def test_rearm_suppression_holds_regardless_of_session_state(state: SessionState) -> None:
    """The pending_flow_start guard takes precedence the same way the
    CLOSE/ESCALATED guard does — checked for EVERY state, not only
    non-terminal ones (ADR-015 BC-2's own wording: "regardless of
    session.state")."""
    starter = FakeFlowStarter()

    rearmed = await maybe_rearm_turn_flow(
        _result_with_pending_flow_start(state),
        _payload(contact_id="c1"),
        turn_flow_id="80",
        glific_client=starter,
    )

    assert rearmed is False
    assert starter.started == []


@pytest.mark.asyncio
async def test_rearm_still_fires_normally_when_pending_flow_start_is_none() -> None:
    """Regression guard: the new suppression must not swallow the
    pre-existing F-019 re-arm behavior for an ordinary turn."""
    starter = FakeFlowStarter()
    session = new_session("c1", "917834811114", needs_identity_confirmation=True)
    result = TurnProcessingResult(
        handled=True, session=session, reply_sent=True, pending_flow_start=None
    )

    rearmed = await maybe_rearm_turn_flow(
        result, _payload(contact_id="c1"), turn_flow_id="80", glific_client=starter
    )

    assert rearmed is True
    assert starter.started == [("80", "c1")]


@pytest.mark.asyncio
async def test_rearm_suppressed_for_an_ORGANICALLY_reached_flow_start(session_store) -> None:
    """The organic-reachability standard test_rearm_skipped_for_an_
    ORGANICALLY_reached_close_state (test_turn_processor.py) established
    for CLOSE, applied here: drives the REAL process_turn -> run_turn
    pipeline to a genuine start_glific_flow resolution, then confirms
    maybe_rearm_turn_flow suppresses against that REAL TurnProcessingResult
    — not only a synthetic one."""
    await _seeded_activity_in_progress_session(session_store, "c-organic")
    glific = FakeGlificSender()

    result = await process_turn(
        _payload(contact_id="c-organic", text="I want to talk to buddy"),
        session_store=session_store,
        lms_client=MockTapLmsClient(),
        model_router=FlowStartingRouter(),
        glific_client=glific,
        flow_registry=REGISTRY,
    )
    assert result.pending_flow_start == "TAP Buddy"  # organically reached

    starter = FakeFlowStarter()
    rearmed = await maybe_rearm_turn_flow(
        result, _payload(contact_id="c-organic"), turn_flow_id="80", glific_client=starter
    )

    assert rearmed is False
    assert starter.started == []


# ---- AC-7: reachability test (P-005 shape) --------------------------------


def test_start_contact_flow_call_site_for_pending_flow_start_exists_in_turn_processor() -> None:
    """AST-walks the real `app/worker/turn_processor.py` module (mirrors
    `tests/core/test_isolation.py`'s AST-walk idiom, P-005) and confirms:
    (a) `_dispatch_pending_flow_start` actually calls
    `glific_client.start_contact_flow` — not just unit-tested against a
    mock in isolation; (b) `process_turn` actually calls
    `_dispatch_pending_flow_start` — the call site is present in the LIVE
    call graph, not merely defined and never invoked (L-011's exact
    unwired-control failure class, the same gap that shipped
    `maybe_rearm_turn_flow` with no caller at F-019).
    """
    module_path = (
        Path(__file__).parent.parent.parent / "app" / "worker" / "turn_processor.py"
    )
    tree = ast.parse(module_path.read_text(), filename=str(module_path))

    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
    }

    assert "_dispatch_pending_flow_start" in functions
    assert "process_turn" in functions

    def _calls_attr_named(fn_node: ast.AST, attr_name: str) -> bool:
        for node in ast.walk(fn_node):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == attr_name
            ):
                return True
        return False

    def _calls_name(fn_node: ast.AST, func_name: str) -> bool:
        for node in ast.walk(fn_node):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == func_name
            ):
                return True
        return False

    assert _calls_attr_named(functions["_dispatch_pending_flow_start"], "start_contact_flow"), (
        "_dispatch_pending_flow_start must call glific_client.start_contact_flow"
    )
    assert _calls_name(functions["process_turn"], "_dispatch_pending_flow_start"), (
        "process_turn must actually call _dispatch_pending_flow_start (not just "
        "define it) -- the exact unwired-control-flow gap L-011 was written for"
    )
