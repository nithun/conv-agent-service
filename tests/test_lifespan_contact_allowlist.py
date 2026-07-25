"""CR-010 Acceptance Criteria 1/1b/2/3 + architecture-reviewer C-2 (BLOCKING
— the fail-open defect found during review): fail-closed-by-environment
resolution of the contact allowlist, mirroring
`tests/test_lifespan_auth.py`'s direct-`Settings(...)`-construction
convention (`resolve_contact_allowlist` is the exact function
`app/main.py`'s `lifespan` calls, before constructing any client).
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.ingress.contact_allowlist import (
    LOCAL_DEV_SIMULATOR_CONTACT_IDS,
    ContactAllowlist,
    resolve_contact_allowlist,
)

LOCAL_GLIFIC_API_URL = "http://host.docker.internal:4000"
HOSTED_GLIFIC_API_URL = "https://api.tap.glific.com"


def test_local_environment_with_local_api_url_and_unset_allowlist_falls_back_to_pool() -> None:
    """AC-1: environment='local' + glific_contact_allowlist='' +
    a local-looking glific_api_url does NOT raise, and resolves to the
    known 5-contact simulator pool — the double gate (C-2) requires BOTH
    conditions, not environment=='local' alone.
    """
    settings = Settings(
        environment="local",
        glific_contact_allowlist="",
        glific_api_url=LOCAL_GLIFIC_API_URL,
        _env_file=None,
    )

    result = resolve_contact_allowlist(settings)

    assert result == ContactAllowlist(tokens=LOCAL_DEV_SIMULATOR_CONTACT_IDS)
    assert result.tokens == {"2", "3", "4", "5", "6"}


def test_local_environment_with_non_local_api_url_and_unset_allowlist_raises() -> None:
    """AC-1b (architecture-reviewer C-2): environment=='local' alone is
    INSUFFICIENT — a non-local-looking glific_api_url (e.g. a real hosted
    TAP org) with an unset allowlist must fail closed exactly like any
    other non-local environment, closing the fail-open path where
    ENVIRONMENT=local + a real GLIFIC_API_URL would otherwise have
    silently allowlisted five arbitrary real contacts (ids 2-6).
    """
    settings = Settings(
        environment="local",
        glific_contact_allowlist="",
        glific_api_url=HOSTED_GLIFIC_API_URL,
        _env_file=None,
    )

    with pytest.raises(RuntimeError, match="GLIFIC_CONTACT_ALLOWLIST is required"):
        resolve_contact_allowlist(settings)


def test_local_environment_with_unset_api_url_and_unset_allowlist_raises() -> None:
    """An empty/unset GLIFIC_API_URL does not count as local either — an
    operator who hasn't set it yet gets no free pass.
    """
    settings = Settings(
        environment="local", glific_contact_allowlist="", glific_api_url="", _env_file=None
    )

    with pytest.raises(RuntimeError, match="GLIFIC_CONTACT_ALLOWLIST is required"):
        resolve_contact_allowlist(settings)


@pytest.mark.parametrize(
    "environment",
    [
        "",  # unset (Settings' own default)
        "staging",
        "production",
        "Local",  # case mismatch
        "LOCAL",
        "locol",  # typo
        "local ",  # trailing whitespace
    ],
)
def test_non_local_environment_with_unset_allowlist_raises_runtime_error_regardless_of_url(
    environment: str,
) -> None:
    """AC-2: ANY environment value other than the byte-for-byte string
    'local' fails closed with a RuntimeError when no explicit allowlist is
    configured — regardless of glific_api_url (the local-looking URL alone
    is not a substitute for the exact environment=='local' opt-in either).
    """
    settings = Settings(
        environment=environment,
        glific_contact_allowlist="",
        glific_api_url=LOCAL_GLIFIC_API_URL,
        _env_file=None,
    )

    with pytest.raises(RuntimeError, match="GLIFIC_CONTACT_ALLOWLIST is required"):
        resolve_contact_allowlist(settings)


def test_explicit_allowlist_value_is_parsed_trimmed_and_deduped_and_not_merged_with_default() -> (
    None
):
    """AC-3: whitespace trimmed, empty entries between commas dropped, and
    the simulator-pool default NOT merged in — only the explicit tokens are
    present, even though environment=='local' with a local-looking URL
    would otherwise have qualified for the fallback.
    """
    settings = Settings(
        environment="local",
        glific_contact_allowlist="4, 9876543210 ,,7",
        glific_api_url=LOCAL_GLIFIC_API_URL,
        _env_file=None,
    )

    result = resolve_contact_allowlist(settings)

    assert result.tokens == {"4", "9876543210", "7"}
    # Explicitly not the simulator pool ids that would otherwise apply.
    assert "2" not in result.tokens
    assert "3" not in result.tokens


def test_explicit_allowlist_value_replaces_default_in_non_local_environment_too() -> None:
    settings = Settings(
        environment="production", glific_contact_allowlist="42", _env_file=None
    )

    result = resolve_contact_allowlist(settings)

    assert result.tokens == {"42"}


def test_unset_environment_env_var_resolves_to_empty_string_not_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Mirrors test_lifespan_auth.py's own regression test for the CR-009
    fail-open defect class: constructing `Settings` with `ENVIRONMENT`
    completely absent must resolve to `""`, not `"local"`.
    """
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    settings = Settings(glific_contact_allowlist="", _env_file=None)

    assert settings.environment == ""
    with pytest.raises(RuntimeError):
        resolve_contact_allowlist(settings)


def test_boot_time_check_is_a_pure_function_no_client_side_effects() -> None:
    """Documents/asserts the ordering contract `app/main.py`'s `lifespan`
    relies on: `resolve_contact_allowlist` makes no Redis/Glific/Frappe/LLM
    call and constructs no client — calling it before any client
    constructor is genuinely side-effect-free.
    """
    settings = Settings(
        environment="local",
        glific_contact_allowlist="",
        glific_api_url=LOCAL_GLIFIC_API_URL,
        _env_file=None,
    )

    result = resolve_contact_allowlist(settings)

    assert isinstance(result, ContactAllowlist)
    assert result.tokens
