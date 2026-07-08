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

- GLIFIC_HOST_HEADER_OVERRIDE: local-dev-only Docker workaround (F-021, see
  app/delivery/glific_client.py's module docstring for the full mechanism
  and root cause). Empty/unset by default — same opt-in discipline as
  GLIFIC_API_URL itself. When set (only ever in a local .env, never
  staging/prod), GlificClient sends this value as the outbound `Host`
  header on every request instead of the one httpx derives from
  GLIFIC_API_URL, working around the local dev Glific instance's
  SubdomainPlug hostname allowlist gap (BR-001 in the sibling
  glific-platform workspace; see ../docs/open-followups.md F-021 in the
  parent workspace).
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
    redis_url: str = ""

    # --- LLM / observability (populated once secrets exist, WBS 0.3/0.4) ---
    openrouter_api_key: str = ""
    # Optional override for the OpenAI-compatible endpoint the default
    # provider adapter targets. Empty (the default) = the real OpenRouter
    # URL (the adapter's own constant). Set ONLY in a local .env to point at
    # a self-hosted OpenAI-compatible gateway — e.g. the LiteLLM service in
    # docker-compose.yml fronting Vertex AI (F-020 local live-verification,
    # ADR-011's pluggable-backend intent). Same opt-in discipline as
    # GLIFIC_HOST_HEADER_OVERRIDE: never a baked non-empty default, never
    # set in staging/prod config.
    openrouter_base_url: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # --- Glific auth (session-based, see docs/glific-dev-endpoint.md) ---
    glific_phone: str = ""
    glific_password: str = ""
    # Local-dev-only Docker Host-header override (F-021) — empty/unset by
    # default, must never be given a value here or default to anything;
    # only ever set via a local .env. See this module's docstring above and
    # app/delivery/glific_client.py's module docstring for the mechanism.
    glific_host_header_override: str = ""
    # Optional: 'TAP Buddy Turn'-equivalent relay flow id — when set, the
    # worker re-arms this flow per turn (ported from tap-buddy's
    # GLIFIC_TURN_FLOW_ID / _maybe_rearm_turn_flow). Cutover (which flow
    # this points at in production) is explicitly out of this CR's scope
    # (F-019) — this setting only carries the env-var contract forward.
    glific_turn_flow_id: str = ""

    # --- TAP LMS / Frappe auth ---
    tap_lms_api_key: str = ""
    # Required for get_next_content (allow_guest=False on the live server,
    # ported fact from tap-buddy/CLAUDE.md, live-verified 2026-07-03).
    tap_lms_token: str = ""

    # --- Redis session store (R-2) ---
    # 30-min inactivity expiry, ported from tap-buddy's
    # state_machine.SESSION_TTL_SECONDS — AD-002's Interfaces & schemas
    # section records the field list; this is the TTL decision itself.
    session_ttl_seconds: int = 30 * 60

    # --- Media (ASR/TTS voice notes) ---
    # No baked default on purpose (same discipline as FRAPPE_API_URL): this
    # value is the literal URL prefix Glific/WhatsApp fetches voice-note
    # audio from — a wrong default fails SILENTLY (media links point at the
    # wrong service; the old ":8000" default pointed at tap-buddy's port
    # after the 8001 move — 360°-r2 diff-review High finding, 2026-07-08).
    # Set in .env: local dev = http://localhost:8001.
    public_base_url: str = ""
    media_dir: str = "./data/media"
    media_ttl_seconds: int = 24 * 60 * 60


def get_settings() -> Settings:
    """Return a fresh Settings instance (env re-read each call).

    Intentionally not cached with lru_cache in the scaffold: tests construct
    Settings directly with overrides. A caching wrapper can be added once
    real startup-time consumers exist (WBS 0.3+).
    """
    return Settings()
