"""Code-review fix (2026-07-05, Finding 1 — L-002 regression): proves the
distress-escalation path specifically is resilient to LATENCY, not just
failure. `test_distress_handler_exception_does_not_500_the_webhook_response`
(now `test_escalation_enqueue_failure_does_not_500_the_webhook_response`,
tests/ingress/test_distress_gate_resilience.py) proves the webhook survives
a downstream EXCEPTION; this is a distinct property — a downstream call that
neither raises nor returns quickly (a hung/slow Glific call) must also never
hold the webhook open, because Redis Streams `enqueue_escalation` writes to
Redis and returns without ever touching Glific at all.

Before the fix, `glific_webhook.py` awaited `distress_handler(payload)`
directly, which called through to `GlificClient.send_text` and
`open_ticket_for_escalation` -> `GlificClient.create_ticket` — real network
calls with a 10s httpx timeout (`app/delivery/glific_client.py`). A
deliberately SLOW (not just failing) fake Glific client that sleeps 6+
seconds before returning proves the old code could hold the webhook open
past Glific's 4s flow-webhook timeout. This test asserts the webhook now
returns well within this repo's existing fast-ack budget (<500ms) even
though a slow fake Glific client is wired up — because that fake is never
awaited on the ingress path at all anymore; it would only be exercised by
the worker-side escalation consumer, off this path.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.worker.escalation_consumer import drain_escalation_stream_once


class _SlowFakeGlificSender:
    """Sleeps well past Glific's 4s webhook timeout before returning —
    proves resilience to LATENCY specifically, as distinct from the
    existing exception-resilience test.
    """

    def __init__(self, delay_seconds: float = 6.0) -> None:
        self.delay_seconds = delay_seconds
        self.sent_texts: list[tuple[str, str]] = []
        self.tickets: list[tuple[str, str, str]] = []

    async def send_text(self, receiver_id: str, body_text: str) -> dict[str, str]:
        await asyncio.sleep(self.delay_seconds)
        self.sent_texts.append((receiver_id, body_text))
        return {"id": "fake-message-id"}

    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> dict[str, str]:
        await asyncio.sleep(self.delay_seconds)
        self.tickets.append((contact_id, topic, body_text))
        return {"id": "fake-ticket-id"}


@pytest.mark.asyncio
async def test_distress_webhook_acks_fast_even_with_slow_downstream_glific_client(
    client_with_escalation_worker,
) -> None:
    """The webhook response itself must stay within budget regardless of
    how slow the downstream Glific client eventually turns out to be — the
    slow fake is wired onto the worker-side escalation consumer (never
    awaited on the ingress path), so this test asserts the STRUCTURAL fix:
    the webhook response time is independent of Glific's latency entirely.
    """
    client_with_escalation_worker.fake_glific = _SlowFakeGlificSender(delay_seconds=6.0)

    payload = {
        "contact_id": "40",
        "phone": "919999999999",
        "message_id": "slow-glific-1",
        "text": "I want to kill myself",
    }

    start = time.monotonic()
    response = await client_with_escalation_worker.client.post("/webhook/glific", json=payload)
    elapsed_ms = (time.monotonic() - start) * 1000

    assert response.status_code == 200
    assert response.json()["escalated"] is True
    assert elapsed_ms < 500, (
        f"distress webhook ack took {elapsed_ms:.1f}ms even though the slow Glific fake "
        f"sleeps 6s+ per call — fast-ack budget is 500ms; a regression here means the "
        f"Glific-facing call has crept back onto the pre-ack ingress path (L-002)"
    )


@pytest.mark.asyncio
async def test_slow_glific_fake_is_only_reached_by_the_worker_drain_not_the_webhook(
    client_with_escalation_worker,
) -> None:
    """Companion assertion: confirms the slow fake really is wired up and
    really would be slow if reached — draining the escalation stream
    (simulating the standalone worker process) takes >= the fake's delay,
    proving the previous test's fast webhook response isn't fast merely
    because the fake was never invoked by ANYTHING.
    """
    client_with_escalation_worker.fake_glific = _SlowFakeGlificSender(delay_seconds=0.3)

    payload = {
        "contact_id": "41",
        "phone": "919999999999",
        "message_id": "slow-glific-2",
        "text": "I want to kill myself",
    }
    response = await client_with_escalation_worker.client.post("/webhook/glific", json=payload)
    assert response.status_code == 200

    redis = client_with_escalation_worker.client._transport.app.state.redis
    session_store = client_with_escalation_worker.client._transport.app.state.session_store

    start = time.monotonic()
    drained = await drain_escalation_stream_once(
        redis,
        "test-slow-drain-consumer",
        session_store=session_store,
        glific_client=client_with_escalation_worker.fake_glific,
        block_ms=50,
    )
    elapsed = time.monotonic() - start

    assert drained is True
    assert elapsed >= 0.3
    assert len(client_with_escalation_worker.fake_glific.sent_texts) == 1
