---
title: Configuration & boot gates
okf_version: "0.1"
kind: module
updated: 2026-08-07
source: app/config/
---

# Configuration — `app/config/`

Settings, the fail-closed boot gates, and the flow registry.

## Settings (`settings.py`)
`class Settings(BaseSettings)` (`:37`), `get_settings()` (`:223`) — pydantic-settings,
`env_file=".env"`, with **no environment-specific defaults** for external URLs or
secrets. `environment` defaults to `""` (not `"local"`) so that *unset ≠ local* (`:64`)
— a safety choice: you must opt into local behavior explicitly.

## Boot-time fail-closed gates (`deploy_gates.py`)
Run in `lifespan` **before any client is constructed** (`app/main.py:82`–`:110`); each
raises `RuntimeError` to abort startup rather than boot unhealthy:

| Gate | File | Rule |
|---|---|---|
| Webhook secret | `webhook_auth.py:90` | secret must resolve unless `environment=="local"` |
| Contact allowlist | `contact_allowlist.py:84` | resolved at boot (route dependency dormant) |
| Langfuse egress | `deploy_gates.py:94` | Langfuse must be **self-hosted** in prod (GL-005-a) |
| LMS write target | `deploy_gates.py:126` | `FRAPPE_API_URL` must be `lms.evalix.xyz` (GL-005-b) |
| Flow-orchestrator prod | `deploy_gates.py:155` | flow orchestrator **forbidden** in prod (BC-4) |

Env values are compared case- and whitespace-normalized.

## Flow registry (`flow_registry.py`)
`load_flow_registry` (`:77`) loads `flow_registry.yaml` keyed by a stable **name**. A
BC-3 no-loopback guard rejects any entry whose `flow_id == GLIFIC_TURN_FLOW_ID`
(`:98`–`:109`). An empty/absent file yields an empty registry — a deliberate kill
switch.

## Environment variables (names only — never commit values)
Runtime: `ENVIRONMENT`, `GLIFIC_API_URL`, `FRAPPE_API_URL`, `REDIS_URL`,
`OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`, `GLIFIC_PHONE`, `GLIFIC_PASSWORD`,
`GLIFIC_HOST_HEADER_OVERRIDE`, `GLIFIC_TURN_FLOW_ID`, `TAP_LMS_API_KEY`,
`TAP_LMS_TOKEN`, `SESSION_TTL_SECONDS`, `PUBLIC_BASE_URL`, `MEDIA_DIR`,
`MEDIA_TTL_SECONDS`, `GOOGLE_APPLICATION_CREDENTIALS`, `GLIFIC_WEBHOOK_SECRET`,
`MAX_WEBHOOK_BODY_BYTES`, `GLIFIC_CONTACT_ALLOWLIST`, `FLOW_ORCHESTRATOR_ENABLED`.

Compose/gateway only: `LITELLM_MASTER_KEY`, `GCP_SA_KEY_PATH`, `LANGFUSE_DB_PASSWORD`,
`LANGFUSE_NEXTAUTH_SECRET`, `LANGFUSE_SALT`.

> Populate a real `.env` from [`../.env.example`](../.env.example). Keys are secrets —
> never paste values into this wiki or any committed file (GL-001).

Related: [llm](llm.md) (the LiteLLM→Vertex note) · [design-principles](design-principles.md#5-boot-time-fail-closed-gates)
