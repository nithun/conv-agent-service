"""Ingress layer: FastAPI routes that receive Glific webhooks.

Invariant (L-002): handlers here must fast-ack (<200ms) and enqueue only —
no LLM call, Frappe call, or media download may run inline. Real webhook
parsing/validation lands in WBS 1.1; dedupe (L-003) in 1.2.
"""
