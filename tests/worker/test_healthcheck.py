"""Tests for the docker-compose `worker` healthcheck entrypoint
(app/worker/healthcheck.py, `docs/planning/BC-C-liveness-alert-tasks.md`
task 2.2) — the SAME heartbeat-freshness check `/health/ready`
(tests/ingress/test_readiness.py) uses, invoked as
`python -m app.worker.healthcheck` from docker-compose.yml's `worker`
service (mirrors litellm's python3 CMD healthcheck, F-078 precedent: this
image has no curl/wget).
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

import app.worker.healthcheck as healthcheck_module
from app.worker.healthcheck import main, worker_is_healthy
from app.worker.heartbeat import write_heartbeat


@pytest.mark.asyncio
async def test_worker_is_healthy_true_when_heartbeat_fresh(monkeypatch) -> None:
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await write_heartbeat(fake_redis)
    monkeypatch.setattr(healthcheck_module, "_build_redis_client", lambda settings: fake_redis)

    healthy, detail = await worker_is_healthy()

    assert healthy is True
    assert "fresh" in detail


@pytest.mark.asyncio
async def test_worker_is_healthy_false_when_heartbeat_missing(monkeypatch) -> None:
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(healthcheck_module, "_build_redis_client", lambda settings: fake_redis)

    healthy, detail = await worker_is_healthy()

    assert healthy is False
    assert "missing" in detail


def test_main_returns_exit_code_0_when_healthy(monkeypatch) -> None:
    async def _fake_worker_is_healthy() -> tuple[bool, str]:
        return True, "heartbeat fresh"

    monkeypatch.setattr(healthcheck_module, "worker_is_healthy", _fake_worker_is_healthy)

    assert main() == 0


def test_main_returns_exit_code_1_when_unhealthy(monkeypatch) -> None:
    async def _fake_worker_is_healthy() -> tuple[bool, str]:
        return False, "heartbeat key missing"

    monkeypatch.setattr(healthcheck_module, "worker_is_healthy", _fake_worker_is_healthy)

    assert main() == 1
