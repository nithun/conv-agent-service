"""Tests for env-sourced Settings loading.

Asserts the no-baked-in-default discipline for environment-specific
endpoints (Glific/Frappe URLs) so a future PR can't silently reintroduce a
hardcoded host.docker.internal or localhost default.
"""

from __future__ import annotations

from app.config.settings import Settings


def test_settings_has_no_baked_in_glific_or_frappe_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("GLIFIC_API_URL", raising=False)
    monkeypatch.delenv("FRAPPE_API_URL", raising=False)

    settings = Settings(_env_file=None)

    assert settings.glific_api_url == ""
    assert settings.frappe_api_url == ""


def test_settings_reads_endpoints_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("GLIFIC_API_URL", "http://host.docker.internal:4000")
    monkeypatch.setenv("FRAPPE_API_URL", "https://tap-lms.example.org")
    monkeypatch.setenv("REDIS_URL", "redis://redis:6379")

    settings = Settings(_env_file=None)

    assert settings.glific_api_url == "http://host.docker.internal:4000"
    assert settings.frappe_api_url == "https://tap-lms.example.org"
    assert settings.redis_url == "redis://redis:6379"
