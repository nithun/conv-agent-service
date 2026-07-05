"""Delivery layer: Glific GraphQL client (send/flow/ticketing calls).

Ported from `tap-buddy/src/tap_buddy/adapters/glific_client.py` (CR-004
Task 13/14). Uses the verified live mutation names (see
docs/glific-dev-endpoint.md in the parent workspace):
`createAndSendMessage`, `createMessageMedia`, `startContactFlow`,
`createTicket` — not the architecture doc's shorthand (F-003).
`createTicket` is the CR-002/L-007 escalation surface; tap-buddy's ad-hoc
`adapters/review.py` dashboard does NOT port (ADR-010 Invariant 5).
"""
