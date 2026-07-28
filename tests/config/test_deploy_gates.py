"""CR-011 / ADR-013 GL-005-a (Langfuse egress, F-070) + GL-005-b
(LMS-write-target) boot-time fail-close gates: isolated tests for
`resolve_langfuse_host_gate`/`resolve_lms_write_target_gate` directly
against `Settings(...)`, mirroring
`tests/test_lifespan_contact_allowlist.py`'s direct-construction
convention (these are the exact functions `app/main.py`'s `lifespan` calls,
before constructing any client).

`tests/test_real_lifespan_deploy_gates.py` covers the REAL `lifespan`
wiring proof (mirrors CR-010's own C-6.2 idiom) — this file only proves the
gate functions' own logic.
"""

from __future__ import annotations

import pytest

from app.config.deploy_gates import resolve_langfuse_host_gate, resolve_lms_write_target_gate
from app.config.settings import Settings

SELF_HOSTED_LANGFUSE_HOST = "http://langfuse:3000"
CLOUD_LANGFUSE_DEFAULT = "https://cloud.langfuse.com"
EVALIX_LMS_URL = "https://lms.evalix.xyz"


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "environment": "production",
        "langfuse_host": SELF_HOSTED_LANGFUSE_HOST,
        "frappe_api_url": EVALIX_LMS_URL,
        "_env_file": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


# ---- GL-005-a: Langfuse egress fail-close gate (F-070) ---------------------


def test_langfuse_gate_rejects_production_with_baked_cloud_default() -> None:
    """Reject case: environment='production' + the field's own baked
    default (`https://cloud.langfuse.com`, never explicitly overridden) ->
    RuntimeError.
    """
    settings = _settings(langfuse_host=CLOUD_LANGFUSE_DEFAULT)

    with pytest.raises(RuntimeError, match="LANGFUSE_HOST"):
        resolve_langfuse_host_gate(settings)


def test_langfuse_gate_rejects_production_with_any_other_external_host() -> None:
    """Reject case: environment='production' + some OTHER external host
    (not the baked default, not self-hosted either) -> RuntimeError.
    """
    settings = _settings(langfuse_host="https://my-langfuse.example.com")

    with pytest.raises(RuntimeError, match="LANGFUSE_HOST"):
        resolve_langfuse_host_gate(settings)


def test_langfuse_gate_passes_production_with_self_hosted_host() -> None:
    """Pass case: environment='production' + the compose-local self-hosted
    value -> no raise.
    """
    settings = _settings(langfuse_host=SELF_HOSTED_LANGFUSE_HOST)

    resolve_langfuse_host_gate(settings)  # must not raise


@pytest.mark.parametrize("environment", ["local", "staging", ""])
def test_langfuse_gate_passes_any_non_production_environment_regardless_of_host(
    environment: str,
) -> None:
    """Pass case: this gate is production-ONLY (CR-011's own pinned
    asymmetry vs. CR-009/CR-010's `!= 'local'` shape) — any other
    environment value, even with the external cloud default still in
    place, must not raise.
    """
    settings = _settings(environment=environment, langfuse_host=CLOUD_LANGFUSE_DEFAULT)

    resolve_langfuse_host_gate(settings)  # must not raise


def test_langfuse_gate_rejects_production_with_unset_langfuse_host() -> None:
    """Unset counts as NOT self-hosted -- must not be treated as a pass."""
    settings = _settings(langfuse_host="")

    with pytest.raises(RuntimeError, match="LANGFUSE_HOST"):
        resolve_langfuse_host_gate(settings)


# ---- GL-005-b: LMS-write-target fail-close gate -----------------------------


def test_lms_write_target_gate_rejects_production_with_non_evalix_host() -> None:
    """Reject case: environment='production' + a plausible-looking
    live-prod-style hostname (L-017 adversarial fixture, not an obviously
    fake placeholder) -> RuntimeError.
    """
    settings = _settings(frappe_api_url="https://tap-lms-production.example.org")

    with pytest.raises(RuntimeError, match="FRAPPE_API_URL"):
        resolve_lms_write_target_gate(settings)


def test_lms_write_target_gate_rejects_production_with_unset_frappe_api_url() -> None:
    """Reject case: unset must never be treated as "not production, skip
    the check" -- mirrors the other gates' unset-is-not-a-pass discipline.
    """
    settings = _settings(frappe_api_url="")

    with pytest.raises(RuntimeError, match="FRAPPE_API_URL"):
        resolve_lms_write_target_gate(settings)


@pytest.mark.parametrize(
    "url",
    [
        "https://lms.evalix.xyz",
        "http://lms.evalix.xyz",
        "https://lms.evalix.xyz/api/method/foo",
    ],
)
def test_lms_write_target_gate_passes_production_with_evalix_host(url: str) -> None:
    """Pass case: environment='production' + any scheme/path variant of
    the evalix restore host -> no raise.
    """
    settings = _settings(frappe_api_url=url)

    resolve_lms_write_target_gate(settings)  # must not raise


@pytest.mark.parametrize("environment", ["local", "staging", ""])
def test_lms_write_target_gate_passes_any_non_production_environment(environment: str) -> None:
    settings = _settings(
        environment=environment, frappe_api_url="https://tap-lms-production.example.org"
    )

    resolve_lms_write_target_gate(settings)  # must not raise


def test_both_gates_are_pure_functions_no_client_side_effects() -> None:
    """Documents/asserts the ordering contract `app/main.py`'s `lifespan`
    relies on: neither gate makes a Redis/Glific/Frappe/LLM call or
    constructs a client -- calling both before any client constructor is
    genuinely side-effect-free.
    """
    settings = _settings()

    resolve_langfuse_host_gate(settings)
    resolve_lms_write_target_gate(settings)


# ---- F-074: environment comparison is case/whitespace-normalized ------------
# A structural GL-005 gate must not silently no-op on a casing/whitespace typo
# in ENVIRONMENT (code-review 2026-07-27 Low).


@pytest.mark.parametrize(
    "environment", ["Production", "PRODUCTION", " production ", "production\n"]
)
def test_langfuse_gate_fires_on_casing_or_whitespace_variant_of_production(
    environment: str,
) -> None:
    settings = _settings(environment=environment, langfuse_host=CLOUD_LANGFUSE_DEFAULT)

    with pytest.raises(RuntimeError, match="LANGFUSE_HOST"):
        resolve_langfuse_host_gate(settings)


@pytest.mark.parametrize("environment", ["Production", " PRODUCTION "])
def test_lms_write_target_gate_fires_on_casing_variant_of_production(environment: str) -> None:
    settings = _settings(
        environment=environment, frappe_api_url="https://tap-lms-production.example.org"
    )

    with pytest.raises(RuntimeError, match="FRAPPE_API_URL"):
        resolve_lms_write_target_gate(settings)
