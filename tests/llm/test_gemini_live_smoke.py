"""CR-012 AC-4: a live smoke turn through the production-shaped config
(`ModelRouter` -> `OpenRouterProvider` pointed at the LiteLLM gateway ->
Vertex AI `gemini-2.5-flash`) returns a REAL completion with a populated
Langfuse trace (`route=tap_buddy_turn`) — the F-020-class evidence bar,
re-confirmed for CR-012's "real path, not a stopgap" claim (L-009/L-010
floor before `implemented`; not run by default, mirrors
`tests/delivery/test_glific_client.py::test_live_glific_smoke`'s
skip-if-unconfigured pattern for CR-012's own Test plan item 2).

Needs, from the environment the test process actually runs in:
- `OPENROUTER_BASE_URL` pointed at a reachable LiteLLM gateway (the
  compose-local one, `http://litellm:4000`, by default in `.env`).
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` for a
  reachable Langfuse project (compose-local `http://langfuse:3000`).
- The `litellm` compose service itself configured with real GCP creds
  (`GCP_SA_KEY_PATH` in `.env`) so it can actually reach Vertex AI.

Run via `docker compose run --rm app pytest -m integration
tests/llm/test_gemini_live_smoke.py` (or the full `-m integration` suite)
-- NOT the bare `docker run --rm tap-agent-service:dev pytest` form used
for the default gate, since that container has no network path to the
`litellm`/`langfuse` compose services.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import httpx
import pytest

from app.config.settings import get_settings
from app.llm.router import build_default_model_router
from app.llm.tracing import LangfuseTracer


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_gemini_turn_via_litellm_gateway_traces_to_langfuse() -> None:
    settings = get_settings()
    if not settings.openrouter_base_url:
        pytest.skip(
            "set OPENROUTER_BASE_URL (e.g. http://litellm:4000) to run this "
            "smoke test against the LiteLLM -> Vertex/Gemini gateway"
        )
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        pytest.skip("set LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY to verify the trace lands")

    started_at = datetime.now(UTC)

    tracer = LangfuseTracer()
    router = build_default_model_router(
        settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        tracer=tracer,
    )

    response = await router.generate(
        "tap_buddy_turn",
        [{"role": "user", "content": "Reply with exactly one word: pong"}],
    )
    tracer.flush()

    # Real Gemini completion (via LiteLLM->Vertex), not a canned/mocked
    # value -- non-empty text AND real, non-trivial usage counts prove a
    # network round trip actually happened.
    assert response.text.strip()
    assert response.input_tokens is not None and response.input_tokens > 0
    assert response.output_tokens is not None and response.output_tokens > 0

    # Populated Langfuse trace, route=tap_buddy_turn: query the self-hosted
    # Langfuse's own public API rather than trusting the SDK call didn't
    # raise -- per L-009, the SERVER's accepted shape is the real contract
    # (a v3-shaped `usage_details` payload was silently dropped by the v2
    # server in exactly this module, see LangfuseTracer._end's docstring).
    trace = None
    deadline = time.monotonic() + 15
    async with httpx.AsyncClient(
        base_url=settings.langfuse_host,
        auth=(settings.langfuse_public_key, settings.langfuse_secret_key),
        timeout=10.0,
    ) as client:
        while time.monotonic() < deadline and trace is None:
            resp = await client.get(
                "/api/public/traces", params={"name": "tap_buddy_turn", "limit": 5}
            )
            resp.raise_for_status()
            for candidate in resp.json()["data"]:
                timestamp = datetime.fromisoformat(candidate["timestamp"].replace("Z", "+00:00"))
                if timestamp >= started_at:
                    trace = candidate
                    break
            if trace is None:
                await asyncio.sleep(1)

    assert trace is not None, (
        "no tap_buddy_turn trace created after this test started appeared in "
        "Langfuse within 15s -- either the LangfuseTracer flush didn't land "
        "or the trace API's `name` filter/response shape changed (L-009)"
    )
    assert trace["name"] == "tap_buddy_turn"
