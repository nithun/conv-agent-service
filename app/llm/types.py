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
    # F-079 (architecture-reviewer ratification of CR-012): the model that
    # actually served this call, when a provider can detect it differs from
    # the configured `RouteConfig.model` label. `None` means "trust
    # route.model" (the common case — a direct OpenRouter call, or any
    # provider with no such detection). Populated by
    # `OpenRouterProvider.generate` when the OpenAI-compatible endpoint is
    # actually the LiteLLM gateway (CR-012): LiteLLM echoes the REQUESTED
    # alias back in the response body's `model` field, so the real served
    # model (e.g. `gemini-2.5-flash`) only appears in a response header —
    # `ModelRouter` forwards this into the Langfuse trace so cost/model
    # dashboards attribute the real Gemini call, not the Claude-shaped
    # route label.
    served_model: str | None = None
    # The real per-call cost LiteLLM computed for the ACTUAL served model
    # (its `x-litellm-response-cost` response header, USD) — `None` when
    # unavailable (real OpenRouter never sends this header). Surfaced as
    # Langfuse trace metadata only (F-079); not fed into Langfuse's own
    # cost_details calculation, whose schema is unverified for this SDK/
    # server pin (L-009 — no guessing a vendor payload shape untested).
    served_cost_usd: float | None = None
