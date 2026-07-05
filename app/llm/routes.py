"""`MODEL_ROUTES` config (ADR-011 final shape).

Every route entry is an object: `provider` (defaults to `"openrouter"` if
absent), `model` (provider-specific identifier, never referenced as a
literal outside this module), `selected_by` (required IFF `provider !=
"openrouter"` — a path to a dated benchmark artifact under
`docs/benchmarks/`), and an optional `fallback` `{provider, model}` pair.

This CR (CR-004) does not choose a non-default provider for any route —
every entry below defaults to `"openrouter"`. `tap_buddy_turn` is the first
route, ported from tap-buddy's single-Vertex-client main turn loop
(`ADR-010` R-1). `intent_classification`/`escalation_reasoning` are declared
per AD-002's Interfaces & schemas section but have no call site in this port
(WBS 1.8/1.16-18 remain open, separate work) — declaring them here now
costs nothing and keeps the config surface AD-002 documents accurate;
`ModelRouter.resolve` only requires a route to exist when a caller actually
asks for it.
"""

from __future__ import annotations

from app.llm.types import FallbackRoute, RouteConfig

MODEL_ROUTES: dict[str, RouteConfig] = {
    "tap_buddy_turn": RouteConfig(
        name="tap_buddy_turn",
        provider="openrouter",
        model="anthropic/claude-sonnet-5",
    ),
    "intent_classification": RouteConfig(
        name="intent_classification",
        provider="openrouter",
        model="anthropic/claude-haiku-4.5",
    ),
    "escalation_reasoning": RouteConfig(
        name="escalation_reasoning",
        provider="openrouter",
        model="anthropic/claude-opus-4.8",
    ),
}


class UnknownRouteError(KeyError):
    """Raised when `ModelRouter.resolve` is asked for a route key that does
    not exist in `MODEL_ROUTES` — never silently falls back to a default
    model string (that would reintroduce a hardcoded-model-string escape
    hatch, L-004).
    """


class InvalidRouteConfigError(ValueError):
    """Raised when a `MODEL_ROUTES` entry violates ADR-011's evidence gate:
    a non-default `provider` with no resolvable `selected_by` reference.
    """


def validate_model_routes(routes: dict[str, RouteConfig]) -> None:
    """ADR-011's evidence gate, made mechanical: every route whose
    `provider` is not `"openrouter"` MUST carry a `selected_by` reference to
    a real, readable file. For this CR every route defaults to
    `"openrouter"`, so this is trivially satisfied today — but the check
    itself must exist and run so a future non-default-provider route added
    without evidence is caught (AC-1(b)).
    """
    import os

    for key, route in routes.items():
        if route.provider == "openrouter":
            continue
        if not route.selected_by:
            raise InvalidRouteConfigError(
                f"MODEL_ROUTES[{key!r}] sets provider={route.provider!r} but has no "
                "selected_by — ADR-011 requires a benchmark artifact reference for any "
                "non-default provider."
            )
        if not os.path.isfile(route.selected_by):
            raise InvalidRouteConfigError(
                f"MODEL_ROUTES[{key!r}]'s selected_by={route.selected_by!r} does not "
                "resolve to a real file — ADR-011's evidence gate requires a dated, "
                "reviewable benchmark artifact, not a dangling reference."
            )


# Re-exported for readability at call sites that only need the fallback
# shape, not the whole routes module.
__all__ = [
    "MODEL_ROUTES",
    "FallbackRoute",
    "InvalidRouteConfigError",
    "UnknownRouteError",
    "validate_model_routes",
]
