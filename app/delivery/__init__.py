"""Delivery layer: Glific GraphQL client (send/flow/HSM/ticketing calls).

Uses the verified live mutation names (see docs/glific-dev-endpoint.md in
the parent workspace): createAndSendMessage, startContactFlow,
updateContact, sendHsmMessage - not the architecture doc's shorthand
(F-003). Implementation lands in WBS 0.6.
"""
