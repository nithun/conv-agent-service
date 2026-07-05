"""Shared fixtures for ingress-layer tests: an httpx AsyncClient against the
real FastAPI `app` object, with `app.state.redis` swapped for fakeredis so
tests never need a live Redis, per tests/ingress' async-stack convention
(tap-agent-service/CLAUDE.md's Test layout section).

`client` (the plain fixture, most tests) is sufficient for tests that only
need to confirm a distress-hit message is enqueued onto the escalation
stream and NOT the normal turn stream. `client_with_escalation_worker`
wires a REAL `RedisSessionStore` (backed by fakeredis) and a fake Glific
client, plus a `drain_escalation()` helper that runs
`app.worker.escalation_consumer.drain_escalation_stream_once` — this is
what Task 8/AC-9's e2e-through-real-ingress acceptance criterion needs: the
distress gate reachable from the actual `/webhook/glific` route, with the
real Glific-facing side effects (send_text, create_ticket) verified via the
worker-side drain, not a direct call to `handle_distress` (code-review fix,
2026-07-05, Finding 1 — the ingress route itself now only enqueues, per
L-002; the fixture's `drain_escalation()` stands in for the standalone
worker process that performs the real send/Ticket-open asynchronously).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Protocol

import fakeredis.aioredis
import httpx
import pytest

from app.config.settings import Settings
from app.main import app
from app.worker.escalation_consumer import drain_escalation_stream_once
from app.worker.session_store import RedisSessionStore


class _GlificSenderLike(Protocol):
    """Structural type covering both `FakeGlificSender` and any test-local
    fake (e.g. the slow fake in test_distress_gate_latency.py) that a test
    may swap onto `_ClientWithEscalationWorker.fake_glific`.
    """

    async def send_text(self, receiver_id: str, body_text: str) -> object: ...
    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> object: ...


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@dataclass
class FakeGlificSender:
    """Records every send_text/create_ticket call — a fake, not a mock, so
    tests assert on real recorded calls rather than mock-framework
    call-count bookkeeping.
    """

    sent_texts: list[tuple[str, str]] = field(default_factory=list)
    tickets: list[tuple[str, str, str]] = field(default_factory=list)

    async def send_text(self, receiver_id: str, body_text: str) -> dict[str, str]:
        self.sent_texts.append((receiver_id, body_text))
        return {"id": "fake-message-id"}

    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> dict[str, str]:
        self.tickets.append((contact_id, topic, body_text))
        return {"id": "fake-ticket-id"}


@dataclass
class _ClientWithEscalationWorker:
    client: httpx.AsyncClient
    fake_glific: _GlificSenderLike

    async def drain_escalation(self) -> bool | None:
        """Runs exactly one pass of the escalation-stream consumer
        (mirrors what the standalone worker process, `app/worker/main.py`,
        does continuously) — call this after posting a distress-triggering
        payload to actually perform the Glific send/Ticket-open the ingress
        route deferred.
        """
        redis = self.client._transport.app.state.redis  # type: ignore[attr-defined]
        session_store = self.client._transport.app.state.session_store  # type: ignore[attr-defined]
        return await drain_escalation_stream_once(
            redis,
            "test-escalation-consumer",
            session_store=session_store,
            glific_client=self.fake_glific,
            block_ms=50,
        )

    def __iter__(self):
        # Preserves the old `client, fake_glific = client_with_distress_handler`
        # unpacking shape so existing tests need only the drain-step addition.
        return iter((self.client, self.fake_glific))


@pytest.fixture
async def client_with_escalation_worker() -> AsyncGenerator[_ClientWithEscalationWorker, None]:
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    fake_glific = FakeGlificSender()
    settings = Settings(_env_file=None)
    session_store = RedisSessionStore(fake_redis, settings)

    app.state.redis = fake_redis
    app.state.session_store = session_store

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield _ClientWithEscalationWorker(client=c, fake_glific=fake_glific)
