"""Code-review fix (2026-07-05, Finding 1): the worker-side escalation
consumer performs the REAL Glific-facing side effects of a distress hit
(`GlificClient.send_text`, `open_ticket_for_escalation` ->
`GlificClient.create_ticket`) — off the pre-ack ingress path. Mirrors
tests/worker/test_consumer_loop.py's retry/dead-letter shape, applied to
the escalation stream's own consumer group.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import fakeredis.aioredis
import pytest

from app.config.settings import Settings
from app.core.state_machine import EscalationReason, SessionState
from app.queue.escalation_queue import enqueue_escalation, read_next_escalation_entry
from app.worker.escalation_consumer import (
    drain_escalation_stream_once,
    process_one_escalation_entry,
)
from app.worker.session_store import RedisSessionStore, session_key


@dataclass
class FakeGlificSender:
    sent_texts: list[tuple[str, str]] = field(default_factory=list)
    tickets: list[tuple[str, str, str]] = field(default_factory=list)

    async def send_text(self, receiver_id: str, body_text: str) -> dict[str, str]:
        self.sent_texts.append((receiver_id, body_text))
        return {"id": "fake-message-id"}

    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> dict[str, str]:
        self.tickets.append((contact_id, topic, body_text))
        return {"id": "fake-ticket-id"}


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def session_store(redis):
    settings = Settings(_env_file=None)
    return RedisSessionStore(redis, settings)


@pytest.mark.asyncio
async def test_drain_escalation_stream_once_processes_and_acks(redis, session_store) -> None:
    await enqueue_escalation(
        redis,
        {"contact_id": "50", "phone": "919999999999", "message_id": "esc-1", "text": "help me"},
    )
    glific = FakeGlificSender()

    result = await drain_escalation_stream_once(
        redis, "worker-1", session_store=session_store, glific_client=glific
    )

    assert result is True
    assert len(glific.sent_texts) == 1
    assert glific.sent_texts[0][0] == "50"
    assert len(glific.tickets) == 1

    session_fields = await redis.hgetall(session_key("50"))
    assert session_fields["state"] == SessionState.ESCALATED.value
    assert session_fields["escalation_reason"] == EscalationReason.DISTRESS.value

    # Acked — nothing left pending.
    next_entry = await read_next_escalation_entry(redis, "worker-1", block_ms=50)
    assert next_entry is None


@pytest.mark.asyncio
async def test_drain_escalation_stream_once_returns_none_when_empty(redis, session_store) -> None:
    glific = FakeGlificSender()
    result = await drain_escalation_stream_once(
        redis, "worker-1", session_store=session_store, glific_client=glific, block_ms=50
    )
    assert result is None


@pytest.mark.asyncio
async def test_simulated_failure_is_retried_then_permanently_logged_not_crashed(
    redis, session_store
) -> None:
    """A simulated downstream failure results in retry-then-give-up (acked
    after MAX_ESCALATION_DELIVERY_ATTEMPTS so the stream isn't blocked
    forever), never a crashed consumer loop — mirrors the P-002 pattern
    used by the turn consumer loop.
    """
    await enqueue_escalation(
        redis,
        {"contact_id": "51", "phone": "919999999999", "message_id": "esc-2", "text": "help"},
    )

    class FailingGlificSender:
        async def send_text(self, *a, **kw):
            raise RuntimeError("simulated Glific outage")

        async def create_ticket(self, *a, **kw):
            raise RuntimeError("simulated Glific outage")

    entry = await read_next_escalation_entry(redis, "worker-1", block_ms=100)
    assert entry is not None
    entry_id, fields = entry

    from app.worker.escalation_consumer import MAX_ESCALATION_DELIVERY_ATTEMPTS

    for attempt in range(1, MAX_ESCALATION_DELIVERY_ATTEMPTS + 1):
        result = await process_one_escalation_entry(
            redis,
            "worker-1",
            entry_id,
            fields,
            session_store=session_store,
            glific_client=FailingGlificSender(),
            attempt=attempt,
        )
        assert result is False  # never raises out to the caller

    # After exhausting attempts, the entry is acked (unblocks the stream)
    # even though it never succeeded — logged, not silently dropped.
    next_entry = await read_next_escalation_entry(redis, "worker-1", block_ms=50)
    assert next_entry is None


@pytest.mark.asyncio
async def test_failed_attempt_below_cap_is_not_acked(redis, session_store) -> None:
    await enqueue_escalation(
        redis,
        {"contact_id": "52", "phone": "919999999999", "message_id": "esc-3", "text": "help"},
    )

    class FailingGlificSender:
        async def send_text(self, *a, **kw):
            raise RuntimeError("boom")

        async def create_ticket(self, *a, **kw):
            raise RuntimeError("boom")

    entry = await read_next_escalation_entry(redis, "worker-1", block_ms=100)
    assert entry is not None
    entry_id, fields = entry

    await process_one_escalation_entry(
        redis,
        "worker-1",
        entry_id,
        fields,
        session_store=session_store,
        glific_client=FailingGlificSender(),
        attempt=1,
    )

    from app.queue.escalation_queue import ESCALATION_STREAM_KEY

    pending = await redis.xpending(ESCALATION_STREAM_KEY, "tap_agent_escalation_workers")
    assert pending["pending"] == 1
