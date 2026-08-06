---
type: "Reference"
title: "LLM — `app/llm/`"
openwiki_generated: true
---

# LLM — `app/llm/`

Route → provider adapter → traced model call.

## ⚠️ Naming caveat (read this first)
The `"openrouter"` key, the `OpenRouterProvider` adapter, and the `OPENROUTER_*` env
vars are **legacy names**. In real deployment the live route is served through the
**self-hosted LiteLLM gateway → Vertex AI `gemini-2.5-flash`** (GCP project
`tap-buddy-mvp`), **not** OpenRouter cloud. All three `model_name` aliases in
[`litellm.config.yaml`](../litellm.config.yaml) map to `vertex_ai/gemini-2.5-flash`.
The `anthropic/claude-*` strings in `routes.py` are **route labels**, not a claim that a
Claude model is served. Documented at `settings.py:71`, `router.py:43`, `routes.py:9`,
`providers/openrouter.py:9`.

## ModelRouter (`router.py`)
`ModelRouter` (`:33`); `generate` (`:71`) resolves a route → provider adapter → wraps a
**Langfuse trace** around every call (route / provider / model / tokens / latency).
`build_default_model_router` (`:116`) wires the one shipped adapter (key `"openrouter"`)
with `base_url` from `Settings.openrouter_base_url`.

## Routes (`routes.py`)
`MODEL_ROUTES` (`:36`):

| Route | Label | Call site |
|---|---|---|
| `tap_buddy_turn` | `anthropic/claude-sonnet-5` | **live** (the only route with a real call site) |
| `intent_classification` | `claude-haiku-4.5` | none yet |
| `escalation_reasoning` | `claude-opus-4.8` | none yet |

`validate_model_routes` (`:69`) enforces an evidence gate; `UnknownRouteError` — the
router **never** falls back to a default model string.

## Provider adapter (`providers/openrouter.py`)
`OpenRouterProvider` (`:84`) — OpenAI-compatible `AsyncOpenAI`. Uses `with_raw_response`
(`:115`) to read the LiteLLM headers `x-litellm-model-api-base` /
`x-litellm-response-cost` and detect the **actually-served** model (F-079).

## Tracing (`tracing.py`)
`Tracer` protocol; `LangfuseTracer` (`:164`, v2 SDK `generation()` / `.end()`),
`NullTracer`, `RecordingTracer`. Corrects the trace `model` field to the real served
model on `end()` (`:245`). **Never logs PII or prompts** (`:27`) — see
[design-principles](design-principles.md#8-pii-discipline). Langfuse must be
self-hosted in production (a boot gate; see [configuration](configuration.md)).

Related: [core](core.md) · [tools](tools.md) · [configuration](configuration.md)
