"""P-002 retry/backoff/dead-letter shape applied to the turn-processing
consumer loop (CR-004 Task 8): a simulated downstream failure results in
retry-then-dead-letter, not a crashed consumer loop.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.queue.consumer import DEAD_LETTER_STREAM_KEY, MAX_DELIVERY_ATTEMPTS, read_next_entry
from app.queue.enqueue import enqueue_turn
from app.worker.consumer_loop import drain_contact_stream_once, process_one_entry


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_successful_turn_processing_acks_entry(redis) -> None:
    await enqueue_turn(
        redis,
        "contact-1",
        {"contact_id": "contact-1", "phone": "919999999999", "message_id": "m1", "text": "hi"},
    )

    calls = []

    async def fake_process_turn(payload):
        calls.append(payload.contact_id)
        return "ok"

    result = await drain_contact_stream_once(
        redis, "contact-1", "worker-1", process_turn_fn=fake_process_turn
    )

    assert result == "ok"
    assert calls == ["contact-1"]

    # Acked — no pending entry remains.
    next_entry = await read_next_entry(redis, "contact-1", "worker-1", block_ms=50)
    assert next_entry is None


@pytest.mark.asyncio
async def test_simulated_downstream_failure_results_in_retry_then_dead_letter(redis) -> None:
    """A simulated failure in this stage results in retry-then-dead-letter,
    not a crashed consumer loop — mirrors the P-002 pattern (WBS 1.6).
    """
    await enqueue_turn(
        redis,
        "contact-2",
        {"contact_id": "contact-2", "phone": "919999999999", "message_id": "m2", "text": "hi"},
    )

    call_count = 0

    async def always_failing_process_turn(payload):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("simulated downstream failure")

    entry = await read_next_entry(redis, "contact-2", "worker-1", block_ms=100)
    assert entry is not None
    entry_id, fields = entry

    for attempt in range(1, MAX_DELIVERY_ATTEMPTS + 1):
        result = await process_one_entry(
            redis,
            "contact-2",
            "worker-1",
            entry_id,
            fields,
            process_turn_fn=always_failing_process_turn,
            attempt=attempt,
        )
        assert result is None  # never crashes/raises out to the caller

    assert call_count == MAX_DELIVERY_ATTEMPTS

    dlq_entries = await redis.xrange(DEAD_LETTER_STREAM_KEY)
    assert len(dlq_entries) == 1
    assert dlq_entries[0][1]["contact_id"] == "contact-2"


@pytest.mark.asyncio
async def test_failed_entry_is_not_acked_before_dead_letter(redis) -> None:
    """A failing attempt below the retry cap must NOT ack the entry — the
    consumer group needs to keep it pending for the next retry attempt.
    """
    await enqueue_turn(
        redis,
        "contact-3",
        {"contact_id": "contact-3", "phone": "919999999999", "message_id": "m3", "text": "hi"},
    )

    async def failing_process_turn(payload):
        raise RuntimeError("boom")

    entry = await read_next_entry(redis, "contact-3", "worker-1", block_ms=100)
    assert entry is not None
    entry_id, fields = entry

    await process_one_entry(
        redis,
        "contact-3",
        "worker-1",
        entry_id,
        fields,
        process_turn_fn=failing_process_turn,
        attempt=1,
    )

    # Still not acked/dead-lettered (attempt 1 of MAX_DELIVERY_ATTEMPTS) —
    # xpending should show this entry as still owned/pending by worker-1.
    pending = await redis.xpending("tap_agent:stream:contact:contact-3", "tap_agent_workers")
    assert pending["pending"] == 1


@pytest.mark.asyncio
async def test_idempotency_guard_prevents_double_send_on_retry(redis) -> None:
    """Task 6: if the send-guard was already marked (a prior attempt got
    far enough to mark it, then "crashed"), a retry must not re-run the
    turn's send step — process_turn_fn is skipped entirely.
    """
    await enqueue_turn(
        redis,
        "contact-4",
        {"contact_id": "contact-4", "phone": "919999999999", "message_id": "m4", "text": "hi"},
    )
    entry = await read_next_entry(redis, "contact-4", "worker-1", block_ms=100)
    assert entry is not None
    entry_id, fields = entry

    call_count = 0

    async def counting_process_turn(payload):
        nonlocal call_count
        call_count += 1
        return "ok"

    first = await process_one_entry(
        redis, "contact-4", "worker-1", entry_id, fields, process_turn_fn=counting_process_turn
    )
    assert first == "ok"
    assert call_count == 1

    # Simulate a retry redelivering the SAME entry_id (e.g. the ack call
    # itself was lost) — the send-guard must prevent a second run.
    second = await process_one_entry(
        redis, "contact-4", "worker-1", entry_id, fields, process_turn_fn=counting_process_turn
    )
    assert second is None
    assert call_count == 1  # process_turn_fn was NOT called a second time
