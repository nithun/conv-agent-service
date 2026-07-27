"""AC-2/Task 10: Redis session store — turn count, re-explanation counter,
and ASR-failure counter persist across the TTL window and correctly
expire at the TTL boundary. Not merely "a Redis write/read round-trips" —
the specific counter-persistence semantics tap-buddy relied on
(state_machine.SessionData's turn_count/reexplanation_count/
asr_failure_count fields).
"""

from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

from app.config.settings import Settings
from app.core.state_machine import (
    EscalationReason,
    SessionState,
    StudentIdentity,
    new_session,
)
from app.worker.session_store import RedisSessionStore, session_key


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def settings():
    return Settings(session_ttl_seconds=1800, _env_file=None)


@pytest.mark.asyncio
async def test_session_round_trips_basic_fields(redis, settings) -> None:
    store = RedisSessionStore(redis, settings)
    session = new_session("contact-1", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(
        student_id="STU-001", child_name="Aarav", grade="5", tap_language="Hindi"
    )

    await store.save_session(session)
    loaded = await store.load_session("contact-1")

    assert loaded is not None
    assert loaded.glific_contact_id == "contact-1"
    assert loaded.phone == "917834811114"
    assert loaded.state == SessionState.GREETING
    assert loaded.identity.student_id == "STU-001"
    assert loaded.identity.child_name == "Aarav"
    assert loaded.identity.tap_language == "Hindi"


@pytest.mark.asyncio
async def test_session_counters_persist_across_ttl_window(redis, settings) -> None:
    """AC-2 literal: turn_count/reexplanation_count/asr_failure_count
    persist correctly, not just "some value round-trips."
    """
    store = RedisSessionStore(redis, settings)
    session = new_session("contact-2", "917834811114", needs_identity_confirmation=False)
    session.turn_count = 7
    session.reexplanation_count = 2
    session.asr_failure_count = 1

    await store.save_session(session)
    loaded = await store.load_session("contact-2")

    assert loaded is not None
    assert loaded.turn_count == 7
    assert loaded.reexplanation_count == 2
    assert loaded.asr_failure_count == 1

    # Still within the TTL window — persists across a second read too.
    loaded_again = await store.load_session("contact-2")
    assert loaded_again is not None
    assert loaded_again.turn_count == 7


@pytest.mark.asyncio
async def test_save_session_refreshes_ttl_each_write(redis, settings) -> None:
    store = RedisSessionStore(redis, settings)
    session = new_session("contact-3", "917834811114", needs_identity_confirmation=False)

    await store.save_session(session)
    ttl_after_first_save = await redis.ttl(session_key("contact-3"))
    assert ttl_after_first_save > 0

    session.turn_count += 1
    await store.save_session(session)
    ttl_after_second_save = await redis.ttl(session_key("contact-3"))
    # Refreshed, not merely "still positive" — should be back near the full
    # configured TTL, not counting down from the first save.
    assert ttl_after_second_save >= ttl_after_first_save


@pytest.mark.asyncio
async def test_session_counters_expire_at_ttl_boundary(redis) -> None:
    """AC-2 literal: counters correctly EXPIRE at the TTL boundary — a
    short real TTL + a real sleep past it, confirming Redis eviction (not
    an app-level is_expired() check) is what actually removes the session.
    """
    short_ttl_settings = Settings(session_ttl_seconds=1, _env_file=None)
    store = RedisSessionStore(redis, short_ttl_settings)
    session = new_session("contact-4", "917834811114", needs_identity_confirmation=False)
    session.turn_count = 3

    await store.save_session(session)
    assert await store.load_session("contact-4") is not None

    await asyncio.sleep(1.2)

    assert await store.load_session("contact-4") is None


@pytest.mark.asyncio
async def test_load_missing_session_returns_none(redis, settings) -> None:
    store = RedisSessionStore(redis, settings)
    assert await store.load_session("never-seen-contact") is None


@pytest.mark.asyncio
async def test_escalation_reason_round_trips(redis, settings) -> None:
    store = RedisSessionStore(redis, settings)
    session = new_session("contact-5", "917834811114", needs_identity_confirmation=False)
    session.state = SessionState.ESCALATED
    session.escalation_reason = EscalationReason.DISTRESS

    await store.save_session(session)
    loaded = await store.load_session("contact-5")

    assert loaded is not None
    assert loaded.state == SessionState.ESCALATED
    assert loaded.escalation_reason == EscalationReason.DISTRESS


@pytest.mark.asyncio
async def test_content_reference_round_trips_as_dict(redis, settings) -> None:
    store = RedisSessionStore(redis, settings)
    session = new_session("contact-6", "917834811114", needs_identity_confirmation=False)
    session.content_reference = {
        "content_id": "CONTENT-101",
        "title": "Creative problem-solving",
        "content_type": "video",
    }

    await store.save_session(session)
    loaded = await store.load_session("contact-6")

    assert loaded is not None
    assert loaded.content_reference == session.content_reference


@pytest.mark.asyncio
async def test_delete_session_removes_it(redis, settings) -> None:
    store = RedisSessionStore(redis, settings)
    session = new_session("contact-7", "917834811114", needs_identity_confirmation=False)
    await store.save_session(session)
    assert await store.load_session("contact-7") is not None

    await store.delete_session("contact-7")
    assert await store.load_session("contact-7") is None


@pytest.mark.asyncio
async def test_session_key_is_derived_from_glific_contact_id() -> None:
    """AC-7/R-7 (part 1): the Redis key itself is glific_contact_id-keyed,
    not an independently-derived identity.
    """
    assert session_key("42") == "tap_agent:session:42"


# ---- CR-007: language_choice_attempts field ---------------------------


@pytest.mark.asyncio
async def test_language_choice_attempts_round_trips(redis, settings) -> None:
    store = RedisSessionStore(redis, settings)
    session = new_session("contact-8", "917834811114", needs_identity_confirmation=True)
    session.state = SessionState.LANGUAGE_CHOICE
    session.language_choice_attempts = 1

    await store.save_session(session)
    loaded = await store.load_session("contact-8")

    assert loaded is not None
    assert loaded.state == SessionState.LANGUAGE_CHOICE
    assert loaded.language_choice_attempts == 1


@pytest.mark.asyncio
async def test_missing_language_choice_attempts_hash_field_deserializes_as_zero(
    redis, settings
) -> None:
    """Forward-only migration (CR-007 Migration section): a Redis hash
    written by pre-CR-007 code has no `language_choice_attempts` field at
    all — HGETALL simply omits it, and the deserializer must default-
    tolerate the missing key rather than KeyError.
    """
    store = RedisSessionStore(redis, settings)
    session = new_session("contact-9", "917834811114", needs_identity_confirmation=False)
    await store.save_session(session)

    # Simulate a pre-CR-007 hash by deleting the field after the fact.
    await redis.hdel(session_key("contact-9"), "language_choice_attempts")

    loaded = await store.load_session("contact-9")
    assert loaded is not None
    assert loaded.language_choice_attempts == 0


@pytest.mark.asyncio
async def test_asr_escalation_ticket_message_id_round_trips(redis, settings) -> None:
    """F-039: the ASR-escalation ticket idempotency marker persists across
    a save/load cycle, the same shape as `language_choice_last_message_id`.
    """
    store = RedisSessionStore(redis, settings)
    session = new_session("contact-10", "917834811114", needs_identity_confirmation=False)
    session.asr_escalation_ticket_message_id = "m2"

    await store.save_session(session)
    loaded = await store.load_session("contact-10")

    assert loaded is not None
    assert loaded.asr_escalation_ticket_message_id == "m2"


@pytest.mark.asyncio
async def test_missing_asr_escalation_ticket_message_id_hash_field_deserializes_as_none(
    redis, settings
) -> None:
    """Forward-only migration: a Redis hash written before F-039 has no
    `asr_escalation_ticket_message_id` field at all — HGETALL simply omits
    it, and the deserializer must default-tolerate the missing key (as "no
    Ticket opened yet for this session") rather than KeyError.
    """
    store = RedisSessionStore(redis, settings)
    session = new_session("contact-11", "917834811114", needs_identity_confirmation=False)
    await store.save_session(session)

    await redis.hdel(session_key("contact-11"), "asr_escalation_ticket_message_id")

    loaded = await store.load_session("contact-11")
    assert loaded is not None
    assert loaded.asr_escalation_ticket_message_id is None
