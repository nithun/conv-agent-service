"""Enqueue path: Redis SETNX dedupe on Glific `message_id`, then XADD onto
the contact's own Redis Stream (R-3, ADR-010 highest-risk item — built
fresh, no tap-buddy equivalent to port; tap-buddy's ordering guarantee was
"whatever asyncio's scheduler happens to give a single process," explicitly
not carried forward, per ADR-010's Risk 3).

Both operations here are single, cheap Redis round-trips — safe to call
inline from the fast-ack webhook handler (L-002) unlike any LLM/Frappe/media
call.
"""

from __future__ import annotations

from typing import cast

from redis.asyncio import Redis

from app.queue.settings import (
    MESSAGE_DEDUPE_TTL_SECONDS,
    dedupe_key,
    stream_key,
)

# redis-py's stub types xadd's `fields` as `Dict[<broad union>, <broad union>]`
# (invariant `Dict`, not `Mapping`) — a concrete `dict[str, str]` is a valid
# value at runtime (str is one of the allowed key/value types) but mypy's
# invariance rejects the narrower type without a cast. This alias documents
# why the cast below exists rather than scattering `# type: ignore`.
_StreamFields = dict[
    bytes | bytearray | memoryview | str | int | float,
    bytes | bytearray | memoryview | str | int | float,
]


async def is_duplicate_message(redis: Redis, message_id: str) -> bool:
    """SETNX-based dedupe check-and-mark in one round-trip: `SET key val NX
    EX ttl` returns None if the key already existed (duplicate), or sets it
    (first time seen) if not. Returns True for a duplicate — caller should
    ack without enqueuing.
    """
    was_set = await redis.set(dedupe_key(message_id), "1", nx=True, ex=MESSAGE_DEDUPE_TTL_SECONDS)
    return was_set is None


async def enqueue_turn(redis: Redis, contact_id: str, payload: dict[str, str]) -> str:
    """XADD the turn payload onto this contact's own Stream. Redis Streams
    preserve FIFO order of XADD calls within one stream, so partitioning by
    contact_id (one stream key per contact) is what gives ordering a
    structural guarantee, not an accidental one — this is the mechanism
    behind AC-3(b)/L-003's ordering test.

    Fields are stored as strings (Redis Streams require flat string/bytes
    field values); the payload dict is flattened by the caller
    (`app/ingress/glific_webhook.py`) before calling this.
    """
    entry_id: bytes | str = await redis.xadd(stream_key(contact_id), cast(_StreamFields, payload))
    return entry_id if isinstance(entry_id, str) else entry_id.decode()
