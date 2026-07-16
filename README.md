# tap-agent-service

FastAPI microservice between Glific (WhatsApp delivery) and TAP LMS/Frappe
(system of record). See `CLAUDE.md` in this repo for stack conventions and
`../CLAUDE.md` / `../docs/architecture.md` in the parent planning workspace
for system design.

**Integrating a messaging platform** (webhook contract, delivery-client
interface, what a new channel needs): `docs/INTEGRATION.md`. Glific is the
reference integration.

Quickstart: `docker compose up --build` then `curl http://localhost:8001/health`.

This brings up **six** containers: `app` (FastAPI ingress, host port 8001 — tap-buddy holds 8000 until the F-019 cutover),
`worker` (the standalone async worker process, `app/worker/main.py`), and
`redis`. **Both `app` and `worker` must be running** — `app` only fast-acks
the Glific webhook and enqueues the turn (L-002: no LLM/Frappe/media work
inline in the webhook handler); `worker` is what actually processes the
turn (LLM call, Frappe tool calls, guardrails, the real Glific reply) and
drains the escalation stream (the Glific send + Ticket-open for a distress
hit). If `worker` is not running, ingress will still return 200 OK for
every inbound message, but no turn reply and no distress escalation will
ever be delivered — this is silent from the webhook's point of view, so
always confirm `docker compose ps` shows `worker` as `Up` (not just `app`
and `redis`) when diagnosing "nothing is happening" in local dev.
