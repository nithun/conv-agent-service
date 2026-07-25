"""Architecture-reviewer C-6.2 (verbatim: "the live-container check CR-009
ran, repeated for this gate") / L-008 / L-011: proof that the REAL
`app/main.py` `lifespan` — not just a direct call to
`resolve_contact_allowlist`, and not just a fixture that injects
`app.state.contact_allowlist` — actually resolves and assigns the contact
allowlist, and that the real app genuinely fails to boot when it's
unresolvable.

`tests/test_lifespan_contact_allowlist.py` and
`tests/ingress/conftest.py`'s fixture-injected `app.state.contact_allowlist`
can both pass even if `app/main.py`'s `lifespan` never assigned that state
at all — the resulting failure mode in production would be a 500 (an
`AttributeError` reading `request.app.state.contact_allowlist`), i.e.
fail-closed, but the *boot gate itself* would be entirely unproven. This
file closes that gap by exercising `app.main.lifespan` directly (FastAPI's
own async-context-manager shape — no `asgi-lifespan`/`httpx.ASGITransport`
lifespan support needed, mirroring how `app/main.py` itself invokes it).
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

import app.main as main_module
from app.config.settings import Settings
from app.ingress.contact_allowlist import ContactAllowlist

LOCAL_GLIFIC_API_URL = "http://host.docker.internal:4000"


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "environment": "local",
        "glific_webhook_secret": "",  # local fallback applies (CR-009)
        "glific_contact_allowlist": "",
        "glific_api_url": LOCAL_GLIFIC_API_URL,
        "redis_url": "redis://localhost:6379/0",
        "frappe_api_url": "",
        "tap_lms_api_key": "",
        "openrouter_api_key": "test-key",
        "glific_phone": "",
        "glific_password": "",
        "_env_file": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_real_lifespan_resolves_and_assigns_contact_allowlist(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """(a): boots the REAL `lifespan` context manager against a plain
    FastAPI app object (not a probe app, not a fixture override) and
    asserts `app.state.contact_allowlist` is a genuine `ContactAllowlist`
    instance populated by the real `resolve_contact_allowlist` code path —
    exercising the simulator-pool fallback branch end-to-end through the
    actual startup sequence.
    """
    settings = _settings()
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    probe_app = FastAPI()

    async with main_module.lifespan(probe_app):
        assert isinstance(probe_app.state.contact_allowlist, ContactAllowlist)
        assert probe_app.state.contact_allowlist.tokens == {"2", "3", "4", "5", "6"}
        assert probe_app.state.contact_allowlist_rejection_count == 0
        # Ordering contract: the allowlist is resolved before any client is
        # constructed, but by the time `yield` returns control here the
        # clients exist too — sanity-check startup actually completed.
        assert probe_app.state.redis is not None


@pytest.mark.asyncio
async def test_real_lifespan_resolves_explicit_allowlist_value(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(glific_contact_allowlist="101,102")
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    probe_app = FastAPI()

    async with main_module.lifespan(probe_app):
        assert probe_app.state.contact_allowlist.tokens == {"101", "102"}


@pytest.mark.asyncio
async def test_real_app_fails_to_boot_with_allowlist_unset_outside_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """(b): the live-container-class boot-failure check — with a VALID
    webhook secret (so signature resolution succeeds and the failure is
    unambiguously attributable to the contact allowlist gate) but an
    unset `GLIFIC_CONTACT_ALLOWLIST` outside `environment == 'local'`, the
    real `lifespan` context manager must raise `RuntimeError` before
    `yield` — before any client is constructed — never let the process
    come up looking healthy.
    """
    settings = _settings(
        environment="staging",
        glific_webhook_secret="a-real-configured-secret",
        glific_contact_allowlist="",
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    probe_app = FastAPI()

    with pytest.raises(RuntimeError, match="GLIFIC_CONTACT_ALLOWLIST is required"):
        async with main_module.lifespan(probe_app):
            pytest.fail("lifespan must raise before yielding control")

    # No client was ever constructed — the boot gate ran before any of
    # them, exactly like CR-009's own webhook-secret gate.
    assert not hasattr(probe_app.state, "redis")


@pytest.mark.asyncio
async def test_real_app_fails_to_boot_with_local_environment_but_hosted_api_url(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """The C-2 double-gate, proven at the real-lifespan level: environment
    == 'local' alone does not save an unset allowlist when GLIFIC_API_URL
    points at a real, hosted Glific org.
    """
    settings = _settings(
        environment="local",
        glific_webhook_secret="",
        glific_contact_allowlist="",
        glific_api_url="https://api.tap.glific.com",
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)

    probe_app = FastAPI()

    with pytest.raises(RuntimeError, match="GLIFIC_CONTACT_ALLOWLIST is required"):
        async with main_module.lifespan(probe_app):
            pytest.fail("lifespan must raise before yielding control")
