"""Per-contact consumer-group processing loop (CR-004 Task 7/8, P-002):
drains one contact's Redis Stream in order, calling `process_turn` for
each entry, with retry/backoff + dead-letter wiring so failures never
crash the consumer loop.

This is deliberately a plain async function (not literally an `arq.Worker`
job) — the ordering guarantee this module exists to provide comes from
processing ONE contact's stream strictly sequentially via the consumer
group (`app/queue/consumer.py`), which is orthogonal to whichever job
scheduler dispatches `process_one_contact_stream` calls across contacts
(arq, a simple asyncio task pool, etc. — that dispatch layer is
infrastructure, not part of this CR's ordering-correctness surface).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis

from app.ingress.glific_payload import GlificWebhookPayload
from app.queue.consumer import MAX_DELIVERY_ATTEMPTS, ack_entry, dead_letter, read_next_entry
from app.queue.idempotency import is_reply_already_sent, mark_reply_sent_if_not_already
from app.worker.turn_processor import TurnProcessingResult

logger = logging.getLogger("app.worker.consumer_loop")

ProcessTurnFn = Callable[[GlificWebhookPayload], Awaitable[TurnProcessingResult]]


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


async def process_one_entry(
    redis: Redis,
    contact_id: str,
    consumer_name: str,
    entry_id: str,
    fields: dict[str, str],
    *,
    process_turn_fn: ProcessTurnFn,
    attempt: int = 1,
) -> TurnProcessingResult | None:
    """Processes exactly one Stream entry, then acks it (on success) or
    dead-letters it (after MAX_DELIVERY_ATTEMPTS) — never crashes the
    caller's loop on a processing failure.

    Idempotency (Task 6): a READ-ONLY send-guard check runs BEFORE calling
    `process_turn_fn` — if a PRIOR attempt already completed successfully
    (the turn fully ran, including its outbound send(s), but the worker
    crashed before the ack call reached Redis, so the consumer group
    redelivered the same still-pending entry_id), this call skips straight
    to acking without re-invoking `process_turn_fn` at all. The guard
    itself is only MARKED after `process_turn_fn` returns successfully — a
    genuinely FAILED attempt never marks it, so retries-after-failure are
    NOT blocked (that would defeat the retry/dead-letter mechanism below).
    """
    payload = _payload_from_stream_fields(fields)

    if await is_reply_already_sent(redis, contact_id, entry_id):
        await ack_entry(redis, contact_id, entry_id)
        return None

    try:
        result = await process_turn_fn(payload)
    except Exception:
        logger.exception(
            "turn processing failed for contact=%s entry=%s (attempt %d/%d)",
            contact_id,
            entry_id,
            attempt,
            MAX_DELIVERY_ATTEMPTS,
        )
        if attempt >= MAX_DELIVERY_ATTEMPTS:
            await dead_letter(redis, contact_id, entry_id, fields)
        # A failed attempt does NOT mark the send-guard and does NOT ack —
        # the consumer group keeps the entry pending for a subsequent read
        # (retry), up to the dead-letter cap above. Does not re-raise: a
        # crashed consumer loop would stop processing every OTHER contact
        # too.
        return None

    await mark_reply_sent_if_not_already(redis, contact_id, entry_id)
    await ack_entry(redis, contact_id, entry_id)
    return result


async def drain_contact_stream_once(
    redis: Redis,
    contact_id: str,
    consumer_name: str,
    *,
    process_turn_fn: ProcessTurnFn,
    block_ms: int = 1000,
) -> TurnProcessingResult | None:
    """Reads and processes exactly the next pending entry for this
    contact, in order (the consumer group guarantees no other consumer in
    this group has already claimed it). Returns None if nothing was
    pending within `block_ms`.
    """
    entry = await read_next_entry(redis, contact_id, consumer_name, block_ms=block_ms)
    if entry is None:
        return None
    entry_id, fields = entry
    return await process_one_entry(
        redis, contact_id, consumer_name, entry_id, fields, process_turn_fn=process_turn_fn
    )
