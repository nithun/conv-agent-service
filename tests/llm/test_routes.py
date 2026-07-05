"""AC-1: MODEL_ROUTES config + the evidence-gate static/runtime check."""

from __future__ import annotations

import pytest

from app.llm.routes import (
    MODEL_ROUTES,
    InvalidRouteConfigError,
    validate_model_routes,
)
from app.llm.types import RouteConfig


def test_every_route_defaults_to_openrouter_provider() -> None:
    """No route in this CR chooses a non-default provider (CR-004 scope)."""
    for route in MODEL_ROUTES.values():
        assert route.provider == "openrouter"


def test_tap_buddy_turn_route_exists_with_no_hardcoded_call_site_string() -> None:
    """AC-1: the ported main-turn-loop call resolves via MODEL_ROUTES, not a
    literal model string in business logic.
    """
    route = MODEL_ROUTES["tap_buddy_turn"]
    assert route.model == "anthropic/claude-sonnet-5"
    assert route.provider == "openrouter"
    assert route.selected_by is None  # default provider needs no evidence


def test_default_provider_routes_pass_validation() -> None:
    validate_model_routes(MODEL_ROUTES)


def test_non_default_provider_route_requires_selected_by() -> None:
    """AC-1(b): a route with provider != "openrouter" and no selected_by
    fails to validate.
    """
    bad_routes = {
        "some_route": RouteConfig(name="some_route", provider="vertex", model="gemini-2.5-flash")
    }
    with pytest.raises(InvalidRouteConfigError):
        validate_model_routes(bad_routes)


def test_non_default_provider_route_with_dangling_selected_by_fails() -> None:
    """The evidence gate requires selected_by to resolve to a REAL file, not
    just be present as a string (ADR-011 evidence gate condition 3).
    """
    bad_routes = {
        "some_route": RouteConfig(
            name="some_route",
            provider="vertex",
            model="gemini-2.5-flash",
            selected_by="docs/benchmarks/does-not-exist.md",
        )
    }
    with pytest.raises(InvalidRouteConfigError):
        validate_model_routes(bad_routes)
