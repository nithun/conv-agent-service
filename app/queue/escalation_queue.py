"""Escalation queue (fast-ack fix, 2026-07-05 code-review Finding 1):
the Glific-facing side effects of a distress hit (`GlificClient.send_text`,
`open_ticket_for_escalation` -> `GlificClient.create_ticket`) must never run
on the pre-ack ingress path (L-002) — `GlificClient`'s httpx client defaults
to a 10s timeout, so awaiting those calls inline can hold the webhook open
well past Glific's 4s flow-webhook budget.

This module gives the distress gate its OWN single (not per-contact) Redis
Stream + consumer group, mirroring `app/queue/enqueue.py`/`app/queue/consumer.py`'s
shape exactly (XADD to enqueue, XREADGROUP/XACK to consume) but deliberately
NOT partitioned by contact_id: escalations don't need cross-contact
ordering against each other (each is a one-shot "escalate this session,
send close reply, open Ticket" job, not a sequence of turns), so one shared
stream keeps the worker side simple (one consumer loop, not N per-contact
ones) while still being a single cheap XADD round-trip on the ingress path —
exactly as cheap as `enqueue_turn`'s per-contact XADD.
"""

from __future__ import annotations

from typing import cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

ESCALATION_STREAM_KEY = "tap_agent:stream:escalation"
ESCALATION_CONSUMER_GROUP_NAME = "tap_agent_escalation_workers"

# Same broad-union cast rationale as app/queue/enqueue.py's _StreamFields —
# redis-py's xadd stub types `fields` as an invariant `Dict`, so a concrete
# dict[str, str] needs a cast even though it's runtime-valid.
_StreamFields = dict[
    bytes | bytearray | memoryview | str | int | float,
    bytes | bytearray | memoryview | str | int | float,
]


async def enqueue_escalation(redis: Redis, payload: dict[str, str]) -> str:
    """XADD onto the shared escalation stream. A single, cheap Redis
    round-trip — safe to call inline from the fast-ack webhook handler
    (L-002), unlike the actual Glific send/Ticket-open calls this enqueue
    defers to the worker.
    """
    entry_id: bytes | str = await redis.xadd(ESCALATION_STREAM_KEY, cast(_StreamFields, payload))
    return entry_id if isinstance(entry_id, str) else entry_id.decode()


async def ensure_escalation_consumer_group(redis: Redis) -> None:
    """Idempotently creates the escalation stream's consumer group —
    `mkstream=True` so the group exists even before any escalation has ever
    been enqueued (same partial-deployment reasoning as
    `app/queue/consumer.py::ensure_consumer_group`).
    """
    try:
        await redis.xgroup_create(
            ESCALATION_STREAM_KEY, ESCALATION_CONSUMER_GROUP_NAME, id="0", mkstream=True
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def read_next_escalation_entry(
    redis: Redis, consumer_name: str, *, block_ms: int = 1000
) -> tuple[str, dict[str, str]] | None:
    """Reads exactly one never-delivered escalation entry via the consumer
    group. Returns (entry_id, fields) or None if nothing pending within
    `block_ms`.
    """
    await ensure_escalation_consumer_group(redis)
    response = await redis.xreadgroup(
        ESCALATION_CONSUMER_GROUP_NAME,
        consumer_name,
        {ESCALATION_STREAM_KEY: ">"},
        count=1,
        block=block_ms,
    )
    if not response:
        return None
    _stream_key, entries = response[0]
    entry_id, fields = entries[0]
    entry_id = entry_id if isinstance(entry_id, str) else entry_id.decode()
    decoded_fields = {
        (k if isinstance(k, str) else k.decode()): (v if isinstance(v, str) else v.decode())
        for k, v in fields.items()
    }
    return entry_id, decoded_fields


async def ack_escalation_entry(redis: Redis, entry_id: str) -> None:
    await redis.xack(ESCALATION_STREAM_KEY, ESCALATION_CONSUMER_GROUP_NAME, entry_id)
