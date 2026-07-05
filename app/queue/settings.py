"""Queue-layer tunables — TTLs and stream-key naming, isolated from the rest
of `app/config/settings.py` so the queue module can be exercised in tests
without pulling in Frappe/Glific/LLM env vars.
"""

from __future__ import annotations

# Redis SETNX dedupe TTL on Glific message_id. Matches-or-exceeds tap-buddy's
# SQLite `message_dedupe` 24h window (store.py's MESSAGE_DEDUPE_TTL_SECONDS)
# per CR-004's Session/queue state section ("TTL matches or exceeds the 24h
# window the SQLite version used, unless a shorter window is deliberately
# chosen") — no deliberate shortening was chosen, so this stays at 24h.
MESSAGE_DEDUPE_TTL_SECONDS = 24 * 60 * 60

# Redis key prefixes. One Stream per contact_id (not one shared stream with
# a contact_id field) is the mechanism that gives per-contact ordering a
# structural guarantee rather than an accidental one: a single consumer
# processing one contact's stream in order can never interleave that
# contact's messages with another's, and Redis Streams themselves guarantee
# FIFO order of XADD calls within one stream.
STREAM_KEY_PREFIX = "tap_agent:stream:contact:"
DEDUPE_KEY_PREFIX = "tap_agent:dedupe:message:"
CONSUMER_GROUP_NAME = "tap_agent_workers"


def stream_key(contact_id: str) -> str:
    return f"{STREAM_KEY_PREFIX}{contact_id}"


def dedupe_key(message_id: str) -> str:
    return f"{DEDUPE_KEY_PREFIX}{message_id}"
