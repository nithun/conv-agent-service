"""Top-level distress gate handler (CR-004 Task 8, BR-003, Invariant 4,
AC-9). This is the PRIMARY distress check — invoked directly from
`app/ingress/glific_webhook.py` BEFORE any state handling (identity
resolution, greeting/readiness, mode_choice) and before enqueue, so it is
reachable from a child's very first ~3 turns exactly like BR-003's fix
required in tap-buddy (`_maybe_escalate_on_distress` running ahead of ALL
state branches, not only inside `run_turn`).

On distress, in ANY state (including identity_confirmation with no
resolved identity yet — `escalation_reason`/ticket may carry no
student_id, that's acceptable, safety over attribution): escalate the
session (or create one if none exists yet), send the scripted close reply,
open a Glific Ticket (Task 13's escalation surface — CR-002/L-007, NOT
tap-buddy's dropped ad-hoc `/review` dashboard), and STOP. The message is
never enqueued for normal turn processing.
"""

from __future__ import annotations

from typing import Protocol

from app.config.scripted_replies import get_scripted_reply
from app.core.state_machine import (
    EscalationReason,
    SessionData,
    SessionState,
    escalate,
    new_session,
)
from app.ingress.glific_payload import GlificWebhookPayload
from app.worker.escalation import open_ticket_for_escalation
from app.worker.session_store import RedisSessionStore


class _GlificSender(Protocol):
    async def send_text(self, receiver_id: str, body_text: str) -> object: ...
    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> object: ...


async def _escalate_for_distress(
    session: SessionData,
    contact_id: str,
    *,
    session_store: RedisSessionStore,
    glific_client: _GlificSender,
) -> None:
    """The escalate + save + close-reply + ticket sequence shared by BOTH
    distress gates: this module's own ingress-reachable `handle_distress`
    (which loads/creates its own session) and
    `app/worker/turn_processor.py`'s worker-side, audio-transcript-only
    distress check (BR-004 Task 3), which has ALREADY loaded (or just
    created) its session and must not re-load it a second time or
    duplicate this 3-step sequence verbatim.

    Never raises on a Ticket-creation failure (logs instead, via
    `open_ticket_for_escalation`) — a child-safety close reply must go out
    even if the Ticket call itself fails; the reply already tells the
    child a human is coming.
    """
    escalate(session, EscalationReason.DISTRESS)
    await session_store.save_session(session)

    reply_text = get_scripted_reply("distress_close", session.identity.tap_language)
    await glific_client.send_text(contact_id, reply_text)

    await open_ticket_for_escalation(contact_id, session, glific_client)


async def handle_distress(
    payload: GlificWebhookPayload,
    *,
    session_store: RedisSessionStore,
    glific_client: _GlificSender,
) -> None:
    """Escalates the session, sends the scripted close reply, and opens a
    Glific Ticket — the top-level, ingress-reachable distress handler.
    Never raises on a Ticket-creation failure (logs instead, via
    `open_ticket_for_escalation`) — a child-safety close reply must go out
    even if the Ticket call itself fails; the reply already tells the
    child a human is coming.
    """
    session = await session_store.load_session(payload.contact_id)
    if session is None:
        session = new_session(payload.contact_id, payload.phone, needs_identity_confirmation=False)

    await _escalate_for_distress(
        session, payload.contact_id, session_store=session_store, glific_client=glific_client
    )


def session_is_terminal(state: SessionState) -> bool:
    return state in (SessionState.CLOSE, SessionState.ESCALATED)
