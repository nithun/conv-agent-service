"""Worker layer: async consumer group processing jobs off the Redis Stream.

Per-contact ordered processing, retry/backoff, and dead-letter handling
(P-002 in patterns.md) land in WBS 1.5-1.6. Never put webhook-handling
logic here — this layer only consumes already-enqueued jobs.
"""
