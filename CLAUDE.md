# CLAUDE.md — tap-agent-service

This is the FastAPI microservice's OWN repo, nested inside the
`glific` planning workspace and gitignored there. This file owns stack
conventions. The parent workspace's `CLAUDE.md` and `.claude/agents/*`
govern the planning loop (PRD/CR/BR -> tasks -> build -> review -> test ->
retrospective) on top of these conventions — read both, this file for
"how", the parent for "why" and "what's approved to build."

Architecture design: `../docs/designs/AD-002-tap-agent-service-conversational-pipeline.md`
(components, data flow, external contracts, the port from tap-buddy per
ADR-010/ADR-011/CR-004 — read before touching `app/`).

## Stack

- Python 3.11 (pinned in `pyproject.toml` as `>=3.11,<3.13`).
- FastAPI + `uvicorn[standard]` (ASGI).
- `pydantic` v2 + `pydantic-settings` for config (env-sourced, see
  `app/config/settings.py`; no environment-specific defaults baked in for
  Glific/Frappe URLs — those come from `.env`/compose env only).
- `httpx` for outbound HTTP (Glific GraphQL, Frappe REST, OpenRouter).
- `redis` (py client) + `arq` for the queue/worker layer.
- `guardrails-ai` for deterministic output validators (CR-001 / L-006).
- `openai` SDK (OpenAI-compatible client) — never call a provider SDK
  directly from business logic; always through `app/llm`'s `ModelRouter` +
  Langfuse wrapper (L-004). The adapter is named/keyed `"openrouter"`
  (`app/llm/providers/openrouter.py`) and defaults to OpenRouter's real
  cloud base URL, but its base URL is a runtime setting
  (`Settings.openrouter_base_url`). **CR-012 (2026-07-28): in this
  service's actual production configuration, `tap_buddy_turn`'s traffic is
  pointed at the self-hosted LiteLLM gateway (below), which serves Gemini
  via Vertex AI — NOT OpenRouter's cloud.** That gateway is the real,
  documented production LLM transport, not a local-dev-only stopgap; see
  `app/config/settings.py::openrouter_base_url` and
  `app/llm/providers/openrouter.py`'s module docstring for the full naming
  rationale (CR-012 AC-3), and `app/llm/tracing.py` (F-079) for how the
  Langfuse trace is corrected to record the actually-served model.
- `langfuse` SDK for tracing every model call (ADR-005).

### Why `arq`, not `celery`

Chosen at scaffold time (WBS 0.1). Reasons:
- **asyncio-native**: the whole service is async (FastAPI, httpx, redis.asyncio);
  arq's worker loop is `asyncio`-based, so there's one concurrency model
  end-to-end instead of mixing in Celery's prefork/eventlet worker model.
- **Redis-only**: arq uses Redis directly as both broker and result store —
  Redis is already our queue/session store (ADR-003, Redis Streams), so
  there's no second broker (RabbitMQ) or result backend to run/operate.
- **Lighter weight**: no separate `celery beat`/`flower`-style tooling
  needed for this service's scale (500k students, I/O-bound per
  `docs/architecture.md`); arq's job model maps directly onto the
  per-contact-partitioned consumer-group shape the architecture calls for
  (WBS 1.3, 1.5).

If a future task needs Celery-specific features (complex workflow
primitives, non-Redis broker), that's an architecture decision — file it
through `architecture-reviewer` in the parent workspace, don't swap
silently.

### Why `uv`

Dependency resolution/lockfile uses `uv` (installed inside the Dockerfile;
no local Python environment assumed). `uv.lock` is committed — it is the
actual pinned dependency set; `pyproject.toml`'s version ranges are the
declared constraints uv resolves against. Regenerate the lock with
`uv lock` after any `pyproject.toml` dependency change, and commit the
updated lockfile in the same commit as the change.

## Project structure

Mirrors the architecture's top-down pipeline shape
(`docs/architecture.md` Sec4/Sec2 in the parent workspace):

```
app/
├── main.py          FastAPI app entrypoint, router registration
├── config/          pydantic-settings loaders (env-sourced, no baked defaults)
├── ingress/         FastAPI routes receiving Glific webhooks (fast-ack only, L-002)
├── queue/           Redis Streams enqueue/dedupe (SETNX), contact_id partitioning (L-003)
├── worker/          async consumer group: per-contact ordered processing, retry/DLQ (P-002)
├── llm/             ModelRouter + Langfuse tracing wrapper (L-004, P-003)
├── tools/           Frappe tool bindings for the conversation-turn loop (P-001, L-001)
└── delivery/        Glific GraphQL client (createAndSendMessage, startContactFlow, etc.)

tests/               mirrors app/ 1:1 (tests/ingress, tests/queue, ...)
```

Each `app/*` subpackage currently (WBS 0.1) contains only an `__init__.py`
docstring describing its future contents plus whatever a specific task has
implemented — do not add business logic to a package ahead of the WBS task
that owns it (MVP discipline, no pre-scaffolding hypothetical future
state).

## Local dev — Docker-first (no bare `uvicorn` workflow)

Per `docs/architecture.md` Sec5 "Docker-first local dev and build" in the
parent workspace: local dev runs via `docker compose up`, not bare-metal
`uvicorn` against a separately provisioned Redis. One `Dockerfile` (this
repo, multi-stage: `dev` target used by compose + CI, `prod` target is the
staging/prod artifact) serves both.

```bash
cp .env.example .env        # first time only; fill in secrets as they exist
docker compose up --build   # boots app (HOST PORT 8001, public) + worker +
                            # Redis (127.0.0.1:6379) + self-hosted Langfuse
                            # (dashboard 127.0.0.1:3001) + LiteLLM gateway
                            # (127.0.0.1:4001) — see .env.example
curl http://localhost:8001/health
docker compose ps           # confirm app, worker, redis, langfuse, litellm all Up
docker compose down         # tear down when finished
```

**Internal service ports are loopback-only, by a tracked override (F-044 /
ADR-012 BC-2).** `docker-compose.yml` publishes no host port at all for
`redis`/`langfuse`/`litellm` — app and worker reach them over the compose
network by service name. The tracked `docker-compose.override.yml` (loaded
automatically alongside `docker-compose.yml`, no extra `-f` flag needed) is
the sole source of any host publish for those three services, and binds
each to `127.0.0.1` only — reachable from the host/VM loopback for
debugging, never from another machine (this closes the gap where an
untracked one-off file on the VM was the only thing preventing an
unauthenticated Redis, a master-keyed LiteLLM, and PII-bearing Langfuse
traces from being reachable by every co-tenant VM on the shared GCP `default`
network). The `app` service's port (8001) is deliberately untouched — it is
the public Glific webhook target. `tests/config/test_internal_ports_loopback_only.py`
mechanically enforces this: it fails if either file drifts from this shape.

**`worker` is not optional.** `app` (the FastAPI ingress) only fast-acks the
Glific webhook and enqueues the turn onto its per-contact Redis Stream
(L-002) — it never does the LLM call, Frappe tool calls, guardrail pass, or
the actual Glific reply/escalation send. `app/worker/main.py`
(`run_worker_loop`) is the standalone async process that drains those
streams and performs the real side effects, including draining the
escalation stream (distress-hit Glific send + Ticket-open). If `worker`
isn't running, `docker compose up` still looks healthy (ingress keeps
returning 200 OK) but every turn reply and every distress escalation is
silently dropped — there is no error surfaced anywhere in that path. Run
`docker compose ps` after `up` and confirm `worker` shows `Up`, not just
`app`/`redis`.

**App publishes on host port 8001, not 8000** — tap-buddy (still the live
Glific webhook target until F-019's cutover) holds 8000, and both services
run side by side through the transition. glific-platform's CR-001 re-point
must target this service's 8001.

`GLIFIC_API_URL=http://host.docker.internal:4000` in `.env.example` is a
**macOS-only Docker networking value** (Glific dev runs on the host, not in
a container — see `docs/glific-dev-endpoint.md` in the parent workspace).
It must never be hardcoded in application code or appear as a default in
staging/prod config — it exists only in the local `.env` (gitignored).

## Running lint/type-check/tests

Run these the same way CI (WBS 0.7) will — inside the `dev` build target,
matching the container path:

```bash
docker compose build app
docker compose run --rm app pytest
docker compose run --rm app ruff check .
docker compose run --rm app mypy app
```

Equivalent one-liners without compose, if you already have the `dev` image
built:

```bash
docker build --target dev -t tap-agent-service:dev .
docker run --rm tap-agent-service:dev pytest
docker run --rm tap-agent-service:dev ruff check .
docker run --rm tap-agent-service:dev mypy app
```

A change is not "done" until `pytest`, `ruff check .`, and `mypy app` all
pass — this is the same gate CI enforces once WBS 0.7 wires it up.

## Test layout

- `tests/<layer>/test_*.py` mirrors `app/<layer>/` — e.g.
  `app/ingress/health.py` -> `tests/ingress/test_health.py`.
- Use `httpx.ASGITransport` + `httpx.AsyncClient` against the FastAPI `app`
  object for route-level tests (see `tests/ingress/test_health.py`) — no
  `TestClient`/`requests`-style sync client, to match the async stack.
- Any function with a side effect (Frappe call, Glific call, enqueue,
  Redis write, model call) ships with a covering test in the SAME commit —
  parent workspace's feature-builder contract, enforced here too.
- `pytest-asyncio` is configured in `asyncio_mode = "auto"` — async test
  functions do not need an explicit `@pytest.mark.asyncio` decorator, but
  we add it anyway for readability/greppability.

## Config / secrets discipline

- Every external URL (`GLIFIC_API_URL`, `FRAPPE_API_URL`, `REDIS_URL`) and
  every credential (`OPENROUTER_API_KEY`, `LANGFUSE_*`) is read via
  `app/config/settings.py`'s `Settings` (pydantic-settings), never
  hardcoded, never given an environment-specific default in code.
- `FRAPPE_API_URL` has no default at all — code that needs it must fail
  loudly if unset, not silently point at localhost or a guessed URL (see
  `docs/planning/PHASE-0-tasks.md` task 0.5a in the parent workspace).
- Never log PII, prompts, or credentials (L-007) — Langfuse is the
  sanctioned prompt/response capture surface.

## What NOT to add here (yet)

Per MVP discipline in the parent workspace's `feature-builder` agent: no
webhook parsing, no queue consumers, no Frappe/Glific client bodies, no
LLM calls — those are WBS 0.3–0.6 / Phase 1 tasks, each gated by their own
task file. This repo, as of WBS 0.1, is scaffold + `/health` only.
