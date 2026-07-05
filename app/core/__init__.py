"""Core conversational logic — channel-agnostic by construction.

Ported from `tap-buddy/src/tap_buddy/core/` (CR-004): state machine, turn
logic, deterministic guardrail checks, and identity resolution. This
package must never import FastAPI, Redis, the Glific client, or the
webhook-ingress module — enforced by `tests/core/test_isolation.py`
(mirrors tap-buddy's own Task 19 isolation discipline).
"""
