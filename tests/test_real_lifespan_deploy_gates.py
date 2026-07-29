"""CR-011 / ADR-013 GL-005-a + GL-005-b: proof that the REAL `app/main.py`
`lifespan` — not just a direct call to `resolve_langfuse_host_gate`/
`resolve_lms_write_target_gate` — actually invokes both boot gates, in the
right order (after the CR-010 contact allowlist, before any client is
constructed), and that the real app genuinely fails to boot when either
precondition is unmet.

Mirrors `tests/test_real_lifespan_contact_allowlist.py`'s idiom exactly
(architecture-reviewer C-6.2 / L-008 / L-011): exercises `app.main.lifespan`
directly against a plain FastAPI probe app, not a fixture override.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

import app.main as main_module
from app.config.settings import Settings

SELF_HOSTED_LANGFUSE_HOST = "http://langfuse:3000"
CLOUD_LANGFUSE_DEFAULT = "https://cloud.langfuse.com"
EVALIX_LMS_URL = "https://lms.evalix.xyz"
LOCAL_GLIFIC_API_URL = "http://host.docker.internal:4000"


def _production_settings(**overrides: object) -> Settings:
    """A fully-valid, boot-clean PRODUCTION config: CR-009's webhook secret
    and CR-010's allowlist are both explicitly set (production is not
    'local', so neither gate's own local-only fallback applies) — isolates
    any boot failure in these tests to the GL-005-a/GL-005-b gates
    specifically, not an unrelated prior gate.
    """
    defaults: dict[str, object] = {
        "environment": "production",
        "glific_webhook_secret": "a-real-configured-production-secret",
        "glific_contact_allowlist": "101,102",
        "glific_api_url": "https://api.tap.glific.com",
        "langfuse_host": SELF_HOSTED_LANGFUSE_HOST,
        "frappe_api_url": EVALIX_LMS_URL,
        "redis_url": "redis://localhost:6379/0",
        "tap_lms_api_key": "test-key",
        "openrouter_api_key": "test-key",
        "glific_phone": "917834811114",
        "glific_password": "Secret1234!",
        "_env_file": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_real_app_fails_to_boot_in_production_with_cloud_langfuse_host(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """GL-005-a reject case at the real-lifespan level: a production boot
    whose `LANGFUSE_HOST` is still the baked `cloud.langfuse.com` default
    must raise `RuntimeError` before `yield` — before any client is
    constructed.
    """
    settings = _production_settings(langfuse_host=CLOUD_LANGFUSE_DEFAULT)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    probe_app = FastAPI()

    with pytest.raises(RuntimeError, match="LANGFUSE_HOST"):
        async with main_module.lifespan(probe_app):
            pytest.fail("lifespan must raise before yielding control")

    assert not hasattr(probe_app.state, "redis")


@pytest.mark.asyncio
async def test_real_app_fails_to_boot_in_production_with_non_evalix_frappe_url(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """GL-005-b reject case at the real-lifespan level: a production boot
    whose `FRAPPE_API_URL` points at anything other than `lms.evalix.xyz`
    must raise `RuntimeError` before `yield` — before any client is
    constructed. The Langfuse gate is left passing (self-hosted) so the
    failure is unambiguously attributable to the LMS-write-target gate.
    """
    settings = _production_settings(
        frappe_api_url="https://tap-lms-production.example.org",
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    probe_app = FastAPI()

    with pytest.raises(RuntimeError, match="FRAPPE_API_URL"):
        async with main_module.lifespan(probe_app):
            pytest.fail("lifespan must raise before yielding control")

    assert not hasattr(probe_app.state, "redis")


@pytest.mark.asyncio
async def test_real_app_fails_to_boot_in_production_with_flow_orchestrator_enabled(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """CR-014/ADR-015 BC-4 reject case at the real-lifespan level: a
    production boot with `flow_orchestrator_enabled=True` must raise
    `RuntimeError` before `yield` — before any client is constructed. The
    other two GL-005 gates are left passing so the failure is unambiguously
    attributable to the flow-orchestrator gate.
    """
    settings = _production_settings(flow_orchestrator_enabled=True)
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    probe_app = FastAPI()

    with pytest.raises(RuntimeError, match="flow_orchestrator_enabled"):
        async with main_module.lifespan(probe_app):
            pytest.fail("lifespan must raise before yielding control")

    assert not hasattr(probe_app.state, "redis")


@pytest.mark.asyncio
async def test_real_app_boots_clean_in_production_with_self_hosted_langfuse_and_evalix_lms(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """Pass case at the real-lifespan level: a production boot with a
    self-hosted `LANGFUSE_HOST` AND `FRAPPE_API_URL` pointed at the evalix
    restore boots clean through both new gates (and every prior boot gate)
    — startup completes and clients are constructed.
    """
    settings = _production_settings()
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    probe_app = FastAPI()

    async with main_module.lifespan(probe_app):
        # Ordering sanity check: by the time `yield` returns control here,
        # every boot gate has passed AND clients exist.
        assert probe_app.state.redis is not None
        assert probe_app.state.contact_allowlist.tokens == {"101", "102"}
