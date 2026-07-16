"""Turn-processing entrypoint (CR-004 Task 7/8): the worker-side
counterpart to tap-buddy's `adapters/glific_webhook.py::_process_turn`.

Per Invariant 4/BR-003 (AC-9), the PRIMARY distress check runs at ingress
(`app/ingress/glific_webhook.py`), BEFORE this entrypoint is ever reached —
a distress-flagged message never gets enqueued for normal processing at
all. This module's own `detect_distress` call inside `run_turn` (via
`app/core/turn_logic.py`) remains as defense-in-depth for turns that DO
reach the state machine (activity_in_progress, doubt_clearing, etc.),
mirroring tap-buddy's "PRIMARY check at ingress + backstop inside
run_turn" shape exactly — this entrypoint does not re-implement a THIRD
distress check; the layering is deliberate, not redundant.

Every state-branch reachable here (identity_confirmation, greeting,
mode_choice, and the run_turn-driven states) is deterministic/structural —
"never an LLM's own narrative sense of progress" — mirroring tap-buddy's
`_process_turn` decision tree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.identity import confirm_identity, resolve_identity_by_name
from app.core.state_machine import (
    EscalationReason,
    SessionData,
    SessionState,
    new_session,
    transition,
)
from app.core.turn_logic import ModelRouterLike, run_turn
from app.ingress.glific_payload import GlificWebhookPayload
from app.worker.escalation import open_ticket_for_escalation
from app.worker.session_store import RedisSessionStore

logger = logging.getLogger("app.worker.turn_processor")


class _GlificSender(Protocol):
    """Structural type for the delivery client this module needs — a
    subset of `app.delivery.glific_client.GlificClient`.
    """

    async def send_text(self, receiver_id: str, body_text: str) -> object: ...
    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> object: ...


class _FlowStarter(Protocol):
    """Structural type for the turn-flow re-arm — the one method of
    `app.delivery.glific_client.GlificClient` that `maybe_rearm_turn_flow`
    needs.
    """

    async def start_contact_flow(self, flow_id: str, contact_id: str) -> object: ...


@dataclass
class TurnProcessingResult:
    """What one call to `process_turn` did — the worker/DLQ layer inspects
    this to decide retry/dead-letter outcomes and the escalation-handoff
    caller inspects `escalated`/`escalation_reason`.
    """

    handled: bool
    escalated: bool = False
    escalation_reason: EscalationReason | None = None
    session: SessionData | None = None
    reply_sent: bool = False


async def process_turn(
    payload: GlificWebhookPayload,
    *,
    session_store: RedisSessionStore,
    lms_client: Any,
    model_router: ModelRouterLike,
    glific_client: _GlificSender,
) -> TurnProcessingResult:
    """Processes exactly one enqueued turn. Ported decision tree from
    tap-buddy's `_process_turn` (identity resolution -> greeting/readiness
    -> mode_choice -> run_turn), re-platformed onto Redis session state and
    ModelRouter. The caller (the consumer loop, `app/worker/consumer_loop.py`)
    is responsible for ack/retry/dead-letter around this call — this
    function itself does not touch the Redis Stream.
    """
    user_text = payload.text or ""

    session = await session_store.load_session(payload.contact_id)

    if session is None:
        # First message from this contact — ask for their name before any
        # LMS call (get_student_by_phone_and_name requires buddy_name).
        session = new_session(payload.contact_id, payload.phone, needs_identity_confirmation=True)
        await session_store.save_session(session)
        await glific_client.send_text(payload.contact_id, "Hi! Before we start, what's your name?")
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    if session.state == SessionState.IDENTITY_CONFIRMATION and not session.identity.candidates:
        resolution = await resolve_identity_by_name(
            payload.contact_id, payload.phone, user_text.strip(), lms_client
        )
        session = resolution.session
        await session_store.save_session(session)
        if resolution.needs_confirmation:
            names = ", ".join(c["child_name"] for c in resolution.candidates)
            await glific_client.send_text(
                payload.contact_id, f"I found a few names: {names}. Which one is you?"
            )
            return TurnProcessingResult(handled=True, session=session, reply_sent=True)
        if not resolution.candidates:
            await glific_client.send_text(
                payload.contact_id,
                "I couldn't find that name for this phone number. Can you check the spelling?",
            )
            return TurnProcessingResult(handled=True, session=session, reply_sent=True)
        reply = (
            f"Hi {session.identity.child_name}! How are you feeling today "
            "— excited, okay, or tired?"
        )
        await glific_client.send_text(payload.contact_id, reply)
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    if session.state == SessionState.IDENTITY_CONFIRMATION and session.identity.candidates:
        if confirm_identity(session, user_text.strip()):
            transition(session, SessionState.GREETING)
            await session_store.save_session(session)
            reply = f"Great, hi {session.identity.child_name}! How are you feeling today?"
            await glific_client.send_text(payload.contact_id, reply)
        else:
            await glific_client.send_text(
                payload.contact_id, "Sorry, I didn't recognize that name — can you try again?"
            )
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    # Turn 2's readiness reply: the answer itself is stored raw (no
    # sentiment classifier). This transition and its reply are
    # deterministic/scripted — no model call on this turn.
    if session.state == SessionState.GREETING:
        session.readiness_response = user_text.strip()
        transition(session, SessionState.MODE_CHOICE)
        await session_store.save_session(session)
        reply = (
            "Great. We can talk in voice, or you can type if that feels easier. "
            "What would you like?"
        )
        await glific_client.send_text(payload.contact_id, reply)
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    # Turn 3's mode choice: capture "voice"/"text" verbatim from the
    # child's reply (the mode itself isn't model-decided).
    if session.state == SessionState.MODE_CHOICE and session.mode is None:
        lowered = user_text.strip().lower()
        if "voice" in lowered:
            session.mode = "voice"
        elif "text" in lowered or "type" in lowered:
            session.mode = "text"
        if session.mode is not None:
            transition(session, SessionState.CONTENT_INTRO)
            await session_store.save_session(session)
        else:
            reply = "Would you like to continue in voice, or by typing?"
            await glific_client.send_text(payload.contact_id, reply)
            return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    outcome = await run_turn(
        session=session, user_input=user_text, model_router=model_router, lms_client=lms_client
    )
    await session_store.save_session(outcome.session)

    await glific_client.send_text(payload.contact_id, outcome.reply_text)

    if outcome.escalated:
        # Task 13/Invariant 5: every escalation trigger reachable through
        # run_turn (distress defense-in-depth, unresolved-doubt cap) opens
        # a Glific Ticket — the same handoff surface the ingress-level
        # distress gate uses (app/worker/distress_gate.py), never the
        # dropped ad-hoc review dashboard.
        await open_ticket_for_escalation(payload.contact_id, outcome.session, glific_client)

    return TurnProcessingResult(
        handled=True,
        escalated=outcome.escalated,
        escalation_reason=outcome.session.escalation_reason,
        session=outcome.session,
        reply_sent=True,
    )


async def maybe_rearm_turn_flow(
    result: TurnProcessingResult,
    payload: GlificWebhookPayload,
    *,
    turn_flow_id: str,
    glific_client: _FlowStarter,
) -> bool:
    """Re-arm the 'TAP Buddy Turn' relay flow for this contact after a
    processed turn. Ported from tap-buddy's
    `adapters/glific_webhook.py::_maybe_rearm_turn_flow` — the CR-004 port
    carried the `GLIFIC_TURN_FLOW_ID` setting and the
    `GlificClient.start_contact_flow` method but no call site, found live
    when the F-019 cutover smoke stalled after turn 1 (2026-07-16).

    Why re-arm at all: Glific's webhook Oban jobs are unique on
    (context_id, url, action_id) for 60s, so a wait->webhook loop inside ONE
    flow context silently drops every repeat call — each turn needs a fresh
    flow run. After every reply we start the turn flow again: it parks at a
    wait node, the child's next message resumes it, its webhook node calls
    us, and it ends. No re-arm after close/escalation — the conversation is
    over and the keyword flow is the way back in.

    Never raises: the turn itself already succeeded (reply sent); a re-arm
    failure must not push the already-processed entry into retry/DLQ. It is
    logged and returns False.
    """
    if not turn_flow_id:
        return False
    session = result.session
    if session is not None and session.state in (SessionState.CLOSE, SessionState.ESCALATED):
        return False
    try:
        await glific_client.start_contact_flow(turn_flow_id, payload.contact_id)
        return True
    except Exception:
        logger.exception("turn-flow re-arm failed for contact=%s", payload.contact_id)
        return False
