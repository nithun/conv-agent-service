"""Shared provider adapter interface (ADR-011).

Every provider adapter under `app/llm/providers/<name>.py` implements this
protocol — `generate(route_config, messages, tools) -> ModelResponse` — so
call sites (`ModelRouter`, and transitively `turn_logic.py`/tools/
guardrails) never construct a provider client directly and never see a
provider-specific request/response shape. This is the mechanism that keeps
"multi-provider capable" from becoming "multi-provider ad-hoc" (ADR-011
Consequences).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.llm.types import ModelResponse, RouteConfig


@runtime_checkable
class Provider(Protocol):
    """Shared interface every `app/llm/providers/*` adapter must implement."""

    name: str

    async def generate(
        self,
        route: RouteConfig,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse: ...
