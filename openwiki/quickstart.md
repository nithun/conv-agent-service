---
type: "quickstart"
title: "tap-agent-service Quickstart"
description: "Quickstart for the tap-agent-service, a conversational agent between Glific and TAP LMS."
---

# tap-agent-service

A FastAPI **conversational agent** that sits between **Glific** (WhatsApp / SwiftChat
delivery) and **TAP LMS / Frappe** (the system of record). A student messages on
WhatsApp; the service understands the turn with an LLM, reads/writes learning state
through a small whitelist of Frappe methods, runs safety guardrails, and replies —
all while keeping Frappe as the single source of truth.

> **About this wiki.** These pages were authored as a starter from a full read of
> `app/` on 2026-08-07. The repo also ships OpenWiki wiring (`.openwikiignore`,
> `openwiki/INSTRUCTIONS.md`, `scripts/openwiki-refresh.sh`) so the team can
> regenerate/maintain this wiki with `openwiki --update` — see
> `../docs/OPENWIKI.md`. Env-var **names** appear here; no
> secret values.

## Two processes, one Redis

The service runs as **two OS processes that share one Redis**:

| Process | Role | Entry |
|---|---|---|
| `app` | FastAPI **ingress** — verify, dedupe, distress-check, enqueue, then **fast-ack** | `app/main.py:145` `create_app()` |
| `worker` | Standalone async loop — **all real side effects** (LLM, Frappe, Glific, guardrails) | `app/worker/main.py:306` |

Redis is configured with `decode_responses=True` **mandatorily** (a live bytes-vs-str
failure is documented at `app/main.py:46` and `app/worker/main.py:161`).

## Message lifecycle

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: an unescaped angle bracket inside a label breaks rendering; rephrase the label. -->
```text
flowchart TD
  A[POST /webhook/glific] -->|verify HMAC| B[parse payload]
  B --> C{duplicate?<br/>SETNX}
  C -->|yes| Z[200 ack, drop]
  C -->|no| D{distress?<br/>text check}
  D -->|yes| E[XADD escalation stream] --> Z
  D -->|no| F[XADD per-contact stream] --> Z
  F -.async.-> W[worker drains stream]
  W --> G[process_turn: state machine]
  G --> H[run_turn: LLM + LMS tools]
  H --> I[guardrails: run_all_validators]
  I --> J[deliver via Glific createAndSendMessage]
  J --> K[re-arm turn flow / open ticket if escalated]
  E -.async.-> X[escalation worker] --> Y[Glific reply + createTicket]
```

Ingress does **only** parse + one `SETNX` + a distress check + one `XADD` — no LLM,
Frappe, or media work inline (the fast-ack invariant, <200ms). Everything with a
network side effect happens in the worker. See [design-principles](design-principles.md).

## Subsystem map

| Page | What lives there |
|---|---|
| [ingress](ingress.md) | Webhook route, HMAC auth, payload parsing, dedupe, health/readiness, media serving |
| [queue](queue.md) | Redis Streams: per-contact turn streams, escalation stream, idempotency, DLQ |
| [worker](worker.md) | The drain loop, `process_turn`, escalation consumer, session store, heartbeat |
| [core](core.md) | Channel-agnostic conversation logic: state machine, `run_turn`, identity, detectors |
| [llm](llm.md) | ModelRouter, model routes, provider adapter, Langfuse tracing (LiteLLM→Vertex) |
| [guardrails](guardrails.md) | Guardrails-AI validators wrapping the reply-shape checks |
| [tools](tools.md) | The 8-method TAP LMS / Frappe client + LLM tool schemas + speech client |
| [delivery](delivery.md) | The Glific GraphQL client and the exact mutations it calls |
| [configuration](configuration.md) | Settings, boot-time fail-closed deploy gates, flow registry, env vars |
| [external-contracts](external-contracts.md) | Consolidated Glific + Frappe contracts |
| [testing](testing.md) | Test layout, notable regression guards, how to run |
| [design-principles](design-principles.md) | The invariants that make the system safe |

## Start here

1. [design-principles](design-principles.md) — the non-negotiables (state authority, fast-ack, distress gate).
2. This lifecycle diagram, then [ingress](ingress.md) → [queue](queue.md) → [worker](worker.md).
3. [external-contracts](external-contracts.md) when wiring Glific or Frappe.

Canonical hand-written references (not superseded by this wiki):
`../README.md`, `../CLAUDE.md`,
`../docs/INTEGRATION.md`,
`../docs/GLIFIC-FLOW-INTEGRATION.md`.
