"""AC-1/AC-1b/AC-4: ModelRouter resolution + shared-interface tracing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.llm.router import ModelRouter, ProviderNotRegisteredError
from app.llm.routes import UnknownRouteError
from app.llm.tracing import RecordingTracer
from app.llm.types import ModelResponse, RouteConfig, ToolCall


class FakeProvider:
    """A minimal Provider double — implements the shared interface
    (generate(route, messages, tools) -> ModelResponse) with a canned
    response, so the router/tracing tests never need a real network call.
    """

    def __init__(self, name: str, response: ModelResponse | None = None, raise_error: bool = False):
        self.name = name
        self._response = response or ModelResponse(text="hello", input_tokens=10, output_tokens=5)
        self._raise_error = raise_error
        self.calls: list[tuple[RouteConfig, list[dict[str, str]], list[dict[str, Any]]]] = []

    async def generate(
        self, route: RouteConfig, messages: list[dict[str, str]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        self.calls.append((route, messages, tools))
        if self._raise_error:
            raise RuntimeError("simulated provider failure")
        return self._response


ROUTES = {
    "test_route": RouteConfig(name="test_route", provider="openrouter", model="fake-model-v1"),
}


@pytest.mark.asyncio
async def test_model_route_resolves_via_model_router_no_hardcoded_string() -> None:
    """AC-1(a): the call site only ever passes a route NAME; the model
    string lives in MODEL_ROUTES/RouteConfig, resolved by ModelRouter.
    """
    provider = FakeProvider("openrouter")
    router = ModelRouter(providers={"openrouter": provider}, routes=ROUTES)

    response = await router.generate("test_route", [{"role": "user", "content": "hi"}])

    assert response.text == "hello"
    assert len(provider.calls) == 1
    resolved_route, messages, tools = provider.calls[0]
    assert resolved_route.model == "fake-model-v1"
    assert messages == [{"role": "user", "content": "hi"}]
    assert tools == []


@pytest.mark.asyncio
async def test_unknown_route_raises_instead_of_falling_back_to_a_default_model() -> None:
    provider = FakeProvider("openrouter")
    router = ModelRouter(providers={"openrouter": provider}, routes=ROUTES)

    with pytest.raises(UnknownRouteError):
        await router.generate("nonexistent_route", [])


@pytest.mark.asyncio
async def test_route_naming_unregistered_provider_raises() -> None:
    """A route naming a registered-set provider (per ADR-011's evidence gate,
    a real docs/benchmarks/ artifact) that this ModelRouter instance simply
    hasn't been given an adapter for still fails distinctly from an unknown
    route or a missing-evidence route.
    """
    benchmark_path = Path(__file__).parent / "fixtures" / "fake-benchmark.md"
    routes = {
        "orphan": RouteConfig(
            name="orphan", provider="vertex", model="m", selected_by=str(benchmark_path)
        )
    }
    router = ModelRouter(providers={"openrouter": FakeProvider("openrouter")}, routes=routes)

    with pytest.raises(ProviderNotRegisteredError):
        await router.generate("orphan", [])


@pytest.mark.asyncio
async def test_provider_adapters_share_generate_interface_and_trace_identically() -> None:
    """AC-1b: a trace is recorded through the shared ModelRouter interface,
    not per-provider bespoke tracing code — same tracer, same call shape,
    regardless of which provider served the route.
    """
    tracer = RecordingTracer()
    benchmark_path = str(Path(__file__).parent / "fixtures" / "fake-benchmark.md")
    provider_a = FakeProvider("fake_a", ModelResponse(text="a", input_tokens=1, output_tokens=2))
    provider_b = FakeProvider("fake_b", ModelResponse(text="b", input_tokens=3, output_tokens=4))
    routes = {
        "route_a": RouteConfig(
            name="route_a", provider="fake_a", model="model-a", selected_by=benchmark_path
        ),
        "route_b": RouteConfig(
            name="route_b", provider="fake_b", model="model-b", selected_by=benchmark_path
        ),
    }
    router = ModelRouter(
        providers={"fake_a": provider_a, "fake_b": provider_b}, routes=routes, tracer=tracer
    )

    await router.generate("route_a", [])
    await router.generate("route_b", [])

    assert len(tracer.calls) == 2
    call_a, call_b = tracer.calls
    assert call_a.route_name == "route_a"
    assert call_a.provider == "fake_a"
    assert call_a.model == "model-a"
    assert call_a.input_tokens == 1
    assert call_a.output_tokens == 2
    assert call_a.error is False
    assert call_b.route_name == "route_b"
    assert call_b.provider == "fake_b"


@pytest.mark.asyncio
async def test_trace_recorded_with_error_flag_on_provider_failure() -> None:
    tracer = RecordingTracer()
    provider = FakeProvider("openrouter", raise_error=True)
    router = ModelRouter(providers={"openrouter": provider}, routes=ROUTES, tracer=tracer)

    with pytest.raises(RuntimeError):
        await router.generate("test_route", [])

    assert len(tracer.calls) == 1
    assert tracer.calls[0].error is True


@pytest.mark.asyncio
async def test_generate_forwards_served_model_and_cost_to_the_trace() -> None:
    """F-079 (architecture-reviewer ratification of CR-012): when a provider
    reports the call was actually served by a different model than the
    static route label (the LiteLLM->Vertex/Gemini case), ModelRouter must
    forward that onto the trace, not silently drop it.
    """
    tracer = RecordingTracer()
    provider = FakeProvider(
        "openrouter",
        ModelResponse(
            text="Pong.",
            input_tokens=8,
            output_tokens=31,
            served_model="gemini-2.5-flash",
            served_cost_usd=0.0001731,
        ),
    )
    router = ModelRouter(providers={"openrouter": provider}, routes=ROUTES, tracer=tracer)

    await router.generate("test_route", [])

    assert len(tracer.calls) == 1
    assert tracer.calls[0].served_model == "gemini-2.5-flash"
    assert tracer.calls[0].served_cost_usd == pytest.approx(0.0001731)


@pytest.mark.asyncio
async def test_generate_leaves_served_model_none_when_provider_reports_none() -> None:
    tracer = RecordingTracer()
    provider = FakeProvider("openrouter")  # default ModelResponse has served_model=None
    router = ModelRouter(providers={"openrouter": provider}, routes=ROUTES, tracer=tracer)

    await router.generate("test_route", [])

    assert tracer.calls[0].served_model is None
    assert tracer.calls[0].served_cost_usd is None


@pytest.mark.asyncio
async def test_tool_calls_pass_through_response() -> None:
    provider = FakeProvider(
        "openrouter",
        ModelResponse(text="", tool_calls=[ToolCall(name="get_next_content", arguments={})]),
    )
    router = ModelRouter(providers={"openrouter": provider}, routes=ROUTES)

    response = await router.generate("test_route", [], tools=[{"name": "get_next_content"}])

    assert response.tool_calls[0].name == "get_next_content"
    _, _, tools_passed = provider.calls[0]
    assert tools_passed == [{"name": "get_next_content"}]


# -- build_default_model_router base_url wiring (CR-012: LiteLLM gateway --
# is the real production transport, not a local-only override) -----------


def test_build_default_router_targets_real_openrouter_by_default() -> None:
    """base_url=None (and settings.openrouter_base_url empty -> None at the
    call sites) must target the real OpenRouter URL — guards against the
    LiteLLM-gateway override silently becoming the default (same discipline
    as GLIFIC_HOST_HEADER_OVERRIDE's absent-by-default test).
    """
    from app.llm.router import build_default_model_router
    from app.llm.tracing import NullTracer

    router = build_default_model_router("test-key", tracer=NullTracer())
    provider = router._providers["openrouter"]
    assert str(provider._client.base_url).startswith("https://openrouter.ai/api/v1")


def test_build_default_router_base_url_override_reaches_the_client() -> None:
    """A non-None base_url (wired from Settings.openrouter_base_url) must
    actually reach the OpenAI-compatible client — this is what points the
    adapter at the LiteLLM gateway (CR-012: the real production transport
    for `tap_buddy_turn`, serving Gemini via Vertex AI, in every
    environment, not just local dev).
    """
    from app.llm.router import build_default_model_router
    from app.llm.tracing import NullTracer

    router = build_default_model_router(
        "test-key", tracer=NullTracer(), base_url="http://litellm:4000"
    )
    provider = router._providers["openrouter"]
    assert str(provider._client.base_url).startswith("http://litellm:4000")
