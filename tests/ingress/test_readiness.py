"""Tests for /health/ready (L-025 fix for F-083's silent-worker-death gap,
`docs/planning/BC-C-liveness-alert-tasks.md` task 2.1).

Distinct from /health (tests/ingress/test_health.py, unchanged): this route
DOES touch Redis and reports the worker's own heartbeat freshness, proving
`app` can detect "process up, but worker silently dead/hung" instead of
returning a blanket 200 no matter what — F-083's exact live-caught failure
mode. Uses the `client` fixture from tests/ingress/conftest.py, which wires
`app.state.redis` to fakeredis (no live Redis needed).
"""

from __future__ import annotations

import time

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.ingress.readiness import router as readiness_router
from app.ingress.webhook_auth import verify_glific_webhook_signature
from app.main import app
from app.worker.heartbeat import HEARTBEAT_KEY, HEARTBEAT_TTL_SECONDS, write_heartbeat


def test_health_ready_route_has_no_webhook_signature_dependency() -> None:
    """Mirrors /health's own AC-8 negative-claim reachability check
    (tests/ingress/test_health.py) — /health/ready is an ops-only probe,
    never gated behind Glific's webhook signature.
    """
    route = next(r for r in readiness_router.routes if r.path == "/health/ready")  # type: ignore[attr-defined]
    dependency_callables = [dep.call for dep in route.dependant.dependencies]  # type: ignore[attr-defined]

    assert verify_glific_webhook_signature not in dependency_callables


@pytest.mark.asyncio
async def test_health_ready_returns_200_when_worker_heartbeat_fresh_and_redis_ok(client) -> None:
    await write_heartbeat(app.state.redis)

    response = await client.get("/health/ready")

    body = response.json()
    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["redis"]["ok"] is True
    assert body["worker"]["ok"] is True


@pytest.mark.asyncio
async def test_health_ready_returns_503_when_heartbeat_missing(client) -> None:
    # No write_heartbeat call at all — reproduces F-083 exactly: the worker
    # process never started (or died before writing its first heartbeat).
    response = await client.get("/health/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "unhealthy"
    assert body["redis"]["ok"] is True
    assert body["worker"]["ok"] is False
    assert "missing" in (body["worker"]["detail"] or "")


@pytest.mark.asyncio
async def test_health_ready_returns_503_when_heartbeat_stale(client) -> None:
    # Writes the key directly (bypassing write_heartbeat's own TTL) with an
    # epoch far in the past but a long Redis TTL, so what actually catches
    # the staleness is heartbeat_status's explicit age-vs-threshold
    # comparison (real-time, since the HTTP boundary can't inject a fake
    # `now`) — not Redis's own key eviction, which is exercised directly in
    # tests/worker/test_heartbeat.py.
    stale_epoch = int(time.time()) - HEARTBEAT_TTL_SECONDS - 5
    await app.state.redis.set(HEARTBEAT_KEY, str(stale_epoch), ex=HEARTBEAT_TTL_SECONDS * 10)

    response = await client.get("/health/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["worker"]["ok"] is False
    assert "stale" in (body["worker"]["detail"] or "")


@pytest.mark.asyncio
async def test_health_ready_returns_503_when_redis_unreachable(client) -> None:
    """The other half of F-083's exact mode this task must also catch: not
    just a dead worker, but Redis itself being unreachable (the worker
    can't even write a heartbeat, and /health/ready can't read one either).
    """

    class _BrokenRedis:
        async def ping(self) -> bool:
            raise RedisConnectionError("connection refused")

    app.state.redis = _BrokenRedis()

    response = await client.get("/health/ready")

    body = response.json()
    assert response.status_code == 503
    assert body["status"] == "unhealthy"
    assert body["redis"]["ok"] is False
    assert body["worker"]["ok"] is False
