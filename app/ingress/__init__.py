"""Ingress layer: FastAPI routes that receive Glific webhooks.

Invariant (L-002): handlers here must fast-ack (<200ms) and enqueue only —
no LLM call, Frappe call, or media download may run inline.
`glific_payload.py` parses the webhook body (ported from tap-buddy's
`_parse_payload`); `glific_webhook.py` is the fast-ack route (dedupe +
top-level distress gate + enqueue, CR-004 Task 3/5); `media.py` is the
inbound ASR trigger + outbound `/media/{token}` route (Task 4) — TTS
generation itself and the media download happen in the worker/delivery
path, never inline in the webhook handler.
"""
