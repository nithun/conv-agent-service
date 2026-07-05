"""P-002 retry/DLQ shape: consumer-group read/ack/dead-letter primitives,
plus the partial-deployment edge case (ingress live before worker) where
messages must queue rather than vanish.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.queue.consumer import (
    DEAD_LETTER_STREAM_KEY,
    ack_entry,
    dead_letter,
    ensure_consumer_group,
    read_next_entry,
)
from app.queue.enqueue import enqueue_turn


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_ensure_consumer_group_is_idempotent(redis) -> None:
    await ensure_consumer_group(redis, "contact-1")
    # Calling it again must not raise BUSYGROUP.
    await ensure_consumer_group(redis, "contact-1")


@pytest.mark.asyncio
async def test_consumer_group_created_before_any_message_enqueued(redis) -> None:
    """Partial-deployment edge case (CR-004 Edge cases): if the ingress
    route is live before the worker/consumer group exists, an enqueued
    message with no consumer must not silently vanish — mkstream=True lets
    the group be created against an empty/nonexistent stream.
    """
    await ensure_consumer_group(redis, "contact-fresh")
    await enqueue_turn(redis, "contact-fresh", {"text": "hello"})

    entry = await read_next_entry(redis, "contact-fresh", "worker-1", block_ms=100)

    assert entry is not None
    entry_id, fields = entry
    assert fields["text"] == "hello"


@pytest.mark.asyncio
async def test_read_next_entry_returns_none_when_stream_empty(redis) -> None:
    entry = await read_next_entry(redis, "contact-empty", "worker-1", block_ms=50)
    assert entry is None


@pytest.mark.asyncio
async def test_read_next_entry_processes_in_order(redis) -> None:
    for i in range(3):
        await enqueue_turn(redis, "contact-order", {"seq": str(i)})

    seqs = []
    for _ in range(3):
        entry_id, fields = await read_next_entry(redis, "contact-order", "worker-1", block_ms=100)
        seqs.append(fields["seq"])
        await ack_entry(redis, "contact-order", entry_id)

    assert seqs == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_acked_entry_is_not_redelivered(redis) -> None:
    await enqueue_turn(redis, "contact-ack", {"text": "hi"})
    entry_id, _ = await read_next_entry(redis, "contact-ack", "worker-1", block_ms=100)
    await ack_entry(redis, "contact-ack", entry_id)

    # A fresh read should find nothing new — the only entry was acked.
    next_entry = await read_next_entry(redis, "contact-ack", "worker-1", block_ms=50)
    assert next_entry is None


@pytest.mark.asyncio
async def test_dead_letter_moves_entry_and_acks_it_off_the_contact_stream(redis) -> None:
    await enqueue_turn(redis, "contact-poison", {"text": "bad"})
    entry_id, fields = await read_next_entry(redis, "contact-poison", "worker-1", block_ms=100)

    await dead_letter(redis, "contact-poison", entry_id, fields)

    dlq_entries = await redis.xrange(DEAD_LETTER_STREAM_KEY)
    assert len(dlq_entries) == 1
    assert dlq_entries[0][1]["contact_id"] == "contact-poison"
    assert dlq_entries[0][1]["text"] == "bad"

    # Acked off the original stream — no more pending entries for this
    # contact, so the poison entry doesn't block subsequent messages.
    next_entry = await read_next_entry(redis, "contact-poison", "worker-1", block_ms=50)
    assert next_entry is None
