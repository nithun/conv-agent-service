"""Worker liveness heartbeat (L-025 fix for F-083's silent-worker-death
gap, `docs/planning/BC-C-liveness-alert-tasks.md` task 2.1).

`app` (the FastAPI ingress process, `app/main.py`) and `worker`
(`app/worker/main.py`'s standalone async loop) are SEPARATE OS processes —
`app` cannot inspect `worker` directly (see `app/worker/main.py`'s module
docstring). This module is the shared contract both sides use instead:
`worker` writes a Redis key once per loop iteration (`write_heartbeat`,
called from `run_worker_loop`); `app`'s `/health/ready` route
(`app/ingress/readiness.py`) and the docker-compose `worker` healthcheck
(`app/worker/healthcheck.py`) both read it back (`heartbeat_status`) to
decide whether the worker is actually alive and draining, not merely
"process up".

Before this existed, F-083 was invisible: the worker container died while
`app`'s dependency-free `/health` (`app/ingress/health.py`) kept returning
200, silently dropping every turn reply and distress escalation — nothing
surfaced the gap except a human eyeballing `docker compose ps`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio import Redis

# Single Redis key both processes agree on, namespaced like every other key
# in this service ("tap_agent:" prefix — see STREAM_KEY_PREFIX/
# DEDUPE_KEY_PREFIX in app/queue/settings.py, _SESSION_KEY_PREFIX in
# app/worker/session_store.py).
HEARTBEAT_KEY = "tap_agent:worker:heartbeat"

# TTL / staleness bound, in seconds. TUNABLE — flagged as a follow-up
# (see the handoff this module shipped with) rather than derived from a
# measured production iteration time, which doesn't exist yet at this
# trial's scale.
#
# `app/worker/main.py`'s loop sleeps POLL_INTERVAL_SECONDS (1.0s) between
# iterations, plus whatever time draining every active contact's stream +
# the escalation stream takes that pass (sequential, KEYS-scan discovery —
# see `run_worker_loop`'s own docstring on why that trade is accepted at
# this trial's scale). 15s is a deliberately generous ~3x multiple over the
# steady-state ~1-2s cadence this trial actually sees, so one slow
# iteration (a slow Frappe/Glific/LLM call inside one drain) doesn't
# false-positive the whole worker as down, while still catching a truly
# dead/hung process within one grace window of a forced-worker-down drill
# (Phase 4 of `docs/planning/BC-C-liveness-alert-tasks.md`).
HEARTBEAT_TTL_SECONDS = 15


@dataclass(frozen=True)
class HeartbeatStatus:
    """Result of a freshness check against `HEARTBEAT_KEY`."""

    fresh: bool
    age_seconds: float | None
    reason: str | None


async def write_heartbeat(redis: Redis, *, now: float | None = None) -> None:
    """Writes the current epoch second to `HEARTBEAT_KEY` with a TTL of
    `HEARTBEAT_TTL_SECONDS`. Called once per `run_worker_loop` iteration
    (`app/worker/main.py`), AFTER that iteration's drain work completes —
    so a hung drain (process alive, but not actually processing) also lets
    the key go stale, not only a crashed/killed process.
    """
    epoch = now if now is not None else time.time()
    await redis.set(HEARTBEAT_KEY, str(int(epoch)), ex=HEARTBEAT_TTL_SECONDS)


async def heartbeat_status(redis: Redis, *, now: float | None = None) -> HeartbeatStatus:
    """Reads `HEARTBEAT_KEY` back and reports whether it is present and
    fresh. Two independent signals collapse to the same "down" verdict:
    Redis's own TTL eviction (the key is simply absent once
    `HEARTBEAT_TTL_SECONDS` elapses with no refresh) and an explicit
    age-vs-threshold comparison against the stored epoch value (belt and
    suspenders — catches a key that somehow survives past its intended
    freshness window, e.g. clock skew).
    """
    value = await redis.get(HEARTBEAT_KEY)
    if value is None:
        return HeartbeatStatus(fresh=False, age_seconds=None, reason="heartbeat key missing")

    epoch = now if now is not None else time.time()
    age_seconds = max(0.0, epoch - int(value))
    if age_seconds > HEARTBEAT_TTL_SECONDS:
        return HeartbeatStatus(
            fresh=False,
            age_seconds=age_seconds,
            reason=(
                f"heartbeat stale ({age_seconds:.1f}s old, "
                f"threshold {HEARTBEAT_TTL_SECONDS}s)"
            ),
        )
    return HeartbeatStatus(fresh=True, age_seconds=age_seconds, reason=None)
