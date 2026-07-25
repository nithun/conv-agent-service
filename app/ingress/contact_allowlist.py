"""Service-side, fail-closed contact allowlist (CR-010, F-042/F-043).

A second, structural FastAPI dependency ahead of the route handler on
`POST /webhook/glific` (after `app.ingress.webhook_auth.verify_glific_webhook_signature`,
CR-009): a request whose parsed `contact_id`/`phone` is not a member of an
explicit, non-defaulted, fail-closed-when-unset allowlist is rejected with
`403 forbidden` BEFORE dedupe, the top-level distress gate, or enqueue —
no reply, no ticket, no LLM call, no Glific send FROM THIS SERVICE (a
rejected request still wakes the calling Glific flow's own FAILURE branch —
see CR-010's Edge cases "Guardrail/handoff interaction" section; that is a
channel-level consequence outside this module's control).

Mirrors `app.ingress.webhook_auth`'s shape deliberately: `ContactAllowlist`
is resolved once in `app/main.py`'s `lifespan`, before any client is
constructed; an unresolvable allowlist outside `environment == "local"`
raises `RuntimeError` at BOOT time (refuse to start), never a per-request
reject-everything runtime mode; there is no `ALLOW_*`/`*_MODE` bypass
anywhere.

Identity is read via the SAME trusted parser every other caller uses
(`app.ingress.glific_payload.parse_glific_webhook_payload`) — never a
second, divergent identity extractor (L-014). That coupling is
security-relevant: see `glific_payload.py`'s module docstring and
`tests/ingress/test_contact_allowlist.py`'s parser-normalization regression
tests (architecture-reviewer C-6.1).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.config.settings import Settings
from app.ingress.glific_payload import parse_glific_webhook_payload

logger = logging.getLogger("app.ingress.contact_allowlist")

# The entirety of local dev's Glific simulator contact pool (glific-student-sim,
# F-053) — a LOCAL-GLIFIC-SEED fact, not a universal one. Only ever applied
# as a fallback when BOTH `environment == "local"` AND `glific_api_url`
# looks local (architecture-reviewer C-2, 2026-07-22) — never against a
# real Glific org, where these ids are five arbitrary real contacts.
LOCAL_DEV_SIMULATOR_CONTACT_IDS = frozenset({"2", "3", "4", "5", "6"})

_LOCAL_GLIFIC_API_HOSTS = frozenset({"localhost", "127.0.0.1", "host.docker.internal"})

# Deliberately distinct from CR-009's "unauthorized" (architecture-reviewer
# C-1, 2026-07-22) — this check runs only after signature verification
# already succeeded, so the caller is already known to be Glific's own
# authenticated dispatcher; there is no probing-attacker population this
# detail could leak information to.
_REJECTED_DETAIL = "forbidden"


@dataclass(frozen=True)
class ContactAllowlist:
    """One unified token set matched against either `contact_id` or
    `phone` — not two separately-typed lists (CR-010 Decision: a
    digit-string alone cannot be reliably classified as "an id" vs. "a
    phone", and a wrong classification heuristic in a security-relevant
    match is worse than not guessing at all).
    """

    tokens: frozenset[str]

    def is_allowed(self, contact_id: str, phone: str) -> bool:
        return contact_id in self.tokens or phone in self.tokens


def _glific_api_url_looks_local(glific_api_url: str) -> bool:
    """True only if `glific_api_url`'s host is a known local-dev host.
    Empty/unset counts as NOT local — an operator who hasn't set
    GLIFIC_API_URL yet gets no free pass either.
    """
    if not glific_api_url:
        return False
    return (urlsplit(glific_api_url).hostname or "") in _LOCAL_GLIFIC_API_HOSTS


def resolve_contact_allowlist(settings: Settings) -> ContactAllowlist:
    """Resolves the `ContactAllowlist` `verify_contact_allowlisted` checks
    requests against. Called once at app startup (`app/main.py`'s
    `lifespan`), immediately after `resolve_glific_webhook_secret` and
    BEFORE any client (Redis/LMS/Glific/ModelRouter) is constructed — a
    boot-time `RuntimeError` here must abort startup entirely, never let
    the process come up looking healthy while silently rejecting (or
    silently accepting) every real inbound contact.

    Resolution order:

    - `settings.glific_contact_allowlist` non-empty (any environment): the
      comma-separated tokens, whitespace-trimmed, empty entries dropped.
      This value REPLACES any default outright — it never merges with the
      local-dev simulator pool below, in any environment including
      `local` (an operator who sets this to just their own test number
      gets EXACTLY that one contact).
    - Empty AND `settings.environment == "local"` AND
      `settings.glific_api_url` looks local (architecture-reviewer C-2,
      2026-07-22 — closes a fail-open path where `ENVIRONMENT=local` plus
      a REAL, hosted `GLIFIC_API_URL` would otherwise have silently
      allowlisted five arbitrary real contacts): falls back to
      `LOCAL_DEV_SIMULATOR_CONTACT_IDS`.
    - Anything else: raises `RuntimeError` — refusing to boot with the
      contact allowlist unbounded is louder and safer than booting healthy
      and either silently 403-ing every real inbound contact or, worse,
      accepting every contact unconditionally (architecture-reviewer,
      same reasoning ENDORSED for CR-009's own boot-time secret check).
    """
    raw = settings.glific_contact_allowlist
    if raw.strip():
        tokens = frozenset(t.strip() for t in raw.split(",") if t.strip())
        return ContactAllowlist(tokens=tokens)

    if settings.environment == "local" and _glific_api_url_looks_local(settings.glific_api_url):
        logger.warning(
            "GLIFIC_CONTACT_ALLOWLIST unset with environment=local and a "
            "local-looking GLIFIC_API_URL -- falling back to the known "
            "local-dev Glific simulator contact pool (ids 2-6). This is "
            "NOT a valid fallback outside environment=local with a "
            "local-looking GLIFIC_API_URL."
        )
        return ContactAllowlist(tokens=LOCAL_DEV_SIMULATOR_CONTACT_IDS)

    raise RuntimeError(
        f"GLIFIC_CONTACT_ALLOWLIST is required when environment="
        f"{settings.environment!r} and GLIFIC_API_URL="
        f"{settings.glific_api_url!r} (the local-dev simulator pool only "
        "applies when environment is exactly 'local' AND GLIFIC_API_URL "
        "points at a known local-dev host -- ids 2-6 are LOCAL SEEDED "
        "contacts, real people on any other Glific instance) -- refusing "
        "to boot with the contact allowlist unbounded."
    )


def _hash_contact_id(contact_id: str) -> str:
    """Truncated SHA-256 of the raw `contact_id` — NEVER the phone
    (L-007/GL-001: no PII in application logs). Truncation to 12 hex chars
    keeps rejection log lines distinguishable across contacts without
    retaining the full digest for no operational benefit — this is not a
    cryptographic requirement, `contact_id` is never treated as a secret,
    only as PII that must not appear in logs verbatim.
    """
    return hashlib.sha256(contact_id.encode("utf-8")).hexdigest()[:12]


async def verify_contact_allowlisted(request: Request) -> None:
    """FastAPI dependency (wired via `dependencies=[Depends(...)]` on
    `POST /webhook/glific`, as the SECOND entry — after
    `verify_glific_webhook_signature`, never reordered): rejects any
    request whose parsed contact identity is not a member of
    `request.app.state.contact_allowlist` with `403 forbidden`, before the
    route handler body (dedupe/distress-gate/enqueue) ever runs.

    Reuses `parse_glific_webhook_payload` — the SAME parser the route
    handler itself uses — rather than a second, divergent identity
    extractor (L-014). A payload this dependency cannot parse is NOT this
    dependency's concern: the route handler's own JSON/schema parsing
    already 400s any payload lacking an identifiable contact_id/phone/
    message_id before dedupe/distress/enqueue ever runs (unchanged by this
    check), so a malformed body passes through silently here and is
    handled downstream exactly as before this CR.
    """
    try:
        raw = await request.json()
        payload = parse_glific_webhook_payload(raw)
    except Exception:
        return

    allowlist: ContactAllowlist = request.app.state.contact_allowlist
    if allowlist.is_allowed(payload.contact_id, payload.phone):
        return

    # Non-PII observability (CR-010's own requirement: "so a misconfigured
    # allowlist is noticed") — hashed/truncated contact_id only, never the
    # raw contact_id or phone (L-007/GL-001). A module-level counter would
    # leak across unrelated FastAPI app instances in the same process
    # (e.g. probe apps built in isolated tests); per-app state on
    # `request.app.state` keeps the count scoped to the app instance that
    # actually rejected the request, mirroring how every other piece of
    # this dependency's config is already read from `request.app.state`.
    current_count = getattr(request.app.state, "contact_allowlist_rejection_count", 0)
    request.app.state.contact_allowlist_rejection_count = current_count + 1

    logger.warning(
        "contact_allowlist_reject contact_id_hash=%s",
        _hash_contact_id(payload.contact_id),
    )
    raise HTTPException(status_code=403, detail=_REJECTED_DETAIL)
