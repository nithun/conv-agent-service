"""Glific webhook ingress (CR-004 Task 3): fast-ack, payload parsing,
dedupe, top-level distress gate, enqueue onto the contact's Redis Stream.

Fast-ack contract (L-002, ADR-010 Invariant 2): the endpoint ACKs within
<200ms — no LLM/Frappe/media call inline. The only work done here is: parse
payload (pure), a single Redis SETNX dedupe round-trip (Task 5), the
deterministic (non-LLM) distress keyword check as the TOP-LEVEL safety gate
BEFORE any state handling (BR-003/AD-002's data-flow placement — ahead of
identity resolution, greeting/readiness, and mode_choice, none of which run
in this fast-ack path at all; the real state machine runs in the worker), a
single Redis XADD enqueue (Task 5), and — only on a distress hit — a
Glific send + Ticket open before acking. All other turn processing
(identity resolution, LLM calls, tool calls, guardrails) happens in
`app/worker/`, never here.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError

from app.core.guardrails_checks import detect_distress
from app.ingress.glific_payload import GlificWebhookPayload, parse_glific_webhook_payload
from app.queue.enqueue import enqueue_turn, is_duplicate_message

logger = logging.getLogger("app.ingress.glific_webhook")

router = APIRouter()


def _flatten_payload_for_stream(payload: GlificWebhookPayload) -> dict[str, str]:
    """Redis Streams require flat string field values — flattens the parsed
    payload into that shape for `enqueue_turn`.
    """
    return {
        "contact_id": payload.contact_id,
        "phone": payload.phone,
        "message_id": payload.message_id,
        "message_type": payload.message_type,
        "text": payload.text or "",
        "media_url": payload.media_url or "",
        "contact_name": payload.contact_name or "",
    }


@router.post("/webhook/glific")
async def glific_webhook(request: Request) -> dict[str, object]:
    """Fast-ack endpoint. Parses the payload, dedupes, checks distress, and
    enqueues onto the contact's Redis Stream partition — then acks
    immediately. The worker (arq) picks up the enqueued job and runs the
    actual state machine/LLM/tool/guardrail/delivery pipeline.
    """
    try:
        raw = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    try:
        payload = parse_glific_webhook_payload(raw)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail=f"malformed Glific payload: {exc}") from exc

    redis = request.app.state.redis

    if await is_duplicate_message(redis, payload.message_id):
        # Already processed (or in flight) — ack without re-enqueuing
        # (AC-3a).
        return {"status": "ok", "duplicate": True}

    # Top-level distress gate (BR-003/Invariant 4/AC-9): deterministic
    # keyword check, cheap enough to run inline on the fast-ack path (no LLM
    # call). Runs BEFORE any state handling — the worker's state machine
    # never even sees a message that trips this gate. Language is unknown
    # at this point (no session/identity resolved yet in the fast-ack
    # path) — detect_distress's own fallback (combined English+Hindi
    # coverage for None) handles that, matching tap-buddy's documented
    # behavior rather than hardcoding "English" here.
    if payload.text and detect_distress(payload.text, None):
        distress_handler = getattr(request.app.state, "distress_handler", None)
        if distress_handler is not None:
            try:
                await distress_handler(payload)
            except Exception:
                # Live-integration-pass finding (2026-07-05): a downstream
                # Glific outage/auth failure during the distress-close-reply
                # send must NEVER turn into a 500 on the fast-ack path
                # (L-002) — the webhook's own contract is "ack fast," not
                # "ack fast unless the escalation delivery has a problem."
                # The distress detection itself already happened
                # (deterministic, no network call); a failure to actually
                # SEND the close reply / open the Ticket is a delivery
                # concern, logged here for follow-up, not a reason to fail
                # the webhook response the Glific flow is waiting on.
                logger.exception(
                    "distress_handler failed for contact_id=%s; webhook still acks "
                    "(L-002) — escalation delivery may need manual follow-up",
                    payload.contact_id,
                )
        else:
            logger.warning(
                "distress detected but no distress_handler wired on app.state; "
                "message NOT enqueued for normal processing, escalation NOT sent"
            )
        return {"status": "ok", "escalated": True}

    await enqueue_turn(redis, payload.contact_id, _flatten_payload_for_stream(payload))

    return {"status": "ok"}
