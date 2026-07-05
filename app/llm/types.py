"""Shared types for the LLM layer (ADR-011): provider-agnostic request/
response shapes so `ModelRouter`, every `app/llm/providers/*` adapter, and
call sites (worker/turn logic) all speak the same vocabulary regardless of
which provider actually served a given route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FallbackRoute:
    """A `{provider, model}` pair `ModelRouter` dispatches to if the primary
    provider errors or times out (ADR-011's `fallback` field) — no
    `selected_by`/`fallback` of its own (one level of fallback only).
    """

    provider: str
    model: str


@dataclass(frozen=True)
class RouteConfig:
    """One resolved `MODEL_ROUTES[*]` entry (ADR-011 shape).

    `provider` defaults to `"openrouter"` if the source config omits it.
    `selected_by` is required whenever `provider != "openrouter"` — enforced
    by `app/llm/router.py::validate_model_routes`, not by this dataclass
    itself (construction-time validation lives at the registry-loading
    boundary so the evidence-gate check is a single, testable place).
    """

    name: str
    provider: str = "openrouter"
    model: str = ""
    selected_by: str | None = None
    fallback: FallbackRoute | None = None


@dataclass
class ToolCall:
    """One model-requested tool invocation."""

    name: str
    arguments: dict[str, Any]


@dataclass
class ModelResponse:
    """The shared return shape every provider adapter's `generate()` must
    produce — this is what keeps call sites (turn_logic) provider-blind.
    """

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Usage/latency, when the provider reports them — consumed by the
    # Langfuse tracing wrapper (Task 2), not required for a valid response.
    input_tokens: int | None = None
    output_tokens: int | None = None
    raw: Any = None
