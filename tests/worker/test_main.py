"""Standalone worker process entrypoint tests (CR-004 Phase 7 gap-close):
contact-stream discovery finds active streams by key prefix, and
`_build_redis_client` sets `decode_responses=True` (same live-caught
requirement as `app.main`'s).
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.config.settings import Settings
from app.worker.main import _build_redis_client, _discover_active_contact_ids


@pytest.mark.asyncio
async def test_discover_active_contact_ids_finds_streams() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.xadd("tap_agent:stream:contact:1", {"a": "1"})
    await redis.xadd("tap_agent:stream:contact:42", {"a": "1"})

    contact_ids = await _discover_active_contact_ids(redis)

    assert set(contact_ids) == {"1", "42"}


@pytest.mark.asyncio
async def test_discover_active_contact_ids_empty_when_no_streams() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    contact_ids = await _discover_active_contact_ids(redis)
    assert contact_ids == []


def test_worker_build_redis_client_sets_decode_responses_true() -> None:
    settings = Settings(redis_url="redis://localhost:6379", _env_file=None)
    client = _build_redis_client(settings)
    assert client.connection_pool.connection_kwargs.get("decode_responses") is True
