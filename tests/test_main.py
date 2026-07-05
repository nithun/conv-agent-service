"""Live-integration-pass finding (2026-07-05, Phase 7): `main.py`'s Redis
client MUST be constructed with `decode_responses=True` — every queue/
session module (app/queue/*, app/worker/session_store.py) is written
against str keys/values throughout. Without this flag, redis-py's async
client returns bytes by default, and every str-keyed dict lookup against a
REAL Redis server (not fakeredis's default-decoding FakeRedis, which every
other test in this repo used) silently KeyErrors. This was caught only by
running the actual Docker Compose stack against a real Redis container —
exactly the class of defect L-008/L-010 warn a green mocked suite can miss.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.config.settings import Settings
from app.core.state_machine import new_session
from app.main import _build_redis_client
from app.worker.session_store import RedisSessionStore


def test_build_redis_client_sets_decode_responses_true() -> None:
    settings = Settings(redis_url="redis://localhost:6379", _env_file=None)
    client = _build_redis_client(settings)
    # redis-py's connection pool carries the connection_kwargs including
    # decode_responses - inspect it directly rather than trusting a
    # constructor call was made correctly by convention alone.
    assert client.connection_pool.connection_kwargs.get("decode_responses") is True


@pytest.mark.asyncio
async def test_session_store_round_trip_fails_loudly_without_decode_responses() -> None:
    """Reproduces the exact live-caught failure mode: a Redis client
    WITHOUT decode_responses=True returns bytes keys, and
    RedisSessionStore's str-keyed field lookups (`fields["identity"]`)
    raise KeyError. This test exists so the failure mode is documented and
    detectable in the test suite, not only discoverable by running the
    real Docker stack.
    """
    non_decoding_redis = fakeredis.aioredis.FakeRedis(decode_responses=False)
    settings = Settings(_env_file=None)
    store = RedisSessionStore(non_decoding_redis, settings)

    session = new_session("contact-bytes", "919999999999", needs_identity_confirmation=False)
    await store.save_session(session)

    with pytest.raises(KeyError):
        await store.load_session("contact-bytes")


@pytest.mark.asyncio
async def test_session_store_round_trip_works_with_decode_responses_true() -> None:
    """The same round-trip succeeds once decode_responses=True is set —
    confirming main.py's `_build_redis_client` fix is both necessary and
    sufficient for this failure mode.
    """
    decoding_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = Settings(_env_file=None)
    store = RedisSessionStore(decoding_redis, settings)

    session = new_session("contact-str", "919999999999", needs_identity_confirmation=False)
    await store.save_session(session)

    loaded = await store.load_session("contact-str")
    assert loaded is not None
    assert loaded.glific_contact_id == "contact-str"
