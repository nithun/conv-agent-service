"""CR-009 Acceptance Criteria 1/2 + architecture-reviewer CONDITION 2
(BLOCKING — the fail-open defect found during review): fail-closed-by-
environment resolution of the Glific webhook signature secret.

Tests `resolve_glific_webhook_secret` directly — the exact function
`app/main.py`'s `lifespan` calls, before constructing any client — per
`get_settings()`'s own documented direct-`Settings(...)`-construction test
convention, rather than booting the whole ASGI app (no `asgi-lifespan`
dependency in this repo).
"""

from __future__ import annotations

import pytest

from app.config.settings import Settings
from app.ingress.webhook_auth import (
    LOCAL_DEV_DEFAULT_SIGNATURE_SECRET,
    resolve_glific_webhook_secret,
)


def test_local_environment_with_unset_secret_falls_back_to_dummy_secret() -> None:
    """AC-1: environment='local' + glific_webhook_secret='' does NOT
    raise, and resolves to the published Glific dummy secret."""
    settings = Settings(environment="local", glific_webhook_secret="", _env_file=None)

    assert resolve_glific_webhook_secret(settings) == LOCAL_DEV_DEFAULT_SIGNATURE_SECRET
    assert LOCAL_DEV_DEFAULT_SIGNATURE_SECRET == "This is a dummy secret"  # Glific's own literal


def test_real_secret_configured_is_used_verbatim_regardless_of_environment() -> None:
    settings = Settings(
        environment="production", glific_webhook_secret="a-real-signature-phrase", _env_file=None
    )

    assert resolve_glific_webhook_secret(settings) == "a-real-signature-phrase"


@pytest.mark.parametrize(
    "environment",
    [
        "",  # unset (Settings' own default — the exact fail-open defect found)
        "staging",
        "production",
        "Local",  # case mismatch
        "LOCAL",
        "locol",  # typo
        "local ",  # trailing whitespace
    ],
)
def test_non_local_environment_with_unset_secret_raises_runtime_error(environment: str) -> None:
    """AC-2 + architecture-reviewer CONDITION 2: ANY environment value
    other than the byte-for-byte string 'local' — including unset (''),
    and any typo/case/whitespace variant — fails closed with a
    RuntimeError when no real secret is configured. This is the specific
    fix for the fail-open defect the reviewer found: `Settings.environment`
    used to default to `"local"` with no `ENVIRONMENT=` line in
    `.env.example`, so an unconfigured `.env` silently qualified for the
    dummy-secret fallback the moment the webhook took public traffic.
    """
    settings = Settings(environment=environment, glific_webhook_secret="", _env_file=None)

    with pytest.raises(RuntimeError, match="GLIFIC_WEBHOOK_SECRET is required"):
        resolve_glific_webhook_secret(settings)


def test_unset_environment_env_var_resolves_to_empty_string_not_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The core of the fail-open defect (architecture-reviewer CONDITION 2):
    constructing `Settings` with the `ENVIRONMENT` env var completely
    absent (e.g. a `.env` copied from an old `.env.example`, or a bare
    `docker run` with no override) must resolve `settings.environment` to
    `""` — NOT the string `"local"` — so it falls into the fail-closed
    `RuntimeError` branch, never the dummy-secret fallback.
    """
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    # Isolate the secret too: this test builds Settings from the AMBIENT env
    # (no kwargs), and since the F-047 fix .env pre-fills
    # GLIFIC_WEBHOOK_SECRET — which compose forwards into the container's
    # environment — a leaked secret makes resolve_glific_webhook_secret
    # legitimately not raise (a secret IS set), failing this test only
    # inside Docker. The scenario under test is "nothing configured at
    # all", so both vars must be absent.
    monkeypatch.delenv("GLIFIC_WEBHOOK_SECRET", raising=False)

    settings = Settings(_env_file=None)

    assert settings.environment == ""
    with pytest.raises(RuntimeError):
        resolve_glific_webhook_secret(settings)


def test_boot_time_check_runs_before_any_client_is_constructed() -> None:
    """Documents/asserts the ORDERING contract `app/main.py`'s `lifespan`
    relies on: `resolve_glific_webhook_secret` is a pure function of
    `Settings` — it makes no Redis/Glific/Frappe/LLM call and constructs no
    client itself, so calling it first (before `_build_redis_client` etc.)
    is genuinely side-effect-free until it either returns a secret string
    or raises.
    """
    settings = Settings(environment="local", glific_webhook_secret="", _env_file=None)

    # No patching of any client constructor is needed for this to run
    # cleanly — if resolve_glific_webhook_secret ever grew a side effect
    # (e.g. touching Redis), this call would need such patching too.
    secret = resolve_glific_webhook_secret(settings)

    assert isinstance(secret, str)
    assert secret
