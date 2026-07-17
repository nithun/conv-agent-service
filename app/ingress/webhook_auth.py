"""Glific webhook signature verification (CR-009).

A FastAPI dependency that verifies Glific's own `X-Glific-Signature` header
BEFORE any parsing, dedupe, distress-gate, or enqueue work runs on
`POST /webhook/glific` (`app/ingress/glific_webhook.py`).

Glific signs every flow-webhook dispatch unconditionally, not behind any
flow-editor toggle: `Glific.Flows.Webhooks.Request.add_signature/3`
(`lib/glific/flows/webhooks/request.ex:79-87`, sibling `Glific - clone`
workspace, live-verified this session) is called from `do_oban/3`
(`lib/glific/flows/webhook.ex:217`) for every dispatched webhook job. Header
shape: `X-Glific-Signature: t=<unix_ts>,v1=<hex hmac-sha256>`. Signed
payload: `"<timestamp>.<raw_body>"`. Secret: the Glific organization's
`signature_phrase` (`lib/glific.ex:262-274`), falling back to the literal
`"This is a dummy secret"` when unset — identical across every
unconfigured Glific deployment, hence the fail-closed discipline below.

Non-blocking finding carried forward (architecture-reviewer, CR-009 Status
log 2026-07-17): the timestamp is stamped at Oban ENQUEUE time inside
`do_oban/3` (baked into the job args), and job retries (`max_attempts: 2`,
`queue: :webhook`) reuse those SAME cached headers/timestamp — so
`WEBHOOK_SIGNATURE_TOLERANCE_SECONDS` budgets Glific's queue latency +
retry backoff, not network flight time. Do not re-justify this value with a
"in-flight request" mental model in future edits.

No code-level bypass: verification always runs, unconditionally, on every
environment, every request (CR-009's central design decision — there is no
`WEBHOOK_AUTH_MODE`/`ALLOW_UNAUTHENTICATED_WEBHOOK`-style flag anywhere in
this codebase). What varies by environment is only which secret backs the
check, resolved once at startup by `resolve_glific_webhook_secret` — see
that function's docstring for the fail-closed-by-environment rule
(architecture-reviewer CONDITION 2 fix, BLOCKING).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time

from fastapi import HTTPException, Request

from app.config.settings import Settings

logger = logging.getLogger("app.ingress.webhook_auth")

# Deliberately identical to Glific's own published fallback
# (`lib/glific.ex:266-268`, literal `"This is a dummy secret"`) so a
# genuinely-unconfigured local dev Glific org (no `signature_phrase` ever
# set — the common case) already signs with this exact value today, with
# ZERO Glific-side config change required for local dev to work end-to-end
# the moment verification goes live. NEVER a valid fallback outside
# `environment == "local"` — see `resolve_glific_webhook_secret`.
LOCAL_DEV_DEFAULT_SIGNATURE_SECRET = "This is a dummy secret"

# Stripe's own convention (5 minutes) for the `Stripe-Signature`-shaped
# t=/v1= format this mirrors. A CODE CONSTANT, not an env var — a security
# parameter, not an operational knob (GL-005: "structural, not a promise").
# See this module's docstring re: what this window actually budgets for
# Glific's dispatcher (queue latency + retry backoff, not network flight).
WEBHOOK_SIGNATURE_TOLERANCE_SECONDS = 300

# Uniform, generic 401 detail for every rejection mode (missing header,
# malformed format, stale timestamp, wrong signature) — deliberately NOT
# distinguishing them in the response (CR-009 Decision): there is no
# legitimate multi-tenant caller population here that needs a more granular
# error to self-correct, only glific-platform's own flows call this
# endpoint, so a granular error would only help a probing attacker.
_UNAUTHORIZED_DETAIL = "unauthorized"

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def resolve_glific_webhook_secret(settings: Settings) -> str:
    """Resolves the HMAC secret `verify_glific_webhook_signature` checks
    requests against. Called once at app startup (`app/main.py`'s
    `lifespan`), BEFORE any client (Redis/LMS/Glific/ModelRouter) is
    constructed — a boot-time `RuntimeError` here must abort startup
    entirely, never let the process come up looking healthy.

    Fail-closed-by-environment rule (architecture-reviewer CONDITION 2,
    BLOCKING — CR-009 Status log 2026-07-17):

    - `settings.glific_webhook_secret` set (any environment): use it,
      verbatim. This is the only path staging/prod ever takes.
    - `settings.glific_webhook_secret` unset AND `settings.environment`
      reads the EXACT string `"local"`: fall back to
      `LOCAL_DEV_DEFAULT_SIGNATURE_SECRET`. This requires an EXPLICIT
      affirmative opt-in — `Settings.environment` has NO baked default
      (see `app/config/settings.py`), so an unset `ENVIRONMENT` env var
      resolves to `""`, not `"local"`, and does NOT qualify here.
    - Anything else (secret unset AND environment is `""`/unset, or any
      value other than exactly `"local"` — including typos like
      `"locol"` or `"Local"`): raise `RuntimeError`. This is a BOOT-time
      failure, not a per-request 401 — refusing to start is louder and
      safer than booting healthy and silently 401-ing every real inbound
      message, including a distressed child's, at the transport layer
      (architecture-reviewer, ENDORSED, same Status log entry).
    """
    if settings.glific_webhook_secret:
        return settings.glific_webhook_secret

    if settings.environment == "local":
        logger.warning(
            "GLIFIC_WEBHOOK_SECRET unset with environment=local -- falling "
            "back to the published local-dev default signature secret. "
            "This is NOT a valid fallback outside environment=local."
        )
        return LOCAL_DEV_DEFAULT_SIGNATURE_SECRET

    raise RuntimeError(
        f"GLIFIC_WEBHOOK_SECRET is required when environment={settings.environment!r} "
        "(only the exact value 'local' may fall back to the published local-dev "
        "default; an unset, empty, or unrecognized environment never does) -- "
        "refusing to boot with webhook signature verification unkeyable."
    )


def _parse_signature_header(value: str) -> tuple[int, str]:
    """Parses the `t=<int>,v1=<hex>` format (comma-separated key=value
    pairs, same shape Stripe's `Stripe-Signature` uses). Raises `ValueError`
    on ANY malformed shape — every caller must treat that identically to a
    missing header (uniform 401, no distinguishing detail, per this
    module's docstring).
    """
    parts: dict[str, str] = {}
    for chunk in value.split(","):
        if "=" not in chunk:
            raise ValueError("malformed signature component")
        key, _, val = chunk.partition("=")
        parts[key.strip()] = val.strip()

    if "t" not in parts or "v1" not in parts:
        raise ValueError("missing t= or v1= component")

    timestamp = int(parts["t"])  # raises ValueError on any non-integer value

    sig = parts["v1"]
    if not sig or any(c not in _HEX_DIGITS for c in sig):
        raise ValueError("v1 is not a hex digest")

    return timestamp, sig.lower()


async def verify_glific_webhook_signature(request: Request) -> None:
    """FastAPI dependency (wired via `dependencies=[Depends(...)]` on
    `POST /webhook/glific`, never inside the handler body): verifies
    Glific's `X-Glific-Signature` header against the RAW request body
    before any parsing/dedupe/distress/enqueue work runs.

    - Body over `settings.max_webhook_body_bytes`: `HTTPException(413)`,
      checked BEFORE the header is even read or any HMAC is computed
      (CR-009 requirement 5).
    - Missing header, malformed `t=`/`v1=` format, timestamp outside
      `WEBHOOK_SIGNATURE_TOLERANCE_SECONDS`, or a signature mismatch: all
      four raise the SAME `HTTPException(401, detail="unauthorized")` — a
      deliberate uniformity decision (CR-009 Decision), not a granularity
      bug.
    - `hmac.compare_digest` is the ONLY comparison primitive used against
      the computed vs. provided digest (AC-5) — never a bare `==`.
    - The secret, the received signature, the received timestamp, and the
      raw body are NEVER logged on any path here, including rejections
      (GL-001/L-007).

    L-002 fast-ack: one `request.body()` read (already buffered by
    Starlette/uvicorn for the handler's own `request.json()` call — no new
    I/O; Starlette caches the body on first read so there is no
    double-consumption), one `hmac.new(...).hexdigest()` call, one
    `hmac.compare_digest` call — all microsecond-scale synchronous CPU work
    over a body capped at `max_webhook_body_bytes` (default 64KB), well
    inside the <200ms fast-ack budget.
    """
    settings: Settings = request.app.state.settings
    secret: str = request.app.state.glific_webhook_secret

    # MUST read raw bytes here, before any JSON parsing — the signature is
    # computed over the exact bytes Glific sent, not a re-serialized JSON
    # value (key order/whitespace differences would silently break
    # verification if we parsed-then-re-encoded instead).
    body = await request.body()

    if len(body) > settings.max_webhook_body_bytes:
        raise HTTPException(status_code=413, detail=_UNAUTHORIZED_DETAIL)

    header = request.headers.get("X-Glific-Signature")
    if not header:
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED_DETAIL)

    try:
        timestamp, provided_sig = _parse_signature_header(header)
    except ValueError:
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED_DETAIL) from None

    if abs(int(time.time()) - timestamp) > WEBHOOK_SIGNATURE_TOLERANCE_SECONDS:
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED_DETAIL)

    # Byte-concatenation, never a decode()+f-string re-encode of `body` —
    # keeps this an exact match of the raw bytes Glific hashed, regardless
    # of encoding edge cases in the payload.
    signed_payload = str(timestamp).encode("utf-8") + b"." + body
    expected_sig = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected_sig, provided_sig):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED_DETAIL)
