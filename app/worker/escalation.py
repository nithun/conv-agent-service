"""Escalation-to-Ticket handoff (CR-004 Task 13, Invariant 5, AC-10).

Every escalation trigger — distress (ingress-level top-level gate,
`app/worker/distress_gate.py`, OR the `run_turn` defense-in-depth path for
turns that reach the state machine) and re-explanation-cap-exceeded
(`EscalationReason.UNRESOLVED_DOUBT`) — opens a Glific Ticket via
`GlificClient.create_ticket` (CR-002/L-007 shape). `adapters/review.py`
(tap-buddy's ad-hoc HTML dashboard) is dropped ENTIRELY, not ported in any
form — no read-only ops-dashboard equivalent exists anywhere in this
codebase (Nithun's decision, ADR-010 Invariant 5).

"Repeated off-turn" is listed as a trigger condition in CR-004's own text,
but tap-buddy never actually implements an off-turn escalation cap/counter
(`handle_off_turn` only redirects; no `EscalationReason` value exists for
it) — this port does not invent that behavior (MVP discipline: build only
what's actually there to port, not a hypothetical future state). If a
repeated-off-turn escalation is wanted, that is new scope for a future
PRD/CR, not silently added here.
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.core.state_machine import EscalationReason, SessionData

logger = logging.getLogger("app.worker.escalation")

_TOPIC_BY_REASON: dict[EscalationReason, str] = {
    EscalationReason.DISTRESS: "Distress escalation",
    EscalationReason.UNRESOLVED_DOUBT: "Unresolved doubt escalation",
    EscalationReason.ASR_FAILURE: "ASR failure escalation",
}


class _TicketingClient(Protocol):
    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> object: ...


async def open_ticket_for_escalation(
    contact_id: str, session: SessionData, glific_client: _TicketingClient
) -> None:
    """Opens a Glific Ticket for an already-escalated session. Never raises
    on failure (logs instead) — the child-facing reply for this turn has
    already been decided/sent by the caller; a Ticket-creation failure must
    not additionally break turn processing.
    """
    reason = session.escalation_reason or EscalationReason.DISTRESS
    topic = _TOPIC_BY_REASON.get(reason, "Escalation")
    try:
        await glific_client.create_ticket(
            contact_id,
            topic=topic,
            body_text=(
                f"{topic} for contact {contact_id}. "
                f"Student: {session.identity.child_name or 'unresolved'}. "
                f"Reason: {reason.value}. "
                "Session escalated automatically; review conversation history in Glific."
            ),
        )
    except Exception:
        logger.exception(
            "failed to open Glific Ticket for escalation reason=%s contact=%s",
            reason.value,
            contact_id,
        )
