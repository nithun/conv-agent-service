"""Glific webhook ingress (CR-004 Task 3): fast-ack, payload parsing,
dedupe, top-level distress gate, enqueue onto the contact's Redis Stream.

Fast-ack contract (L-002, ADR-010 Invariant 2): the endpoint ACKs within
<200ms — no LLM/Frappe/media call inline. The only work done here is: parse
payload (pure), a single Redis SETNX dedupe round-trip (Task 5), the
deterministic (non-LLM) distress keyword check as the TOP-LEVEL safety gate
BEFORE any state handling (BR-003/AD-002's data-flow placement — ahead of
identity resolution, greeting/readiness, and mode_choice, none of which run
in this fast-ack path at all; the real state machine runs in the worker), a
single Redis XADD enqueue (Task 5), and — on a distress hit — a SECOND
cheap XADD onto the dedicated escalation stream (`app/queue/escalation_queue.py`).

Code-review fix (2026-07-05, Finding 1 — L-002 regression): this handler
used to `await distress_handler(...)` inline, which performed REAL Glific
network calls (`GlificClient.send_text`, `create_ticket` via
`open_ticket_for_escalation`) before acking — `GlificClient`'s httpx client
defaults to a 10s timeout, so a slow/hung Glific call could hold the webhook
open past Glific's 4s flow-webhook budget. The Glific-facing side effects
now run only in the worker (`app/worker/escalation_consumer.py`), off this
ack path, exactly like every other reply. All other turn processing
(identity resolution, LLM calls, tool calls, guardrails) also happens in
`app/worker/`, never here.

CR-009 (2026-07-17): the route gained a signature-verification dependency
(`app/ingress/webhook_auth.py::verify_glific_webhook_signature`), wired via
`dependencies=[Depends(...)]` — it runs BEFORE this handler body, ahead of
JSON parsing/dedupe/distress-gate/enqueue, and rejects with a uniform 401
(or 413 for an oversized body) before any of those steps run. No line in
this handler body changed for CR-009.

CR-010 (2026-07-25, F-042/F-043): the route gained a second dependency,
`app/ingress/contact_allowlist.py::verify_contact_allowlisted`, wired as
the SECOND entry in `dependencies=`, after signature verification —
rejected (403) any request whose `contact_id`/`phone` was not on the
resolved allowlist, before dedupe/distress-gate/enqueue. Ordering was
load-bearing: signature verification ran first, since an unauthenticated
claim about `contact_id` is worthless to check against anything.

CR-011 (2026-07-25, keyword-triggered conv-AI test flow, ADR-013): that
second dependency is REMOVED from `dependencies=` below — a closed-set
allowlist and the open, keyword-triggered self-provisioning flow CR-011
ships are structurally incompatible in the same request path (§Proposed
behavior 5, "disabled, not deleted"). `app/ingress/contact_allowlist.py`
and its own test suite are UNCHANGED and stay dormant, not deleted —
reversibility (a future broader-population trial can re-add the single
`Depends()` entry below without rebuilding anything). No line in this
handler body changed for either CR-010 or CR-011.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from app.core.guardrails_checks import detect_distress
from app.ingress.glific_payload import GlificWebhookPayload, parse_glific_webhook_payload
from app.ingress.webhook_auth import verify_glific_webhook_signature
from app.queue.enqueue import enqueue_turn, is_duplicate_message
from app.queue.escalation_queue import enqueue_escalation

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


@router.post(
    "/webhook/glific",
    dependencies=[
        Depends(verify_glific_webhook_signature),
    ],
)
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
    #
    # Finding 1 fix (2026-07-05): the actual Glific send + Ticket-open
    # (real network calls, up to GlificClient's 10s httpx timeout) must
    # never run inline here (L-002) — this handler only does a second cheap
    # XADD onto the dedicated escalation stream and acks immediately. The
    # worker (`app/worker/escalation_consumer.py`) drains that stream
    # asynchronously and performs the real Glific-facing side effects,
    # exactly like every other turn reply already goes through the worker.
    if payload.text and detect_distress(payload.text, None):
        try:
            await enqueue_escalation(redis, _flatten_payload_for_stream(payload))
        except Exception:
            # Mirrors the prior resilience guarantee
            # (test_distress_handler_exception_does_not_500_the_webhook_response):
            # a Redis outage on the escalation-enqueue round-trip must never
            # 500 the fast-ack response the Glific flow is waiting on. The
            # deterministic distress detection itself already happened; a
            # failure to enqueue is logged for manual follow-up, not a
            # reason to fail this webhook call.
            logger.exception(
                "failed to enqueue escalation for contact_id=%s; webhook still acks "
                "(L-002) — escalation delivery may need manual follow-up",
                payload.contact_id,
            )
        return {"status": "ok", "escalated": True}

    await enqueue_turn(redis, payload.contact_id, _flatten_payload_for_stream(payload))

    return {"status": "ok"}
