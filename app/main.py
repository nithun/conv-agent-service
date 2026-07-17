"""FastAPI application entrypoint.

Wires `app.state` with the Redis client, LMS client, Glific client, and
ModelRouter — all constructed once at startup (lifespan), injected into
request handlers via `request.app.state`. Mirrors tap-buddy's `main.py`
assembly pattern, re-platformed onto this service's target infra (Redis
Streams/arq instead of an asyncio background task, ModelRouter instead of a
direct Vertex client) per CR-004.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as redis_asyncio
from fastapi import FastAPI
from redis.asyncio import Redis

from app.config.settings import Settings, get_settings
from app.delivery.glific_client import GlificClient
from app.ingress.glific_webhook import router as webhook_router
from app.ingress.health import router as health_router
from app.ingress.media import router as media_router
from app.ingress.webhook_auth import resolve_glific_webhook_secret
from app.llm.router import build_default_model_router
from app.tools.lms_client import TapLmsClient
from app.worker.session_store import RedisSessionStore


def _build_lms_client(settings: Settings) -> TapLmsClient:
    return TapLmsClient(
        settings.frappe_api_url,
        settings.tap_lms_api_key,
        frappe_token=settings.tap_lms_token or None,
    )


def _build_redis_client(settings: Settings) -> Redis:
    # redis-py's `from_url` has no type stubs (untyped helper) — the
    # explicit Redis return annotation here is the actual contract callers
    # rely on, not from_url's own (missing) signature.
    #
    # decode_responses=True is REQUIRED (live-integration-pass finding,
    # 2026-07-05): every queue/session module (app/queue/*, app/worker/
    # session_store.py) is written against str keys/values throughout
    # (e.g. RedisSessionStore._deserialize_session's `fields["identity"]`).
    # Without this flag, redis-py's async client returns bytes by default,
    # and every str-keyed dict lookup against a real Redis server silently
    # KeyErrors — every test in this repo used
    # fakeredis.aioredis.FakeRedis(decode_responses=True), which masked
    # this until a live Redis instance (this Docker Compose stack) was
    # actually exercised. Confirmed live: unit/e2e tests were green
    # throughout; only running against a REAL Redis server surfaced this
    # (L-008/L-010's exact point — mocked-external tests are not
    # sufficient evidence).
    client: Redis = redis_asyncio.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url or "redis://localhost:6379", decode_responses=True
    )
    return client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings

    # CR-009 (webhook auth, 2026-07-17): resolve — or fail-closed reject —
    # the Glific webhook signature secret BEFORE constructing any client.
    # An unkeyable verification config must never let the process come up
    # looking healthy (architecture-reviewer ENDORSED this boot-time-failure
    # behavior; AC-2). See app/ingress/webhook_auth.py's
    # resolve_glific_webhook_secret docstring for the fail-closed-by-
    # environment rule.
    app.state.glific_webhook_secret = resolve_glific_webhook_secret(settings)

    app.state.redis = _build_redis_client(settings)
    app.state.lms_client = _build_lms_client(settings)
    app.state.model_router = build_default_model_router(
        settings.openrouter_api_key,
        base_url=settings.openrouter_base_url or None,
    )
    app.state.glific_client = GlificClient(
        settings.glific_api_url,
        settings.glific_phone,
        settings.glific_password,
        host_header_override=settings.glific_host_header_override or None,
    )
    app.state.session_store = RedisSessionStore(app.state.redis, settings)

    # Top-level distress gate (BR-003/Invariant 4/AC-9): the webhook route
    # (`app/ingress/glific_webhook.py`) enqueues onto the dedicated
    # escalation stream (`app/queue/escalation_queue.py`) directly, before
    # the turn enqueue, so it is reachable e2e from the real webhook route
    # for every early state (identity_confirmation, greeting, mode_choice),
    # not just unit-tested in isolation (L-008). The actual Glific-facing
    # side effects (`handle_distress`) run only in the worker process
    # (`app/worker/escalation_consumer.py`), never inline here — code-review
    # fix, 2026-07-05, Finding 1 (L-002 regression: those are real network
    # calls up to GlificClient's 10s httpx timeout, which must never block
    # the fast-ack response).

    yield

    await app.state.lms_client.aclose()
    await app.state.glific_client.aclose()
    await app.state.redis.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(webhook_router)
    app.include_router(media_router)
    return app


app = create_app()
