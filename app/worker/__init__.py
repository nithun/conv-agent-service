"""Worker layer: async consumer group processing jobs off the Redis Stream.

Ported from tap-buddy's `adapters/glific_webhook.py::_process_turn` (CR-004
Task 7/8): `session_store.py` is the Redis-backed session store (R-2);
`turn_processor.py` is the state-machine decision tree (identity
resolution -> greeting/readiness -> mode_choice -> `app/core/turn_logic.py`
handoff); `distress_gate.py` is the TOP-LEVEL distress check ingress calls
directly, before enqueue (BR-003/Invariant 4); `consumer_loop.py` drains
one contact's Stream in order with retry/backoff/dead-letter wiring
(P-002) and the idempotent-send guard (Task 6). Never put webhook-handling
logic here — this layer only consumes already-enqueued jobs (or, for the
distress gate, is called directly by ingress as a pre-enqueue check).
"""
