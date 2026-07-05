"""Idempotent retry semantics for partially-completed turn jobs (CR-004
Task 6). Tap-buddy has no retry semantics at all (single-shot asyncio
task) — this is designed fresh, no prior behavior to port.

Problem: if an `arq` job for a turn is retried after partially completing
(e.g. `save_submission` succeeded, then the worker crashed before sending
the reply), a naive retry would re-run the whole turn and send a SECOND
reply for the same entry_id. `save_submission` itself is assumed already
idempotent Frappe-side (existing whitelisted API, not this CR's to change)
— what this module adds is an explicit OUTBOUND-SEND guard: a Redis SETNX
keyed by `(contact_id, entry_id)` marks "reply already sent for this
Stream entry," checked immediately before the worker calls
`GlificClient.send_text`/`send_audio`, so a retried job that reaches the
send step twice only actually sends once.
"""

from __future__ import annotations

from redis.asyncio import Redis

# Matches the dedupe-key TTL family in app/queue/settings.py — long enough
# to cover any plausible arq retry window, short enough not to grow
# unboundedly.
SEND_GUARD_TTL_SECONDS = 24 * 60 * 60

_SEND_GUARD_KEY_PREFIX = "tap_agent:sent:"


def _send_guard_key(contact_id: str, entry_id: str) -> str:
    return f"{_SEND_GUARD_KEY_PREFIX}{contact_id}:{entry_id}"


async def mark_reply_sent_if_not_already(redis: Redis, contact_id: str, entry_id: str) -> bool:
    """Atomically checks-and-marks whether a reply for this
    (contact_id, entry_id) has already been sent. Returns True if this call
    is the one that should proceed to send (first time), False if a prior
    attempt already sent the reply (caller must skip the send call).

    This is a single SETNX round-trip — safe to call from the worker's
    send step without adding meaningful latency.
    """
    was_set = await redis.set(
        _send_guard_key(contact_id, entry_id), "1", nx=True, ex=SEND_GUARD_TTL_SECONDS
    )
    return was_set is not None


async def is_reply_already_sent(redis: Redis, contact_id: str, entry_id: str) -> bool:
    """Read-only check (no mutation) — used by the consumer loop BEFORE
    calling `process_turn_fn`, to detect "this entry_id already completed
    successfully on a prior attempt" (worker crashed between finishing the
    send and acking the Stream entry) without itself marking anything. The
    mutating mark happens only AFTER `process_turn_fn` succeeds
    (`mark_reply_sent_if_not_already`), so a genuinely FAILED attempt never
    marks the guard and is free to retry.
    """
    value = await redis.get(_send_guard_key(contact_id, entry_id))
    return value is not None
