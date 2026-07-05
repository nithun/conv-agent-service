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

import logging
from typing import Protocol

from app.core.state_machine import EscalationReason, SessionState, escalate, new_session
from app.core.turn_logic import DISTRESS_CLOSE_REPLY
from app.ingress.glific_payload import GlificWebhookPayload
from app.worker.session_store import RedisSessionStore

logger = logging.getLogger("app.worker.distress_gate")


class _GlificSender(Protocol):
    async def send_text(self, receiver_id: str, body_text: str) -> object: ...
    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> object: ...


async def handle_distress(
    payload: GlificWebhookPayload,
    *,
    session_store: RedisSessionStore,
    glific_client: _GlificSender,
) -> None:
    """Escalates the session, sends the scripted close reply, and opens a
    Glific Ticket — the top-level, ingress-reachable distress handler.
    Never raises on a Ticket-creation failure (logs instead) — a
    child-safety close reply must go out even if the Ticket call itself
    fails; the reply already tells the child a human is coming.
    """
    session = await session_store.load_session(payload.contact_id)
    if session is None:
        session = new_session(payload.contact_id, payload.phone, needs_identity_confirmation=False)

    escalate(session, EscalationReason.DISTRESS)
    await session_store.save_session(session)

    await glific_client.send_text(payload.contact_id, DISTRESS_CLOSE_REPLY)

    try:
        await glific_client.create_ticket(
            payload.contact_id,
            topic="Distress escalation",
            body_text=(
                f"Distress keyword detected for contact {payload.contact_id}. "
                f"Student: {session.identity.child_name or 'unresolved'}. "
                "Session escalated automatically; review conversation history in Glific."
            ),
        )
    except Exception:
        logger.exception(
            "failed to open Glific Ticket for distress escalation; "
            "close reply was still sent to the child"
        )


def session_is_terminal(state: SessionState) -> bool:
    return state in (SessionState.CLOSE, SessionState.ESCALATED)
