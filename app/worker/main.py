"""Standalone worker process entrypoint (CR-004 Phase 7 gap-close,
2026-07-05): a long-running async process hosting the consumer-loop
machinery (`app/worker/consumer_loop.py`) against real Redis/TAP LMS/
Glific/ModelRouter clients — the "arq worker process" CR-004's own text
refers to when it says live-integration evidence must be captured "from
the arq worker process context," distinct from tap-buddy's single FastAPI
process (L-009: the new stack's networking/session context differs and
must be verified, not assumed).

Also drains the escalation stream (`app/queue/escalation_queue.py`,
`app/worker/escalation_consumer.py` — code-review fix, 2026-07-05, Finding
1): the ingress distress gate now only enqueues; this loop is what actually
performs the Glific send + Ticket-open for a distress hit, off the pre-ack
path.

This is NOT built on `arq.Worker`'s own job-queue model — this service's
ordering guarantee (R-3) comes from per-contact Redis Streams + consumer
groups (`app/queue/consumer.py`), which is a different mechanism than
arq's job queue. Running literally as `arq.Worker` jobs would mean two
competing queue mechanisms; this process is a plain asyncio loop that
discovers active contact streams and drains each one via the SAME
`process_one_entry`/`drain_contact_stream_once` primitives already tested
in `tests/worker/test_consumer_loop.py` — the retry/backoff/DLQ semantics
are identical either way.

Contact-stream discovery here uses a `KEYS` scan, acceptable at this
trial's scale (per docs/architecture.md's stated ~500k-student ceiling,
not exercised at anywhere near that volume yet) — a production-scale
version would maintain an explicit active-contacts registry set instead of
scanning all keys; that is a future optimization, not required for this
port to be correct.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis

from app.config.settings import Settings, get_settings
from app.delivery.glific_client import GlificClient
from app.ingress.glific_payload import GlificWebhookPayload
from app.llm.router import build_default_model_router
from app.queue.settings import STREAM_KEY_PREFIX
from app.tools.lms_client import TapLmsClient
from app.worker.consumer_loop import drain_contact_stream_once
from app.worker.escalation_consumer import drain_escalation_stream_once
from app.worker.session_store import RedisSessionStore
from app.worker.turn_processor import (
    TurnProcessingResult,
    maybe_rearm_turn_flow,
    process_turn,
)

logger = logging.getLogger("app.worker.main")

POLL_INTERVAL_SECONDS = 1.0
CONSUMER_NAME = "tap_agent_worker"
ESCALATION_CONSUMER_NAME = "tap_agent_escalation_worker"


async def _discover_active_contact_ids(redis: Redis) -> list[str]:
    keys: list[str] = await redis.keys(f"{STREAM_KEY_PREFIX}*")
    prefix_len = len(STREAM_KEY_PREFIX)
    return [key[prefix_len:] for key in keys]


def build_process_turn_fn(
    *,
    session_store: RedisSessionStore,
    lms_client: Any,
    model_router: Any,
    glific_client: GlificClient,
    turn_flow_id: str,
) -> Callable[[GlificWebhookPayload], Awaitable[TurnProcessingResult]]:
    """Composes the worker's per-entry turn function: process the turn, then
    re-arm the turn-relay flow (`maybe_rearm_turn_flow`) so the contact's
    NEXT message reaches this service at all. Module-level (not inline in
    `run_worker_loop`) so the re-arm call site itself is unit-testable —
    the CR-004 port originally shipped the setting and the client method
    with no call site, caught live at F-019 cutover (L-011's exact
    unwired-control failure class).
    """

    async def process_turn_fn(payload: GlificWebhookPayload) -> TurnProcessingResult:
        # Re-arm in `finally`, not only on success (code-review finding 1,
        # 2026-07-16): tap-buddy's `_process_turn` swallows every exception,
        # so its re-arm ALWAYS fires afterward. Here the exception must keep
        # propagating — the consumer loop's retry/DLQ semantics depend on it
        # — but a failing turn still must not strand the contact with no
        # armed relay flow (the same no-next-message symptom F-019 surfaced,
        # reached via a processing error instead of a missing call site).
        # `maybe_rearm_turn_flow` itself never raises, so the original
        # exception is never masked. Re-arming on an attempt that will be
        # retried is harmless: the relay flow just parks at its wait node,
        # and a later re-arm replaces it.
        result: TurnProcessingResult | None = None
        try:
            result = await process_turn(
                payload,
                session_store=session_store,
                lms_client=lms_client,
                model_router=model_router,
                glific_client=glific_client,
            )
            return result
        finally:
            await maybe_rearm_turn_flow(
                result if result is not None else TurnProcessingResult(handled=False),
                payload,
                turn_flow_id=turn_flow_id,
                glific_client=glific_client,
            )

    return process_turn_fn


def _build_redis_client(settings: Settings) -> Redis:
    # Mirrors app/main.py::_build_redis_client exactly (decode_responses=True
    # is required — see that function's docstring for the live-caught
    # failure mode this guards against). Duplicated rather than imported
    # from app.main to avoid this worker process depending on the FastAPI
    # app module (and its router/lifespan wiring) just for one helper.
    import redis.asyncio as redis_asyncio

    client: Redis = redis_asyncio.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url or "redis://localhost:6379", decode_responses=True
    )
    return client


async def run_worker_loop(*, max_iterations: int | None = None) -> None:
    """The long-running loop: discover active contact streams, drain one
    pending entry per contact per pass, sleep, repeat. `max_iterations` is
    for tests/manual verification runs only (None = run forever, the real
    deployment shape).
    """
    settings = get_settings()
    redis = _build_redis_client(settings)
    session_store = RedisSessionStore(redis, settings)
    lms_client = TapLmsClient(
        settings.frappe_api_url,
        settings.tap_lms_api_key,
        frappe_token=settings.tap_lms_token or None,
    )
    glific_client = GlificClient(
        settings.glific_api_url,
        settings.glific_phone,
        settings.glific_password,
        host_header_override=settings.glific_host_header_override or None,
    )
    model_router = build_default_model_router(
        settings.openrouter_api_key,
        base_url=settings.openrouter_base_url or None,
    )

    process_turn_fn = build_process_turn_fn(
        session_store=session_store,
        lms_client=lms_client,
        model_router=model_router,
        glific_client=glific_client,
        turn_flow_id=settings.glific_turn_flow_id,
    )

    logger.info("worker loop starting (redis_url=%s)", settings.redis_url)
    iteration = 0
    try:
        while max_iterations is None or iteration < max_iterations:
            contact_ids = await _discover_active_contact_ids(redis)
            for contact_id in contact_ids:
                try:
                    await drain_contact_stream_once(
                        redis,
                        contact_id,
                        CONSUMER_NAME,
                        process_turn_fn=process_turn_fn,
                        block_ms=100,
                    )
                except Exception:
                    # A discovery-loop-level failure for one contact must
                    # never stop the loop from processing every OTHER
                    # contact (P-002) — process_one_entry already handles
                    # per-entry failures with retry/DLQ; this is a final
                    # backstop for anything that somehow escapes that.
                    logger.exception(
                        "unexpected failure draining contact_id=%s; continuing loop", contact_id
                    )

            try:
                await drain_escalation_stream_once(
                    redis,
                    ESCALATION_CONSUMER_NAME,
                    session_store=session_store,
                    glific_client=glific_client,
                    block_ms=100,
                )
            except Exception:
                # Same backstop reasoning as the per-contact loop above —
                # process_one_escalation_entry already handles per-entry
                # failures; this is the final safety net so a discovery-loop
                # level exception here never stops turn processing for
                # every contact.
                logger.exception("unexpected failure draining escalation stream; continuing loop")

            iteration += 1
            if max_iterations is None or iteration < max_iterations:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
    finally:
        await lms_client.aclose()
        await glific_client.aclose()
        await redis.aclose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker_loop())
