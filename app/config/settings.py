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
    # CR-009 (webhook-auth) fail-closed fix, architecture-reviewer CONDITION
    # 2 (BLOCKING, 2026-07-17): this field used to default to "local", which
    # meant an UNSET `ENVIRONMENT` env var (e.g. a `.env` copied from
    # `.env.example` before that file carried an `ENVIRONMENT=` line) was
    # silently indistinguishable from an operator EXPLICITLY choosing local
    # dev — `app.ingress.webhook_auth.resolve_glific_webhook_secret` only
    # falls back to the published local-dev dummy signature secret when
    # `environment` reads the EXACT string `"local"`, so a genuinely unset
    # env var (now `""`, not `"local"`) correctly hits that function's
    # fail-closed RuntimeError branch instead of silently "authenticating"
    # a public webhook with a constant published in Glific's own
    # open-source repo. Local dev must now set `ENVIRONMENT=local`
    # explicitly (`.env.example` carries it pre-filled, so a fresh
    # `cp .env.example .env` still boots with zero further action) — this
    # is an explicit affirmative opt-in, never a default that happens to
    # read "local".
    environment: str = ""

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

    # --- Google Speech-to-Text/Text-to-Speech (BR-004 Task 1, worker
    # container only) ---
    # No baked default (same discipline as every other credential field):
    # a real GCP service-account key path. Read BOTH by the Google Cloud
    # client libraries themselves via this SAME env var name for ADC
    # discovery (google.auth.default() looks up GOOGLE_APPLICATION_CREDENTIALS
    # directly from the process environment) AND by this app-level field,
    # which gates SpeechClient construction
    # (app/worker/main.py::_build_speech_client) — mirrors tap-buddy's
    # `_build_speech_client(settings)` pattern
    # (tap-buddy/src/tap_buddy/main.py:34-39): empty = no speech client is
    # ever constructed, voice turns degrade gracefully (text-only), never a
    # bare unguarded SpeechClient() call crashing worker startup via
    # DefaultCredentialsError. Reuses docker-compose.yml's `worker` service
    # GCP_SA_KEY_PATH-sourced mount (/secrets/gcp-sa.json) — no second
    # key-path variable invented.
    google_application_credentials: str = ""

    # --- Glific webhook signature verification (CR-009) ---
    # Verifies Glific's own `X-Glific-Signature: t=<unix_ts>,v1=<hex>`
    # header (HMAC-SHA256 over "<timestamp>.<raw_body>", keyed by the
    # Glific organization's `signature_phrase`) ahead of every other step
    # on POST /webhook/glific. No baked default (same no-baked-secret
    # discipline as every other credential field here) — the local-dev
    # fallback secret is resolved by
    # `app.ingress.webhook_auth.resolve_glific_webhook_secret`, gated on an
    # EXPLICIT `environment == "local"`, never on this field's own default
    # or absence. MUST be set to the exact value of the live org's
    # `signature_phrase` in staging/prod, delivered out-of-band — never
    # paste the literal value into any doc/commit in this repo.
    glific_webhook_secret: str = ""
    # Body-size cap enforced BEFORE any HMAC computation (CR-009
    # requirement 5) — generous for a WhatsApp text/media-URL JSON payload
    # (real payloads are a few hundred bytes to a few KB); closes the
    # "attacker sends a gigantic body to burn CPU/memory" gap.
    max_webhook_body_bytes: int = 65_536

    # --- Contact allowlist (CR-010, F-042/F-043) ---
    # Comma-separated Glific contact ids and/or phone numbers permitted to
    # reach POST /webhook/glific — matched by
    # `app.ingress.contact_allowlist.ContactAllowlist.is_allowed` against
    # the SAME parsed contact_id/phone `parse_glific_webhook_payload`
    # already produces for every other caller (no second parser, L-014).
    # No baked default (same no-baked-secret discipline as
    # glific_webhook_secret) — a static read of `Settings()` must never
    # look bounded on its own. The actual fail-closed-by-environment
    # resolution (including the local-dev simulator-pool fallback, gated on
    # BOTH environment == "local" AND a local-looking glific_api_url per
    # architecture-reviewer C-2) lives in
    # `app.ingress.contact_allowlist.resolve_contact_allowlist`, called
    # once at startup — never here, so this field's own default can never
    # silently widen who may reach the webhook.
    glific_contact_allowlist: str = ""


def get_settings() -> Settings:
    """Return a fresh Settings instance (env re-read each call).

    Intentionally not cached with lru_cache in the scaffold: tests construct
    Settings directly with overrides. A caching wrapper can be added once
    real startup-time consumers exist (WBS 0.3+).
    """
    return Settings()
