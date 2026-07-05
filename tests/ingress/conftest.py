"""Shared fixtures for ingress-layer tests: an httpx AsyncClient against the
real FastAPI `app` object, with `app.state.redis` swapped for fakeredis so
tests never need a live Redis, per tests/ingress' async-stack convention
(tap-agent-service/CLAUDE.md's Test layout section).

`client` (the plain fixture, most tests) leaves `distress_handler` unset —
sufficient for tests that only need to confirm a distress-hit message is
NOT enqueued. `client_with_distress_handler` wires the REAL
`app.worker.distress_gate.handle_distress` against a fake Glific client and
a real `RedisSessionStore` (backed by fakeredis) — this is what Task 8's
e2e-through-real-ingress acceptance criterion (AC-9) needs: the distress
gate reachable from the actual `/webhook/glific` route, not a direct call
to `handle_distress`.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

import fakeredis.aioredis
import httpx
import pytest

from app.config.settings import Settings
from app.ingress.glific_payload import GlificWebhookPayload
from app.main import app
from app.worker.distress_gate import handle_distress
from app.worker.session_store import RedisSessionStore


@pytest.fixture
async def client() -> AsyncGenerator[httpx.AsyncClient, None]:
    app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.distress_handler = None
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


@pytest.fixture
async def client_with_distress_handler() -> AsyncGenerator[
    tuple[httpx.AsyncClient, FakeGlificSender], None
]:
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    fake_glific = FakeGlificSender()
    settings = Settings(_env_file=None)
    session_store = RedisSessionStore(fake_redis, settings)

    app.state.redis = fake_redis
    app.state.session_store = session_store

    async def _distress_handler(payload: GlificWebhookPayload) -> None:
        await handle_distress(payload, session_store=session_store, glific_client=fake_glific)

    app.state.distress_handler = _distress_handler

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, fake_glific
