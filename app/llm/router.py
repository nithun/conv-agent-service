"""`ModelRouter` (ADR-011): resolves a `MODEL_ROUTES` key to a registered
provider adapter and dispatches `generate()` calls through it, with a
Langfuse trace wrapped around every call regardless of provider (ADR-005/
L-004) — the tracing wrapper lives here, at the router layer, not
duplicated per-provider-adapter (Task 2).

`ModelRouter.resolve(route_name)` dispatches directly to the route's
configured provider — there is no test/production split and no
"backend" parameter distinguishing environments (ADR-011 Decision,
"no special-casing").
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.llm.providers.base import Provider
from app.llm.routes import MODEL_ROUTES, UnknownRouteError, validate_model_routes
from app.llm.tracing import LangfuseTracer, NullTracer, Tracer
from app.llm.types import ModelResponse, RouteConfig

logger = logging.getLogger("app.llm.router")


class ProviderNotRegisteredError(KeyError):
    """Raised when a `MODEL_ROUTES` entry names a provider with no matching
    adapter registered on this `ModelRouter` instance.
    """


class ModelRouter:
    """Resolves `MODEL_ROUTES` keys to provider adapters and dispatches
    `generate()` calls, Langfuse-tracing every call identically regardless
    of which provider actually serves the route (ADR-011).

    `providers` maps a provider name (`"openrouter"`, future `"vertex"`,
    ...) to a constructed adapter implementing the shared `Provider`
    interface. `routes` defaults to the module-level `MODEL_ROUTES` config
    but is injectable for tests.

    NAMING NOTE (CR-012 AC-3): `"openrouter"` here names the OpenAI-
    compatible ADAPTER (`OpenRouterProvider`), not necessarily the vendor
    actually serving a call. `tap_buddy_turn`'s production configuration
    points that adapter's `base_url` at the self-hosted LiteLLM gateway,
    which serves Gemini via Vertex AI — see `build_default_model_router`
    below and `app/llm/providers/openrouter.py`'s module docstring.
    """

    def __init__(
        self,
        providers: dict[str, Provider],
        routes: dict[str, RouteConfig] | None = None,
        tracer: Tracer | None = None,
    ):
        self._routes = routes if routes is not None else MODEL_ROUTES
        validate_model_routes(self._routes)
        self._providers = providers
        self._tracer = tracer if tracer is not None else NullTracer()

    def resolve_route(self, route_name: str) -> RouteConfig:
        """Looks up a `MODEL_ROUTES` entry by name — never falls back to a
        default model string (L-004); an unknown route is a hard error.
        """
        try:
            return self._routes[route_name]
        except KeyError as exc:
            raise UnknownRouteError(route_name) from exc

    async def generate(
        self,
        route_name: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """Resolves `route_name` to its configured provider, dispatches
        `generate()`, and records a Langfuse trace (route, provider, model,
        tokens, latency) around the call — identically for every provider,
        per ADR-011's "multi-provider capable, not multi-blind-spot"
        framing. Call sites never construct a provider client directly and
        never see which provider actually served the route.
        """
        route = self.resolve_route(route_name)
        provider = self._providers.get(route.provider)
        if provider is None:
            raise ProviderNotRegisteredError(
                f"route {route_name!r} configures provider={route.provider!r}, "
                "but no adapter for that provider is registered on this ModelRouter"
            )

        tools = tools or []
        start = time.monotonic()
        trace = self._tracer.start_trace(
            route_name=route_name, provider=route.provider, model=route.model
        )
        try:
            response = await provider.generate(route, messages, tools)
        except Exception:
            trace.end(error=True, latency_s=time.monotonic() - start)
            raise
        trace.end(
            latency_s=time.monotonic() - start,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            # F-079: forward the provider's actually-served model/cost (when
            # it detected one) so the trace attributes real Gemini calls
            # correctly instead of the static route label — see
            # `app/llm/tracing.py`'s module docstring.
            served_model=response.served_model,
            served_cost_usd=response.served_cost_usd,
        )
        return response


def build_default_model_router(
    openrouter_api_key: str,
    tracer: Tracer | None = None,
    base_url: str | None = None,
) -> ModelRouter:
    """Convenience constructor wiring the one provider ADAPTER this port
    ships (the OpenAI-compatible `OpenRouterProvider`, ADR-011's default) —
    kept separate from `ModelRouter.__init__` so tests can construct a
    router with fake/mock providers without touching real credentials.

    `base_url=None` targets the real OpenRouter cloud URL. A non-None value
    (wired from `Settings.openrouter_base_url`) points the same adapter at a
    self-hosted OpenAI-compatible gateway instead — CR-012 (2026-07-28):
    this is how `tap_buddy_turn` reaches its REAL PRODUCTION transport, the
    LiteLLM gateway fronting Vertex AI Gemini (`docker-compose.yml`'s
    `litellm` service in every environment, not a local-only override).
    Real OpenRouter remains available (empty override) for any route that
    should NOT go through LiteLLM.
    """
    from app.llm.providers.openrouter import OPENROUTER_BASE_URL, OpenRouterProvider

    providers: dict[str, Provider] = {
        "openrouter": OpenRouterProvider(
            api_key=openrouter_api_key,
            base_url=base_url or OPENROUTER_BASE_URL,
        )
    }
    resolved_tracer = tracer if tracer is not None else LangfuseTracer()
    return ModelRouter(providers=providers, tracer=resolved_tracer)
