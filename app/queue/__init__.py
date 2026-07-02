"""Queue layer: Redis Streams enqueue/dedupe, partitioned by contact_id.

Invariant (L-003): every inbound message is deduped by Glific message ID
(Redis SETNX) before enqueue; jobs are partitioned by contact_id so
per-student ordering is guaranteed. Implementation lands in WBS 1.2-1.3.
"""
