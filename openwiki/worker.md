---
title: Worker (all real side effects)
okf_version: "0.1"
kind: module
updated: 2026-08-07
source: app/worker/
---

# Worker — `app/worker/`

The `worker` process: a standalone async loop that drains Redis Streams and performs
**all** the network side effects (LLM, Frappe, Glific, guardrails). Started via
`python -m app.worker.main` (`docker-compose.yml:100`), which runs
`asyncio.run(run_worker_loop())` (`app/worker/main.py:306`).

## The loop (`main.py`)
- `run_worker_loop` (`:175`): discover active contact streams via a `KEYS` scan (`:68`),
  drain each (`:251`), drain the escalation stream (`:269`), then **write the heartbeat**
  (`:293`). `POLL_INTERVAL_SECONDS = 1.0` (`:63`).
- `build_process_turn_fn` (`:94`) composes `process_turn` + `maybe_rearm_turn_flow`
  (the latter in a `finally`).
- `_build_speech_client` (`:74`) returns `None` if GCP creds are absent → voice
  gracefully degrades to text.

> The `KEYS`-scan discovery is fine at trial scale; a registry-set is the noted
> optimization for later.

## Per-entry handling (`consumer_loop.py`)
`process_one_entry` (`:44`): read-only send-guard check → `process_turn_fn` → on
exception, **retry (leave pending)** or **dead-letter at attempt 3** — it **never
re-raises**; on success, mark the send-guard and ack. `drain_contact_stream_once`
(`:98`).

## The turn (`turn_processor.py`)
`process_turn` (`:254`) is the ported decision tree:
`TESTAIRESET` reset (`:303`) → audio transcription + ASR-failure / worker-side distress
gate (`:341`–`:434`) → new-contact phone lookup (CR-013, `:436`) → identity confirm /
auto-provision (`:499`) → candidate confirm (`:620`) → language choice (`:654`) →
greeting (`:717`) → mode choice (`:732`) → **`run_turn`** (`:747`, see [core](core.md)) →
`_deliver_reply` (`:756`) → open ticket if escalated (`:771`) → dispatch pending flow
(`:773`). `maybe_rearm_turn_flow` (`:793`).

`_deliver_reply` (`:160`) does voice-TTS-or-text with distinct failure classes so an
ASR/TTS failure escalates rather than silently dropping.

## Escalation consumer (`escalation_consumer.py`)
`process_one_escalation_entry` (`:43`), `drain_escalation_stream_once` (`:94`).
Escalations are **acked** (not silently DLQ'd) after 3 attempts, with a greppable
`ERROR` log — losing a distress escalation must be visible.

## Distress handling (`distress_gate.py`, `escalation.py`)
`handle_distress` (`distress_gate.py:69`) and shared `_escalate_for_distress` (`:40`):
escalate → save session → scripted `distress_close` reply → `open_ticket_for_escalation`.
`open_ticket_for_escalation` (`escalation.py:41`) — the **only** handoff surface is
`GlificClient.create_ticket` (see [delivery](delivery.md)). Never raises on ticket
failure.

## Session store (`session_store.py`)
`RedisSessionStore` (`:125`): key `tap_agent:session:<contact_id>` (`:43`), hash
serialize/deserialize, TTL refreshed on every save (30-min inactivity, `save_session:135`).

## Heartbeat (`heartbeat.py`, `healthcheck.py`)
Key `tap_agent:worker:heartbeat` (`:32`), TTL 15s (`:49`). `write_heartbeat` (`:61`),
`heartbeat_status` (`:72`). `python -m app.worker.healthcheck` exits 0/1 on freshness
(reused by both the compose healthcheck and `/health/ready`).

Next: [core](core.md) — the channel-agnostic brain `run_turn` calls into.
