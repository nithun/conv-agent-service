"""Queue layer: Redis Streams enqueue/dedupe, partitioned by contact_id.

Invariant (L-003): every inbound message is deduped by Glific message ID
(Redis SETNX, `enqueue.is_duplicate_message`) before enqueue; jobs are
partitioned by contact_id (one Redis Stream key per contact,
`settings.stream_key`) so per-student ordering is a structural guarantee,
not an accidental one. `consumer.py` holds the worker-side read/ack/
dead-letter primitives (P-002). Built fresh per CR-004/ADR-010 R-3 — no
tap-buddy equivalent existed to port (its ordering "guarantee" was
single-process asyncio scheduling luck).
"""
