---
title: Design principles & invariants
okf_version: "0.1"
kind: reference
updated: 2026-08-07
---

# Design principles & invariants

The rules that keep the agent safe and correct. Most are enforced structurally
(a gate, a guard, a test), not merely documented — see the file anchors.

## 1. Fast-ack webhook
Ingress does only: parse → one `SETNX` dedupe → distress check → one `XADD`, then
returns 200. No LLM / Frappe / media work inline. Even an escalation-enqueue Redis
failure never 500s the webhook (`app/ingress/glific_webhook.py:132`). All network
side effects live in the [worker](worker.md).

## 2. State authority = Frappe / TAP LMS
The [state machine](core.md) **never writes LMS state** — it only tracks
conversational state. The only writes to the system of record are the whitelisted
LMS methods (`start_quiz`, `save_submission`, plus test-student provisioning) in
[tools](tools.md). See `app/core/state_machine.py`.

## 3. Identifier authority
`student_id`, `content_id`, quiz `question_id`, and flow names always come from the
**session or registry — never invented by the model**
(`app/core/turn_logic.py:190`, `:251`, `:288`). The model proposes intent; the code
supplies the ids.

## 4. Two-layer distress gate
Safety runs before attribution is certain:
- **Text**, at ingress (`app/ingress/glific_webhook.py:129`).
- **Voice**, in the worker on the raw transcript — including low-confidence ASR
  (a 0.49-confidence transcript is still safety-checked) (`app/worker/turn_processor.py:374`).
- **Defense-in-depth**, again inside `run_turn` (`app/core/turn_logic.py:366`).

## 5. Boot-time fail-closed gates
Startup **aborts** (`RuntimeError`) rather than boot unhealthy if any of these fail:
webhook secret, contact allowlist, Langfuse egress target, LMS write target,
flow-orchestrator prod guard (`app/main.py:82`–`:110`,
`app/config/deploy_gates.py`). See [configuration](configuration.md).

## 6. Worker liveness is explicit
The worker writes a Redis heartbeat after each drain; `/health/ready` and the compose
healthcheck read it. A bare container `Up` is **not** proof of health
(`app/worker/heartbeat.py`, `app/ingress/readiness.py`).

## 7. Idempotency everywhere a retry can double-fire
Message dedupe (`SETNX`, 24h), an outbound send-guard marked only after a successful
send, and per-message-id guards for the language-choice counter and ASR-failure
ticket. Consumer retries then dead-letters at 3 attempts. See [queue](queue.md).

## 8. PII discipline
Never log PII, prompts, or credentials. Logs use a **hashed** contact_id; Langfuse is
the only sanctioned prompt/response capture surface and is told never to log PII
(`app/llm/tracing.py:27`). Guardrails scrub quiz answers before they can reach a
prompt (`app/tools/lms_client.py:196`).

Related: [ingress](ingress.md) · [worker](worker.md) · [tools](tools.md) · [llm](llm.md)
