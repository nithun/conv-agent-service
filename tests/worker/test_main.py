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


@pytest.mark.asyncio
async def test_run_worker_loop_drains_the_escalation_stream_each_iteration(monkeypatch) -> None:
    """Code-review fix (2026-07-05, Finding 1): the standalone worker loop
    must actually call `drain_escalation_stream_once` — this is what
    performs the real Glific-facing side effects the ingress route now
    only enqueues for. Monkeypatches the module's client constructors (the
    real ones need live Frappe/Glific/OpenRouter credentials, out of scope
    for this unit-level wiring check) and asserts the escalation drain is
    invoked with the shared (non-per-contact) consumer name.
    """
    import app.worker.main as worker_main_module

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(worker_main_module, "_build_redis_client", lambda settings: fake_redis)

    class _NoopLmsClient:
        async def aclose(self) -> None:
            return None

    class _NoopGlificClient:
        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        worker_main_module, "TapLmsClient", lambda *a, **kw: _NoopLmsClient()
    )
    monkeypatch.setattr(
        worker_main_module, "GlificClient", lambda *a, **kw: _NoopGlificClient()
    )
    monkeypatch.setattr(
        worker_main_module, "build_default_model_router", lambda *a, **kw: object()
    )

    drain_calls: list[tuple[object, str]] = []

    async def _fake_drain_escalation_stream_once(redis, consumer_name, **kwargs):
        drain_calls.append((redis, consumer_name))
        return None

    monkeypatch.setattr(
        worker_main_module,
        "drain_escalation_stream_once",
        _fake_drain_escalation_stream_once,
    )

    await worker_main_module.run_worker_loop(max_iterations=1)

    assert len(drain_calls) == 1
    assert drain_calls[0][0] is fake_redis
    assert drain_calls[0][1] == worker_main_module.ESCALATION_CONSUMER_NAME
