"""Escalation-stream consumer (code-review fix, 2026-07-05, Finding 1):
drains `app/queue/escalation_queue.py`'s shared escalation Stream and
performs the REAL Glific-facing side effects of a distress hit
(`handle_distress` -> `GlificClient.send_text` + `open_ticket_for_escalation`
-> `GlificClient.create_ticket`) — off the pre-ack ingress path, same as
every other turn reply already goes through the worker.

Mirrors `app/worker/consumer_loop.py`'s retry/dead-letter shape, applied to
the escalation stream's own (non-per-contact) consumer group.
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from app.ingress.glific_payload import GlificWebhookPayload
from app.queue.escalation_queue import (
    ack_escalation_entry,
    read_next_escalation_entry,
)
from app.worker.distress_gate import _GlificSender, handle_distress
from app.worker.session_store import RedisSessionStore

logger = logging.getLogger("app.worker.escalation_consumer")

MAX_ESCALATION_DELIVERY_ATTEMPTS = 3


def _payload_from_stream_fields(fields: dict[str, str]) -> GlificWebhookPayload:
    return GlificWebhookPayload(
        contact_id=fields["contact_id"],
        phone=fields["phone"],
        message_id=fields["message_id"],
        message_type=fields.get("message_type", "text"),
        text=fields.get("text") or None,
        media_url=fields.get("media_url") or None,
        contact_name=fields.get("contact_name") or None,
    )


async def process_one_escalation_entry(
    redis: Redis,
    consumer_name: str,
    entry_id: str,
    fields: dict[str, str],
    *,
    session_store: RedisSessionStore,
    glific_client: _GlificSender,
    attempt: int = 1,
) -> bool:
    """Processes exactly one escalation Stream entry, then acks it (on
    success). Never crashes the caller's loop on a processing failure — a
    downstream Glific outage must not stop OTHER escalations (or, if this
    loop shares a process with the turn consumer loop, anything else) from
    being processed.

    Unlike the turn consumer loop, an escalation entry that keeps failing is
    NOT dead-lettered off the child-safety path silently — after
    `MAX_ESCALATION_DELIVERY_ATTEMPTS` it is still acked (so it doesn't
    block the stream forever) but logged at a distinct, greppable severity
    so an operator can find it; `handle_distress` itself already never
    raises on a Ticket-creation failure (only session-store/send_text
    failures reach this except branch).
    """
    payload = _payload_from_stream_fields(fields)

    try:
        await handle_distress(payload, session_store=session_store, glific_client=glific_client)
    except Exception:
        logger.exception(
            "escalation processing failed for contact=%s entry=%s (attempt %d/%d)",
            payload.contact_id,
            entry_id,
            attempt,
            MAX_ESCALATION_DELIVERY_ATTEMPTS,
        )
        if attempt >= MAX_ESCALATION_DELIVERY_ATTEMPTS:
            logger.error(
                "escalation entry=%s for contact=%s permanently failed after %d attempts; "
                "acking to unblock the stream — MANUAL FOLLOW-UP REQUIRED",
                entry_id,
                payload.contact_id,
                MAX_ESCALATION_DELIVERY_ATTEMPTS,
            )
            await ack_escalation_entry(redis, entry_id)
        return False

    await ack_escalation_entry(redis, entry_id)
    return True


async def drain_escalation_stream_once(
    redis: Redis,
    consumer_name: str,
    *,
    session_store: RedisSessionStore,
    glific_client: _GlificSender,
    block_ms: int = 1000,
) -> bool | None:
    """Reads and processes exactly the next pending escalation entry, in
    order. Returns None if nothing was pending within `block_ms`, else
    whether that entry's processing succeeded.
    """
    entry = await read_next_escalation_entry(redis, consumer_name, block_ms=block_ms)
    if entry is None:
        return None
    entry_id, fields = entry
    return await process_one_escalation_entry(
        redis,
        consumer_name,
        entry_id,
        fields,
        session_store=session_store,
        glific_client=glific_client,
    )
