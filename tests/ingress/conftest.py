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

CR-009 (2026-07-17): `/webhook/glific` now requires a valid
`X-Glific-Signature` header on every request (see
`app/ingress/webhook_auth.py`). `httpx.AsyncClient` normally never runs
FastAPI's `lifespan` (it isn't triggered by `ASGITransport`), so
`app.state.settings`/`app.state.glific_webhook_secret` — which the new
dependency reads — are set directly here, same discipline as the existing
`app.state.redis`/`app.state.session_store` assignments below. `client`
and `client_with_escalation_worker` additionally install a `request` event
hook that auto-signs any POST to `/webhook/glific` with
`TEST_WEBHOOK_SECRET` UNLESS the test already supplied its own
`X-Glific-Signature` header — this keeps every pre-existing test in this
package working unchanged (they exercise post-auth behavior, not auth
itself) while still leaving individual tests free to override the header
to exercise a rejection path. `raw_client` (auth-focused negative-path
tests, `tests/ingress/test_glific_webhook.py`) skips the auto-sign hook
entirely so a test can send NO header at all, a malformed one, or a
deliberately wrong one.

CR-010 (2026-07-25): `/webhook/glific` also requires the request's parsed
`contact_id`/`phone` to be on `app.state.contact_allowlist` (see
`app/ingress/contact_allowlist.py`). `client`, `raw_client`, and
`client_with_escalation_worker` all wire a default
`app.state.contact_allowlist` covering the exact `contact_id` literals
every PRE-EXISTING test in this package already posts (mirroring how
CR-009 wired `app.state.glific_webhook_secret` into the same fixtures) —
so every test written before this CR keeps exercising post-allowlist
behavior unchanged. CR-010's OWN adversarial/rejection tests use fresh
contact ids deliberately absent from this set (e.g. `"999999"`), or
override `app.state.contact_allowlist` directly after obtaining a client.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Protocol

import fakeredis.aioredis
import httpx
import pytest

from app.config.settings import Settings
from app.ingress.contact_allowlist import ContactAllowlist
from app.main import app
from app.worker.escalation_consumer import drain_escalation_stream_once
from app.worker.session_store import RedisSessionStore
from tests.ingress._webhook_signing import sign_webhook_body

# CR-009: fixed test secret for every ingress test that goes through the
# real `/webhook/glific` route via `client`/`client_with_escalation_worker`/
# `raw_client` below — NOT the published Glific dummy secret (deliberately
# distinct, so a test never accidentally "passes" against the wrong
# fallback constant).
TEST_WEBHOOK_SECRET = "cr-009-test-secret-do-not-use-in-any-real-env"

# CR-010: the exact set of `contact_id` literals every PRE-EXISTING test in
# `tests/ingress/` posts through `client`/`raw_client`/
# `client_with_escalation_worker` (grepped this session) — a closed,
# test-only set, mirroring TEST_WEBHOOK_SECRET's own "fixed constant, never
# a real value" discipline. CR-010's own new tests use fresh contact ids
# deliberately absent from this set (e.g. `"999999"`) to exercise
# rejection, or override `app.state.contact_allowlist` after obtaining a
# client.
FIXTURE_ALLOWLISTED_CONTACT_IDS = frozenset(
    {"1", "2", "3", "4", "5", "6", "7", "8", "10", "11", "12", "13", "14", "30", "40", "41"}
    | {str(n) for n in range(60, 68)}
    | {f"load-{i}" for i in range(50)}
)

# CR-010 AC-4's phone-variant fixture: a phone token distinct from any
# contact_id above, allowlisted via the `phone` field of
# `ContactAllowlist.is_allowed` rather than `contact_id`.
FIXTURE_ALLOWLISTED_PHONE = "919876500011"
FIXTURE_ALLOWLISTED_CONTACT_IDS = FIXTURE_ALLOWLISTED_CONTACT_IDS | {FIXTURE_ALLOWLISTED_PHONE}


async def _auto_sign_glific_webhook_requests(request: httpx.Request) -> None:
    """httpx request event hook: signs any POST to `/webhook/glific` with
    `TEST_WEBHOOK_SECRET` over the FINAL request body bytes (after httpx's
    own `json=`/`content=` serialization), unless the test already set its
    own `X-Glific-Signature` header.
    """
    if request.method == "POST" and request.url.path == "/webhook/glific":
        if "X-Glific-Signature" not in request.headers:
            request.headers["X-Glific-Signature"] = sign_webhook_body(
                TEST_WEBHOOK_SECRET, request.content
            )


def _install_webhook_auth_state(settings: Settings | None = None) -> None:
    app.state.settings = settings or Settings(
        environment="local", glific_webhook_secret=TEST_WEBHOOK_SECRET, _env_file=None
    )
    app.state.glific_webhook_secret = TEST_WEBHOOK_SECRET
    app.state.contact_allowlist = ContactAllowlist(tokens=FIXTURE_ALLOWLISTED_CONTACT_IDS)
    app.state.contact_allowlist_rejection_count = 0


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
    _install_webhook_auth_state()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        event_hooks={"request": [_auto_sign_glific_webhook_requests]},
    ) as c:
        yield c


@pytest.fixture
async def raw_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Like `client`, but WITHOUT the auto-sign event hook — for CR-009's
    negative-path/auth-focused tests that need full control over (or
    complete absence of) the `X-Glific-Signature` header.
    """
    app.state.redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    _install_webhook_auth_state()
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
    settings = Settings(
        environment="local", glific_webhook_secret=TEST_WEBHOOK_SECRET, _env_file=None
    )
    session_store = RedisSessionStore(fake_redis, settings)

    app.state.redis = fake_redis
    app.state.session_store = session_store
    _install_webhook_auth_state(settings)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        event_hooks={"request": [_auto_sign_glific_webhook_requests]},
    ) as c:
        yield _ClientWithEscalationWorker(client=c, fake_glific=fake_glific)
