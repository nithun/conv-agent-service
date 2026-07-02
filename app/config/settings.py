"""Env-sourced application settings (pydantic-settings).

All external endpoint URLs are read from the environment with NO baked-in
defaults for anything environment-specific:

- GLIFIC_API_URL: local dev value is http://host.docker.internal:4000
  (macOS-only Docker networking fact, see docs/architecture.md Sec5 in the
  parent workspace) - it must be supplied via .env / compose env, never
  hardcoded here.
- FRAPPE_API_URL: remote HTTPS instance, URL pending from the user
  (see docs/planning/PHASE-0-tasks.md task 0.5b in the parent workspace).
  No default - fails loudly if required and unset.
- REDIS_URL: compose service hostname in local dev (redis://redis:6379),
  a managed instance URL in staging/prod.

None of these have environment-specific defaults baked in here on purpose
(L-001/L-002-adjacent discipline: config drift into code is how a
macOS-only value like host.docker.internal leaks into staging).
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service identity ---
    app_name: str = "tap-agent-service"
    environment: str = "local"

    # --- External endpoints (no environment-specific defaults) ---
    glific_api_url: str = ""
    frappe_api_url: str = ""
    redis_url: str = "redis://localhost:6379"

    # --- LLM / observability (populated once secrets exist, WBS 0.3/0.4) ---
    openrouter_api_key: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"


def get_settings() -> Settings:
    """Return a fresh Settings instance (env re-read each call).

    Intentionally not cached with lru_cache in the scaffold: tests construct
    Settings directly with overrides. A caching wrapper can be added once
    real startup-time consumers exist (WBS 0.3+).
    """
    return Settings()
