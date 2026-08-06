---
type: "Reference"
title: "Testing — `tests/`"
openwiki_generated: true
---

# Testing — `tests/`

`tests/` mirrors `app/` 1:1: `config`, `core`, `delivery`, `guardrails`, `ingress`,
`llm` (+ `llm/providers`), `queue`, `tools`, `worker`, plus top-level lifespan/main
tests.

## Running
`pytest` plus lint + type-check (ruff, mypy), matching the repo's CI gate. See
[../CLAUDE.md](../CLAUDE.md) and [../README.md](../README.md) for the exact commands and
Docker-first workflow. Route-level tests use `httpx.ASGITransport` + `AsyncClient`;
`pytest-asyncio` runs in `asyncio_mode="auto"`.

## Notable guards (worth knowing before you change things)
| Test | Guards |
|---|---|
| `tests/delivery/test_glific_client.py::test_never_calls_send_message_regression_guard` | the client can never regress to the non-existent `sendMessage` mutation |
| `tests/config/test_internal_ports_loopback_only.py` | compose internal ports stay loopback-only |
| `tests/ingress/test_distress_gate_e2e.py` / `_latency.py` / `_resilience.py` | the fast-ack distress gate: correctness, latency budget, resilience |
| `tests/llm/test_gemini_live_smoke.py` | a live LiteLLM→Vertex smoke check |
| `tests/test_lifespan_auth.py`, `test_real_lifespan_deploy_gates.py` | the boot-time fail-closed gates actually abort |
| `tests/test_scripted_replies_reachability.py` | scripted replies are reachable |

## Testing philosophy in this repo
Green tests are treated as a compile check, **not** proof a feature is live
(GL-002): new code paths are traced back to a real entrypoint, and config/contract
changes get at least one real run (boot the stack, drive the flow). The live-smoke and
lifespan tests exist precisely because mocked-green was judged insufficient.

Related: [design-principles](design-principles.md) · [worker](worker.md)
