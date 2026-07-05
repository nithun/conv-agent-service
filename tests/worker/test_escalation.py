"""AC-10/Invariant 5 (CR-004 Task 13): every escalation trigger opens a
Glific Ticket via the Ticketing System — zero calls to any bespoke
escalation-view equivalent of `adapters/review.py`, and no `/review`-style
route exists anywhere in this codebase (dropped entirely, per Nithun's
2026-07-05 decision).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import fakeredis.aioredis
import pytest

from app.config.settings import Settings
from app.core.state_machine import EscalationReason, SessionState, StudentIdentity, new_session
from app.ingress.glific_payload import GlificWebhookPayload
from app.tools.lms_mock import MockTapLmsClient
from app.worker.distress_gate import handle_distress
from app.worker.escalation import open_ticket_for_escalation
from app.worker.session_store import RedisSessionStore
from app.worker.turn_processor import process_turn


@dataclass
class FakeGlificSender:
    sent_texts: list[tuple[str, str]] = field(default_factory=list)
    tickets: list[tuple[str, str, str]] = field(default_factory=list)

    async def send_text(self, receiver_id: str, body_text: str) -> dict[str, str]:
        self.sent_texts.append((receiver_id, body_text))
        return {"id": "fake-message-id"}

    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> dict[str, str]:
        self.tickets.append((contact_id, topic, body_text))
        return {"id": "fake-ticket-id"}


@pytest.fixture
def session_store():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = Settings(_env_file=None)
    return RedisSessionStore(redis, settings)


@pytest.mark.asyncio
async def test_escalation_opens_glific_ticket_not_review_view(session_store) -> None:
    """AC-10 literal: the SAME trigger condition (distress) that fired the
    old SQLite-row path (tap-buddy) now produces a Ticket, not just that
    the old row would have been written.
    """
    glific = FakeGlificSender()
    payload = GlificWebhookPayload(
        contact_id="20", phone="919999999999", message_id="e1", text="I want to kill myself"
    )

    await handle_distress(payload, session_store=session_store, glific_client=glific)

    assert len(glific.tickets) == 1
    contact_id, topic, body = glific.tickets[0]
    assert contact_id == "20"
    assert "distress" in topic.lower()


@pytest.mark.asyncio
async def test_reexplanation_cap_exceeded_opens_glific_ticket_via_turn_processor(
    session_store,
) -> None:
    """The re-explanation-cap-exceeded trigger (EscalationReason.UNRESOLVED_DOUBT),
    reachable through run_turn/turn_processor (not the ingress-level
    distress gate), also opens a Ticket.
    """
    from app.core.state_machine import transition

    session = new_session("21", "919999999999", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    transition(session, SessionState.MODE_CHOICE)
    transition(session, SessionState.CONTENT_INTRO)
    transition(session, SessionState.ACTIVITY_IN_PROGRESS)
    transition(session, SessionState.DOUBT_CLEARING)
    session.reexplanation_count = 2  # already at MAX_REEXPLANATIONS
    await session_store.save_session(session)

    glific = FakeGlificSender()
    lms = MockTapLmsClient()

    class UnusedModelRouter:
        async def generate(self, *a, **kw):
            raise AssertionError("model must not be called on the escalating doubt turn")

    payload = GlificWebhookPayload(
        contact_id="21",
        phone="919999999999",
        message_id="e2",
        text="I really still don't get it",
    )

    result = await process_turn(
        payload,
        session_store=session_store,
        lms_client=lms,
        model_router=UnusedModelRouter(),
        glific_client=glific,
    )

    assert result.escalated is True
    assert result.escalation_reason == EscalationReason.UNRESOLVED_DOUBT
    assert len(glific.tickets) == 1
    assert "doubt" in glific.tickets[0][1].lower()


@pytest.mark.asyncio
async def test_ticket_creation_failure_does_not_raise(session_store) -> None:
    """open_ticket_for_escalation never raises on a Ticket-API failure —
    logs instead. The already-sent child-safety reply must not be undone
    by a downstream Ticketing outage.
    """

    class FailingGlificSender:
        async def create_ticket(self, *a, **kw):
            raise RuntimeError("Glific Ticketing outage")

    session = new_session("22", "919999999999", needs_identity_confirmation=False)
    session.escalation_reason = EscalationReason.DISTRESS

    # Must not raise.
    await open_ticket_for_escalation("22", session, FailingGlificSender())


def test_no_review_route_exists() -> None:
    """AC-10: no /review-style route exists anywhere in this codebase —
    tap-buddy's adapters/review.py is dropped ENTIRELY, not ported in any
    form.
    """
    from app.main import app

    review_routes = [
        route for route in app.routes if hasattr(route, "path") and "review" in route.path.lower()
    ]
    assert review_routes == []


def test_no_review_module_exists_in_the_app_package() -> None:
    """Zero calls to any bespoke escalation-view equivalent exist anywhere
    in the ported code — confirmed by the module (or its parent package)
    simply not existing.
    """
    import importlib.util

    for candidate in ("app.adapters.review", "app.ingress.review", "app.delivery.review"):
        try:
            spec = importlib.util.find_spec(candidate)
        except ModuleNotFoundError:
            spec = None  # the parent package itself doesn't exist — also confirms absence
        assert spec is None, f"unexpected module: {candidate}"
