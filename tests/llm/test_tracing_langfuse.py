"""LangfuseTracer's v2 SDK call shape (360°-r2 diff-review High finding 1).

The 2026-07-08 live probe caught tokens silently zeroed because
`generation.end()` was passed `usage_details=` — a kwarg the v2.60 SDK
ACCEPTS but only a v3 SERVER ingests; the v2-server field is `usage=`.
That was verified live once; these tests make the shape a pinned,
automated contract so a future edit can't silently regress it. The fake
below stands in for `langfuse.Langfuse` — no network, no real project.
"""

from __future__ import annotations

import pytest

from app.llm import tracing as tracing_module
from app.llm.tracing import LangfuseTracer


class FakeGeneration:
    def __init__(self) -> None:
        self.end_kwargs: dict | None = None

    def end(self, **kwargs) -> None:
        self.end_kwargs = kwargs


class FakeLangfuseClient:
    def __init__(self) -> None:
        self.generation_kwargs: dict | None = None
        self.last_generation = FakeGeneration()

    def generation(self, **kwargs) -> FakeGeneration:
        self.generation_kwargs = kwargs
        return self.last_generation


@pytest.fixture
def tracer(monkeypatch) -> LangfuseTracer:
    fake_client = FakeLangfuseClient()
    # Bypass __init__'s real `from langfuse import Langfuse` + network client.
    t = LangfuseTracer.__new__(LangfuseTracer)
    t._client = fake_client
    return t


def test_start_trace_creates_generation_with_route_metadata(tracer: LangfuseTracer) -> None:
    tracer.start_trace(route_name="tap_buddy_turn", provider="openrouter", model="m-1")

    kwargs = tracer._client.generation_kwargs
    assert kwargs is not None
    assert kwargs["name"] == "tap_buddy_turn"
    assert kwargs["model"] == "m-1"
    assert kwargs["metadata"] == {"route": "tap_buddy_turn", "provider": "openrouter"}


def test_end_sends_v2_usage_field_not_v3_usage_details(tracer: LangfuseTracer) -> None:
    span = tracer.start_trace(route_name="r", provider="p", model="m")
    span.end(latency_s=0.5, input_tokens=8, output_tokens=33)

    end_kwargs = tracer._client.last_generation.end_kwargs
    assert end_kwargs is not None
    # The v2-server contract: `usage=` with input/output/unit.
    assert end_kwargs["usage"] == {"input": 8, "output": 33, "unit": "TOKENS"}
    # The v3-only kwarg the v2 server silently drops must NOT come back.
    assert "usage_details" not in end_kwargs
    assert end_kwargs["metadata"] == {"latency_s": 0.5, "error": False}
    assert end_kwargs["level"] == "DEFAULT"


def test_end_without_tokens_sends_no_usage_and_flags_errors(tracer: LangfuseTracer) -> None:
    span = tracer.start_trace(route_name="r", provider="p", model="m")
    span.end(latency_s=1.0, error=True)

    end_kwargs = tracer._client.last_generation.end_kwargs
    assert end_kwargs["usage"] is None
    assert end_kwargs["level"] == "ERROR"


def test_end_swallows_sdk_exceptions(tracer: LangfuseTracer) -> None:
    """Trace finalization must never take down a turn (the try/except in
    `_end`) — an SDK failure is logged, not raised."""

    class ExplodingGeneration(FakeGeneration):
        def end(self, **kwargs) -> None:
            raise RuntimeError("simulated SDK failure")

    tracer._client.last_generation = ExplodingGeneration()
    span = tracer.start_trace(route_name="r", provider="p", model="m")
    span.end(latency_s=0.1, input_tokens=1, output_tokens=1)  # must not raise


def test_module_still_importable_without_langfuse_package() -> None:
    """The lazy import inside __init__ keeps the module import-safe."""
    assert hasattr(tracing_module, "LangfuseTracer")


# -- F-079 (architecture-reviewer ratification of CR-012): trace attributes
# the ACTUALLY-served model/cost, not the static route label -------------


def test_end_overrides_model_when_provider_reports_a_served_model(tracer: LangfuseTracer) -> None:
    """A LiteLLM-served call must correct the trace's `model` at end() time
    to the real Gemini model — otherwise Gemini spend is misattributed as
    Claude in the Langfuse cost/model dashboard (the exact defect F-079
    flags)."""
    span = tracer.start_trace(
        route_name="tap_buddy_turn", provider="openrouter", model="anthropic/claude-sonnet-5"
    )
    span.end(
        latency_s=0.5,
        input_tokens=8,
        output_tokens=31,
        served_model="gemini-2.5-flash",
        served_cost_usd=0.0001731,
    )

    end_kwargs = tracer._client.last_generation.end_kwargs
    assert end_kwargs is not None
    assert end_kwargs["model"] == "gemini-2.5-flash"
    assert end_kwargs["metadata"]["served_model"] == "gemini-2.5-flash"
    assert end_kwargs["metadata"]["served_cost_usd"] == 0.0001731


def test_end_leaves_model_unset_when_no_served_model_reported(tracer: LangfuseTracer) -> None:
    """A real-OpenRouter call (no LiteLLM header detected) must NOT pass
    `model=` on end() -- the generation keeps the label set at start_trace,
    which is already correct in that path."""
    span = tracer.start_trace(
        route_name="r", provider="openrouter", model="anthropic/claude-sonnet-5"
    )
    span.end(latency_s=0.5, input_tokens=8, output_tokens=31)

    end_kwargs = tracer._client.last_generation.end_kwargs
    assert "model" not in end_kwargs
    assert "served_model" not in end_kwargs["metadata"]
    assert "served_cost_usd" not in end_kwargs["metadata"]
