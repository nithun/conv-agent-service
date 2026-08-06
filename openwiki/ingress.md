---
title: Ingress (the fast-ack layer)
okf_version: "0.1"
kind: module
updated: 2026-08-07
source: app/ingress/
---

# Ingress — `app/ingress/`

The `app` process. Verifies, parses, dedupes, distress-checks, enqueues, and
**fast-acks**. No LLM / Frappe / media work happens here (the fast-ack invariant —
see [design-principles](design-principles.md#1-fast-ack-webhook)).

## The webhook route

- Defined at `app/ingress/glific_webhook.py:84` —
  `@router.post("/webhook/glific", dependencies=[Depends(verify_glific_webhook_signature)])`.
- Handler `glific_webhook()` (`:90`):
  1. parse JSON → `parse_glific_webhook_payload`
  2. `is_duplicate_message` (`SETNX`) → drop duplicates
  3. deterministic `detect_distress(text, None)` (`:129`) → on hit `enqueue_escalation` and ack (`:131`)
  4. otherwise `enqueue_turn` (`:147`)
- App assembled in `app/main.py:145` `create_app()`; routers registered at `:148`–`:151`.

## Signature auth

`verify_glific_webhook_signature` (`app/ingress/webhook_auth.py:161`) is a FastAPI
dependency that runs **before** the handler body:
- HMAC-SHA256 over `"<timestamp>.<raw_body>"`; header `X-Glific-Signature: t=…,v1=…`.
- **413** for an oversized body (checked before HMAC); uniform **401** for every other
  rejection; comparison via `hmac.compare_digest` only.
- Secret resolved once at boot by `resolve_glific_webhook_secret` (`:90`) —
  **fail-closed** unless `environment == "local"`.

## Payload parsing

`parse_glific_webhook_payload` (`app/ingress/glific_payload.py:61`), model
`GlificWebhookPayload` (`:42`). Handles Glific quirks: `@`-expression-as-absent (`:52`),
simulator `_N` phone-suffix stripping (`:83`), missing `message_id` generation (`:85`).
A non-dict body raises `ValueError` → **400**.

## Contact allowlist (dormant)

`verify_contact_allowlisted` (`app/ingress/contact_allowlist.py:150`) exists but its
route dependency was **removed** per CR-011 (`glific_webhook.py:40`). The boot-time
`resolve_contact_allowlist` (`:84`) still runs. Logs a **hashed** contact_id only.

## Health vs readiness

- `GET /health` (`app/ingress/health.py:15`) — dependency-free liveness; 200 whenever
  the process is up.
- `GET /health/ready` (`app/ingress/readiness.py:32`) — 200 only if Redis pings **and**
  the [worker heartbeat](worker.md#heartbeat) is fresh, else **503**. This is the real
  "is the worker alive" signal.

## Media

`app/ingress/media.py` — inbound voice `transcribe_inbound_media` (`:63`); outbound TTS
`synthesize_and_store` (`:96`, raises loudly if `PUBLIC_BASE_URL` is unset); serving
route `GET /media/{token}` (`:146`) with path-traversal guarding (`:87`).

Next: [queue](queue.md) — where enqueued turns go.
