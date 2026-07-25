"""Test for the /health liveness route."""

from __future__ import annotations

import httpx
import pytest

from app.ingress.contact_allowlist import verify_contact_allowlisted
from app.ingress.health import router as health_router
from app.ingress.webhook_auth import verify_glific_webhook_signature
from app.main import app


@pytest.mark.asyncio
async def test_health_returns_200_ok() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_route_has_no_webhook_signature_dependency() -> None:
    """AC-8 (CR-009): /health stays a dependency-free liveness probe — LB/
    uptime checks need this. Negative-claim reachability check (P-005
    idiom): asserts the new dependency is ABSENT, not merely that the route
    still returns 200 (which would be true even if the dependency were
    present but a valid signature happened to be supplied).
    """
    route = next(r for r in health_router.routes if r.path == "/health")  # type: ignore[attr-defined]
    dependency_callables = [dep.call for dep in route.dependant.dependencies]  # type: ignore[attr-defined]

    assert verify_glific_webhook_signature not in dependency_callables


def test_health_route_has_no_contact_allowlist_dependency() -> None:
    """AC-8 (CR-010): /health stays untouched, explicitly, per CR-010's own
    Scope of change — extends CR-009's negative-claim reachability check
    with the new dependency's absence too, in every `environment` value
    (this route carries no environment-conditional logic at all).
    """
    route = next(r for r in health_router.routes if r.path == "/health")  # type: ignore[attr-defined]
    dependency_callables = [dep.call for dep in route.dependant.dependencies]  # type: ignore[attr-defined]

    assert verify_contact_allowlisted not in dependency_callables
