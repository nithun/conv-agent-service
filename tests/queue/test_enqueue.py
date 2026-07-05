"""AC-3: Redis SETNX dedupe + contact_id-partitioned Stream enqueue.

Uses fakeredis (an in-memory Redis stand-in with real Streams/SETNX
semantics) so these tests run without a live Redis instance while still
exercising the real redis-py async API surface, not a hand-rolled mock.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.queue.enqueue import enqueue_turn, is_duplicate_message
from app.queue.settings import stream_key


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_first_message_is_not_a_duplicate(redis) -> None:
    assert await is_duplicate_message(redis, "msg-1") is False


@pytest.mark.asyncio
async def test_dedupe_duplicate_message_id_single_processing(redis) -> None:
    """AC-3(a): the same message_id delivered twice is flagged a duplicate
    the second time — the caller (ingress) acks without re-enqueuing, so
    exactly one turn is processed and one outbound reply sent.
    """
    first = await is_duplicate_message(redis, "msg-dup")
    second = await is_duplicate_message(redis, "msg-dup")

    assert first is False
    assert second is True


@pytest.mark.asyncio
async def test_dedupe_is_per_message_id_not_global(redis) -> None:
    assert await is_duplicate_message(redis, "msg-a") is False
    assert await is_duplicate_message(redis, "msg-b") is False
    assert await is_duplicate_message(redis, "msg-a") is True


@pytest.mark.asyncio
async def test_enqueue_turn_writes_to_the_contacts_own_stream(redis) -> None:
    entry_id = await enqueue_turn(redis, "contact-42", {"text": "hello", "message_id": "m1"})

    assert entry_id
    entries = await redis.xrange(stream_key("contact-42"))
    assert len(entries) == 1
    assert entries[0][1]["text"] == "hello"


@pytest.mark.asyncio
async def test_enqueue_turn_does_not_leak_across_contacts(redis) -> None:
    await enqueue_turn(redis, "contact-1", {"text": "one"})
    await enqueue_turn(redis, "contact-2", {"text": "two"})

    contact_1_entries = await redis.xrange(stream_key("contact-1"))
    contact_2_entries = await redis.xrange(stream_key("contact-2"))

    assert [e[1]["text"] for e in contact_1_entries] == ["one"]
    assert [e[1]["text"] for e in contact_2_entries] == ["two"]


@pytest.mark.asyncio
async def test_ordering_rapid_messages_same_contact(redis) -> None:
    """AC-3(b)/L-003: >=2 rapid messages from the same contact_id are
    processed in the order sent. Enqueue simulates "rapid" by issuing the
    XADD calls back-to-back with no delay; Redis Streams preserve FIFO
    order of XADD within one stream key, so reading the stream back in
    order must reproduce the send order exactly.
    """
    for i in range(5):
        await enqueue_turn(redis, "contact-rapid", {"seq": str(i)})

    entries = await redis.xrange(stream_key("contact-rapid"))
    seqs = [e[1]["seq"] for e in entries]

    assert seqs == ["0", "1", "2", "3", "4"]
