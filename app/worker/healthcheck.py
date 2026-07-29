"""Docker-compose healthcheck entrypoint for the `worker` service (L-025,
`docs/planning/BC-C-liveness-alert-tasks.md` task 2.2 — mirrors the `litellm`
service's healthcheck precedent in `docker-compose.yml`, F-078).

The worker image has no `curl`/`wget` (same reason `litellm`'s healthcheck
uses a python3 CMD rather than curl) but DOES have this repo's own
dependencies installed (including the `redis` client), so this reuses the
SAME `app.worker.heartbeat.heartbeat_status` freshness check `/health/ready`
(`app/ingress/readiness.py`) uses — one source of truth for "is the worker
actually alive and draining", not two independently-tuned thresholds.

Invoked by `docker-compose.yml` as `["python", "-m", "app.worker.healthcheck"]`;
exits 0 when the heartbeat is fresh, 1 otherwise, printing the reason to
stdout for a human reading `docker inspect`/`docker compose ps`. A HUNG
worker (process alive, stuck mid-drain, never reaching the heartbeat write
in `run_worker_loop`) shows unhealthy here exactly like a crashed one —
that is the point (F-083's silent-death gap covered "process alive but not
processing" too, not only a crashed process).
"""

from __future__ import annotations

import asyncio
import sys

from app.config.settings import get_settings
from app.worker.heartbeat import heartbeat_status
from app.worker.main import _build_redis_client


async def worker_is_healthy() -> tuple[bool, str]:
    """Builds a fresh Redis client from env-sourced settings (a new,
    short-lived connection per healthcheck invocation — this runs as a
    one-shot CMD, not a long-lived process) and reports the worker
    heartbeat's freshness.
    """
    settings = get_settings()
    redis = _build_redis_client(settings)
    try:
        status = await heartbeat_status(redis)
        return status.fresh, (status.reason or "heartbeat fresh")
    finally:
        await redis.aclose()


def main() -> int:
    healthy, detail = asyncio.run(worker_is_healthy())
    print(detail)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
