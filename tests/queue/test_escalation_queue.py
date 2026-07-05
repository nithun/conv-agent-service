"""Code-review fix (2026-07-05, Finding 1): the escalation queue's own
enqueue/read/ack primitives — the Redis Stream a distress hit now goes onto
instead of a synchronous, inline Glific call from the ingress webhook
handler (L-002).
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.queue.escalation_queue import (
    ESCALATION_STREAM_KEY,
    ack_escalation_entry,
    enqueue_escalation,
    ensure_escalation_consumer_group,
    read_next_escalation_entry,
)


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_enqueue_escalation_writes_to_the_shared_escalation_stream(redis) -> None:
    entry_id = await enqueue_escalation(redis, {"contact_id": "1", "text": "help me"})

    assert entry_id
    entries = await redis.xrange(ESCALATION_STREAM_KEY)
    assert len(entries) == 1
    assert entries[0][1]["contact_id"] == "1"


@pytest.mark.asyncio
async def test_enqueue_escalation_is_a_single_cheap_round_trip_no_glific_import() -> None:
    """Structural guard, lighter-weight companion to the AST-scan test in
    tests/queue/test_isolation.py: this module must not import anything
    Glific-client-shaped — it is a pure Redis enqueue, safe to await inline
    on the fast-ack path.
    """
    import ast
    from pathlib import Path

    module_path = (
        Path(__file__).parent.parent.parent / "app" / "queue" / "escalation_queue.py"
    )
    tree = ast.parse(module_path.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    assert not any("glific_client" in name for name in imported)


@pytest.mark.asyncio
async def test_read_next_escalation_entry_returns_none_when_empty(redis) -> None:
    entry = await read_next_escalation_entry(redis, "worker-1", block_ms=50)
    assert entry is None


@pytest.mark.asyncio
async def test_read_ack_round_trip(redis) -> None:
    await enqueue_escalation(redis, {"contact_id": "2", "text": "help"})

    entry = await read_next_escalation_entry(redis, "worker-1", block_ms=100)
    assert entry is not None
    entry_id, fields = entry
    assert fields["contact_id"] == "2"

    await ack_escalation_entry(redis, entry_id)

    # Acked — no longer pending for this consumer group.
    pending = await redis.xpending(ESCALATION_STREAM_KEY, "tap_agent_escalation_workers")
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_ensure_escalation_consumer_group_is_idempotent(redis) -> None:
    # Calling twice must not raise (BUSYGROUP is swallowed).
    await ensure_escalation_consumer_group(redis)
    await ensure_escalation_consumer_group(redis)


@pytest.mark.asyncio
async def test_ordering_rapid_escalations_preserved(redis) -> None:
    """Multiple rapid distress hits (even across different contacts) are
    read back in the order they were enqueued — Redis Streams' FIFO
    guarantee applies to this shared stream the same way it does to each
    per-contact turn stream.
    """
    for i in range(5):
        await enqueue_escalation(redis, {"contact_id": f"c{i}", "seq": str(i)})

    entries = await redis.xrange(ESCALATION_STREAM_KEY)
    seqs = [e[1]["seq"] for e in entries]
    assert seqs == ["0", "1", "2", "3", "4"]
