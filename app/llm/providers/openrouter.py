"""OpenRouter provider adapter (ADR-011 default provider).

Uses the OpenAI-compatible client pointed at OpenRouter's base URL
(`tap-agent-service/CLAUDE.md`'s stack convention) — never called directly
from business logic; only `ModelRouter` constructs and dispatches to this
adapter. Implements the shared `Provider` interface
(`generate(route, messages, tools) -> ModelResponse`).
"""

from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from app.llm.types import ModelResponse, RouteConfig, ToolCall

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


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

        response = await self._client.chat.completions.create(**kwargs)
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
        )
