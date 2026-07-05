"""Consumer-side helpers: reading a contact's Stream in order via a Redis
consumer group (so unacked entries survive a worker crash/restart, per
P-002's retry/DLQ shape), and the dead-letter stream a job lands on after
exhausting its retries.

`app/worker/` is the caller — this module only wraps the Redis primitives
(XGROUP CREATE, XREADGROUP, XACK, XADD-to-DLQ), never the turn-processing
logic itself.
"""

from __future__ import annotations

from typing import cast

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.queue.settings import CONSUMER_GROUP_NAME, stream_key

# See app/queue/enqueue.py's _StreamFields comment: redis-py's xadd stub
# types `fields` as an invariant `Dict[<broad union>, <broad union>]`, so a
# concrete dict[str, str] needs a cast even though it's runtime-valid.
_StreamFields = dict[
    bytes | bytearray | memoryview | str | int | float,
    bytes | bytearray | memoryview | str | int | float,
]

DEAD_LETTER_STREAM_KEY = "tap_agent:stream:dead_letter"
MAX_DELIVERY_ATTEMPTS = 3


async def ensure_consumer_group(redis: Redis, contact_id: str) -> None:
    """Idempotently creates the consumer group for this contact's stream —
    `mkstream=True` so the group can be created even before the stream has
    any entries (partial-deployment edge case: ingress live before worker,
    per CR-004 Edge cases — messages queue up rather than vanishing).
    """
    try:
        await redis.xgroup_create(
            stream_key(contact_id), CONSUMER_GROUP_NAME, id="0", mkstream=True
        )
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def read_next_entry(
    redis: Redis, contact_id: str, consumer_name: str, block_ms: int = 1000
) -> tuple[str, dict[str, str]] | None:
    """Reads exactly one new (never-delivered) entry for this contact via
    the consumer group — `>` means "only entries never delivered to any
    consumer in this group," which is what gives strict in-order,
    exactly-once-delivery-attempt semantics per contact. Returns
    (entry_id, fields) or None if nothing is pending within `block_ms`.
    """
    await ensure_consumer_group(redis, contact_id)
    response = await redis.xreadgroup(
        CONSUMER_GROUP_NAME,
        consumer_name,
        {stream_key(contact_id): ">"},
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


async def ack_entry(redis: Redis, contact_id: str, entry_id: str) -> None:
    await redis.xack(stream_key(contact_id), CONSUMER_GROUP_NAME, entry_id)


async def dead_letter(redis: Redis, contact_id: str, entry_id: str, fields: dict[str, str]) -> None:
    """Moves a permanently-failed entry to the dead-letter stream (P-002's
    existing shape) and acks it off the contact's own stream so the
    consumer group doesn't keep redelivering it — a poison entry must not
    block that contact's subsequent messages forever.
    """
    await redis.xadd(
        DEAD_LETTER_STREAM_KEY,
        cast(
            _StreamFields,
            {**fields, "contact_id": contact_id, "original_entry_id": entry_id},
        ),
    )
    await ack_entry(redis, contact_id, entry_id)
