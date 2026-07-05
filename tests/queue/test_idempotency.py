"""CR-004 Edge case / Task 6: a retried arq job after partial completion
(e.g. save_submission succeeded, then the worker crashed before sending the
reply) must not double-send the outbound reply. Tap-buddy has no retry
semantics to port from — this is new behavior, tested fresh.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.queue.idempotency import mark_reply_sent_if_not_already


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_first_send_attempt_is_allowed(redis) -> None:
    should_send = await mark_reply_sent_if_not_already(redis, "contact-1", "entry-1")
    assert should_send is True


@pytest.mark.asyncio
async def test_retry_after_partial_completion_no_duplicate_reply(redis) -> None:
    """Simulates: worker processes entry-1 for contact-1, calls
    save_submission (succeeds), marks the send-guard, then "crashes" before
    actually calling GlificClient.send_text. An arq retry re-delivers the
    SAME entry_id — the second attempt must be told NOT to send again.
    """
    first_attempt_should_send = await mark_reply_sent_if_not_already(redis, "contact-2", "entry-42")
    assert first_attempt_should_send is True
    # ... worker "crashes" here, before an actual send call happens in this
    # simulated scenario — but the guard was already marked, mirroring the
    # worker calling mark_reply_sent_if_not_already() BEFORE the send, not
    # after, so a crash between mark and send still prevents a retry from
    # re-sending (accepting "at-most-once send" over "at-least-once send"
    # for outbound replies, since a duplicate reply to a child is worse than
    # an occasional dropped one that a retry can otherwise still recover
    # via the next natural turn).

    retry_attempt_should_send = await mark_reply_sent_if_not_already(redis, "contact-2", "entry-42")
    assert retry_attempt_should_send is False


@pytest.mark.asyncio
async def test_send_guard_is_scoped_per_contact_and_entry(redis) -> None:
    """A guard for one (contact, entry) pair must not block a different
    contact or a different entry for the same contact.
    """
    assert await mark_reply_sent_if_not_already(redis, "contact-3", "entry-1") is True
    assert await mark_reply_sent_if_not_already(redis, "contact-4", "entry-1") is True
    assert await mark_reply_sent_if_not_already(redis, "contact-3", "entry-2") is True
