"""Tests for env-sourced Settings loading.

Asserts the no-baked-in-default discipline for environment-specific
endpoints (Glific/Frappe URLs) so a future PR can't silently reintroduce a
hardcoded host.docker.internal or localhost default.
"""

from __future__ import annotations

from app.config.settings import Settings


def test_settings_has_no_baked_in_endpoint_defaults(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("GLIFIC_API_URL", raising=False)
    monkeypatch.delenv("FRAPPE_API_URL", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.glific_api_url == ""
    assert settings.frappe_api_url == ""
    assert settings.redis_url == ""


def test_settings_reads_endpoints_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GLIFIC_API_URL", "http://host.docker.internal:4000")
    monkeypatch.setenv("FRAPPE_API_URL", "https://tap-lms.example.org")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379")

    settings = Settings(_env_file=None)

    assert settings.glific_api_url == "http://host.docker.internal:4000"
    assert settings.frappe_api_url == "https://tap-lms.example.org"
    assert settings.redis_url == "redis://redis:6379"


def test_google_application_credentials_has_no_baked_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # BR-004 Task 1: empty by default (the worker/main.py speech-client gate
    # relies on this) — no environment-specific value baked into code.
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    settings = Settings(_env_file=None)

    assert settings.google_application_credentials == ""


def test_google_application_credentials_round_trips_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # BR-004 Task 1 AC: round-trips like every other string setting — no
    # live GCP call in this test, purely an env-plumbing assertion.
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/gcp-sa.json")

    settings = Settings(_env_file=None)

    assert settings.google_application_credentials == "/secrets/gcp-sa.json"


def test_environment_has_no_baked_local_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # CR-009 (webhook auth) architecture-reviewer CONDITION 2, BLOCKING: an
    # unset ENVIRONMENT env var must resolve to "" — NOT "local" — so a
    # `.env` missing an `ENVIRONMENT=` line (e.g. copied from a stale
    # .env.example) cannot silently qualify for the local-dev
    # dummy-signature-secret fallback
    # (app.ingress.webhook_auth.resolve_glific_webhook_secret). Full
    # fail-closed-boot-time coverage lives in tests/test_lifespan_auth.py.
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    settings = Settings(_env_file=None)

    assert settings.environment == ""


def test_glific_webhook_secret_and_body_cap_have_no_baked_secret_default(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("GLIFIC_WEBHOOK_SECRET", raising=False)

    settings = Settings(_env_file=None)

    assert settings.glific_webhook_secret == ""
    assert settings.max_webhook_body_bytes == 65_536
