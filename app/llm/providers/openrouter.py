"""OpenRouter provider adapter (ADR-011 default provider).

Uses the OpenAI-compatible client pointed at OpenRouter's base URL
(`tap-agent-service/CLAUDE.md`'s stack convention) — never called directly
from business logic; only `ModelRouter` constructs and dispatches to this
adapter. Implements the shared `Provider` interface
(`generate(route, messages, tools) -> ModelResponse`).

NAMING NOTE (CR-012 AC-3, F-076): this adapter's class/module/registry key
is named "openrouter" because it speaks the OpenAI-compatible wire protocol
OpenRouter defined and defaults to OpenRouter's real cloud endpoint. But its
`base_url` is a runtime-configurable field (`Settings.openrouter_base_url`),
and in this service's actual production configuration it is pointed at the
self-hosted LiteLLM gateway, which serves `tap_buddy_turn` via Vertex AI
`gemini-2.5-flash` — NOT OpenRouter's cloud. feature-builder's call on Open
Q#2: renaming `"openrouter"` throughout (the class, the registry key, both
env vars, every test) was judged too large a blast radius for what is
fundamentally a labelling problem (CR-012 revised scope is doc/config-
honesty, not a rewrite) — this comment plus the ones at
`app/llm/router.py::build_default_model_router`,
`app/llm/routes.py::validate_model_routes`, and
`app/config/settings.py::openrouter_base_url` are the chosen alternative:
explicit, at every site that could mislead a reader. See also `served_model`
below (F-079): the Langfuse TRACE now records the actually-served model
regardless of this adapter's own name.
"""

from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from app.llm.types import ModelResponse, RouteConfig, ToolCall

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _extract_litellm_served_model(headers: Any) -> str | None:
    """F-079: LiteLLM's OpenAI-compatible response echoes the REQUESTED
    model alias back in the JSON body's `model` field (confirmed live
    2026-07-28 against the compose `litellm` service — an
    `anthropic/claude-sonnet-5` request round-trips
    `model: "anthropic/claude-sonnet-5"` even though Vertex AI actually
    served `gemini-2.5-flash`), so the real served model only appears in
    the `x-litellm-model-api-base` response HEADER, e.g.
    `".../publishers/google/models/gemini-2.5-flash:generateContent"`.
    Returns `None` when that header is absent — real OpenRouter responses
    never carry it, so callers keep trusting `route.model` unchanged.
    """
    api_base = headers.get("x-litellm-model-api-base")
    if not api_base:
        return None
    tail = api_base.rsplit("/models/", 1)[-1]
    model = tail.split(":", 1)[0]
    return model or None


def _extract_litellm_response_cost(headers: Any) -> float | None:
    """F-079: LiteLLM computes the real per-call cost for the model it
    actually served and returns it as the `x-litellm-response-cost` header
    (USD). `None` when absent (real OpenRouter) or unparseable.
    """
    raw = headers.get("x-litellm-response-cost")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _to_openai_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Converts this codebase's plain function-declaration dicts
    (`{"name", "description", "parameters"}`, the shape ported from
    tap-buddy's `core/tools.py`) into the OpenAI-compatible
    `{"type": "function", "function": {...}}` tool-call wrapper OpenRouter
    expects.
    """
    return [{"type": "function", "function": tool} for tool in tools]


class OpenRouterProvider:
    """Shared-interface adapter for OpenRouter, the default provider
    (ADR-011) — no route in this port chooses a non-default provider, so
    this is the only provider adapter this CR ships with production traffic
    flowing through.
    """

    name = "openrouter"

    def __init__(self, api_key: str, base_url: str = OPENROUTER_BASE_URL):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def generate(
        self,
        route: RouteConfig,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": route.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)

        # F-079: use with_raw_response so the LiteLLM-only
        # x-litellm-model-api-base / x-litellm-response-cost headers are
        # visible — `.create()` alone only exposes the parsed JSON body,
        # which LiteLLM populates with the REQUESTED alias, not the model
        # that actually served the call. `.parse()` below returns the exact
        # same `ChatCompletion` object `.create()` would have.
        raw_response = await self._client.chat.completions.with_raw_response.create(**kwargs)
        served_model = _extract_litellm_served_model(raw_response.headers)
        served_cost_usd = _extract_litellm_response_cost(raw_response.headers)
        response = raw_response.parse()
        choice = response.choices[0]
        message = choice.message

        text = message.content or ""
        tool_calls: list[ToolCall] = []
        for call in message.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments)
            except (TypeError, ValueError):
                arguments = {}
            tool_calls.append(ToolCall(name=call.function.name, arguments=arguments))

        usage = getattr(response, "usage", None)
        return ModelResponse(
            text=text,
            tool_calls=tool_calls,
            input_tokens=getattr(usage, "prompt_tokens", None) if usage else None,
            output_tokens=getattr(usage, "completion_tokens", None) if usage else None,
            raw=response,
            served_model=served_model,
            served_cost_usd=served_cost_usd,
        )
