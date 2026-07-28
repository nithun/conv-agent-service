"""`OpenRouterProvider` — the OpenAI-compatible adapter, and F-079's
served-model/served-cost header extraction (architecture-reviewer
ratification of CR-012).

Mocks at the httpx transport layer (respx), same pattern as
`tests/delivery/test_glific_client.py` — this exercises the REAL
`AsyncOpenAI` request/response path (including `.with_raw_response`), not
just the two helper functions in isolation.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.llm.providers.openrouter import (
    OpenRouterProvider,
    _extract_litellm_response_cost,
    _extract_litellm_served_model,
)
from app.llm.types import RouteConfig

ROUTE = RouteConfig(name="tap_buddy_turn", provider="openrouter", model="anthropic/claude-sonnet-5")


def _completion_body(model_echo: str = "anthropic/claude-sonnet-5") -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1785219938,
        "model": model_echo,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Pong.", "tool_calls": None},
            }
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 31, "total_tokens": 39},
    }


# -- header-extraction helpers (unit-level) --------------------------------


def test_extract_served_model_parses_the_litellm_api_base_header() -> None:
    headers = httpx.Headers(
        {
            "x-litellm-model-api-base": (
                "us-central1-aiplatform.googleapis.com/v1/projects/tap-buddy-mvp/"
                "locations/us-central1/publishers/google/models/gemini-2.5-flash:generateContent"
            )
        }
    )
    assert _extract_litellm_served_model(headers) == "gemini-2.5-flash"


def test_extract_served_model_returns_none_when_header_absent() -> None:
    assert _extract_litellm_served_model(httpx.Headers({})) is None


def test_extract_response_cost_parses_float() -> None:
    headers = httpx.Headers({"x-litellm-response-cost": "0.0001731"})
    assert _extract_litellm_response_cost(headers) == pytest.approx(0.0001731)


def test_extract_response_cost_returns_none_when_absent_or_unparseable() -> None:
    assert _extract_litellm_response_cost(httpx.Headers({})) is None
    assert _extract_litellm_response_cost(httpx.Headers({"x-litellm-response-cost": "n/a"})) is None


# -- OpenRouterProvider.generate end-to-end (respx-mocked transport) -------


@pytest.mark.asyncio
@respx.mock
async def test_generate_populates_served_model_and_cost_from_litellm_headers() -> None:
    """The LiteLLM path (CR-012's real production transport): the JSON body
    echoes the requested alias, but the response headers carry the real
    served model + cost, and `generate()` must surface both on
    `ModelResponse` (F-079) so the trace can attribute them correctly.
    """
    respx.post("http://litellm:4000/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=_completion_body(model_echo="anthropic/claude-sonnet-5"),
            headers={
                "x-litellm-model-api-base": (
                    "us-central1-aiplatform.googleapis.com/v1/projects/tap-buddy-mvp/"
                    "locations/us-central1/publishers/google/models/"
                    "gemini-2.5-flash:generateContent"
                ),
                "x-litellm-response-cost": "0.0001731",
            },
        )
    )
    provider = OpenRouterProvider(api_key="sk-litellm-master-key", base_url="http://litellm:4000")

    response = await provider.generate(ROUTE, [{"role": "user", "content": "say pong"}], [])

    assert response.text == "Pong."
    assert response.input_tokens == 8
    assert response.output_tokens == 31
    assert response.served_model == "gemini-2.5-flash"
    assert response.served_cost_usd == pytest.approx(0.0001731)


@pytest.mark.asyncio
@respx.mock
async def test_generate_leaves_served_model_none_for_real_openrouter() -> None:
    """A real OpenRouter response carries no `x-litellm-*` headers -- both
    fields must stay None so the trace keeps trusting `route.model`
    unchanged (no false "served_model" ever invented for the real path).
    """
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_completion_body())
    )
    provider = OpenRouterProvider(api_key="sk-real-openrouter-key")

    response = await provider.generate(ROUTE, [{"role": "user", "content": "say pong"}], [])

    assert response.served_model is None
    assert response.served_cost_usd is None
