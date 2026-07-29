"""Tests for app/worker/heartbeat.py (L-025 fix for F-083's silent-worker-
death gap) — the shared write/read contract `/health/ready`
(tests/ingress/test_readiness.py) and the docker healthcheck
(tests/worker/test_healthcheck.py) both build on.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.worker.heartbeat import (
    HEARTBEAT_KEY,
    HEARTBEAT_TTL_SECONDS,
    heartbeat_status,
    write_heartbeat,
)


@pytest.mark.asyncio
async def test_write_heartbeat_sets_key_with_ttl() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await write_heartbeat(redis, now=1_000_000.0)

    value = await redis.get(HEARTBEAT_KEY)
    assert value == "1000000"
    ttl = await redis.ttl(HEARTBEAT_KEY)
    assert 0 < ttl <= HEARTBEAT_TTL_SECONDS


@pytest.mark.asyncio
async def test_heartbeat_status_fresh_immediately_after_write() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await write_heartbeat(redis, now=1_000_000.0)

    status = await heartbeat_status(redis, now=1_000_000.0)

    assert status.fresh is True
    assert status.age_seconds == 0.0
    assert status.reason is None


@pytest.mark.asyncio
async def test_heartbeat_status_missing_when_never_written() -> None:
    """Reproduces F-083's exact starting condition: the worker never wrote
    a heartbeat at all (never started, or died before its first write).
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    status = await heartbeat_status(redis)

    assert status.fresh is False
    assert status.age_seconds is None
    assert status.reason == "heartbeat key missing"


@pytest.mark.asyncio
async def test_heartbeat_status_stale_past_threshold() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await write_heartbeat(redis, now=1_000_000.0)

    status = await heartbeat_status(redis, now=1_000_000.0 + HEARTBEAT_TTL_SECONDS + 1)

    assert status.fresh is False
    assert status.age_seconds is not None
    assert status.age_seconds > HEARTBEAT_TTL_SECONDS
    assert status.reason is not None
    assert "stale" in status.reason


@pytest.mark.asyncio
async def test_write_heartbeat_refreshes_ttl_on_each_call() -> None:
    """Mirrors the session store's touch()-on-write TTL-refresh semantics
    (app/worker/session_store.py) — each loop iteration's write must reset
    the clock, not just the first one.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await write_heartbeat(redis, now=1_000_000.0)
    await write_heartbeat(redis, now=1_000_010.0)

    value = await redis.get(HEARTBEAT_KEY)
    assert value == "1000010"
