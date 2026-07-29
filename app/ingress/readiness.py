"""Worker+broker readiness probe (L-025 fix for F-083's silent-worker-death
gap, `docs/planning/BC-C-liveness-alert-tasks.md` task 2.1).

Distinct from `/health` (`app/ingress/health.py`) — that route stays a
dependency-free liveness probe, UNCHANGED (compose bring-up ordering
depends on it staying fast, dependency-free, and always-200-if-the-process
-is-up). This route DOES touch Redis, on purpose: it exists specifically
to catch the "app process is up, worker is silently dead/hung" failure
mode `/health` cannot see — F-083's exact live-caught manifestation, where
the worker container died while `/health` kept returning 200 and every
turn reply + distress escalation was silently dropped.

Never wired into the Glific webhook fast-ack path (L-002 unaffected) —
this is a separate, ops-only GET route with no ingress caller; the
existing `/webhook/glific` route is untouched, byte-for-byte.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.worker.heartbeat import heartbeat_status

router = APIRouter()


@router.get("/health/ready")
async def health_ready(request: Request) -> JSONResponse:
    redis: Redis = request.app.state.redis

    redis_ok = False
    redis_detail: str | None = None
    try:
        redis_ok = bool(await redis.ping())
    except (RedisError, OSError) as exc:
        redis_detail = f"redis unreachable: {exc}"

    worker_ok = False
    worker_detail: str | None = None
    if redis_ok:
        try:
            status = await heartbeat_status(redis)
        except (RedisError, OSError) as exc:
            worker_detail = f"redis error reading heartbeat: {exc}"
        else:
            worker_ok = status.fresh
            worker_detail = None if status.fresh else status.reason
    else:
        worker_detail = "skipped: redis unreachable"

    healthy = redis_ok and worker_ok
    body: dict[str, Any] = {
        "status": "ok" if healthy else "unhealthy",
        "redis": {"ok": redis_ok, "detail": redis_detail},
        "worker": {"ok": worker_ok, "detail": worker_detail},
    }
    return JSONResponse(content=body, status_code=200 if healthy else 503)
