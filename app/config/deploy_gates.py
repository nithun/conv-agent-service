"""Production deploy-gate boot checks (CR-011, ADR-013 GL-005-a/GL-005-b).

Two boot-time validations, same shape/location convention as
`app.ingress.webhook_auth.resolve_glific_webhook_secret` (CR-009) and
`app.ingress.contact_allowlist.resolve_contact_allowlist` (CR-010): each is
called once in `app/main.py`'s `lifespan`, BEFORE any client (Redis/LMS/
Glific/ModelRouter) is constructed, and raises `RuntimeError` to abort
startup entirely rather than let the process come up looking healthy while
one of ADR-013's two structural preconditions is silently unmet.

Unlike the CR-009/CR-010 gates (which fail closed for ANY environment other
than the exact opt-in string `"local"`), both gates here are deliberately
**production-only** (`settings.environment == "production"`, not
`!= "local"`) — CR-011's own Acceptance criteria pin this asymmetry
explicitly, since these two checks exist to bound what a REAL deployed VM is
allowed to do, not to force every non-local environment (e.g. a `staging`
smoke-test box that may legitimately point at neither self-hosted Langfuse
nor the evalix restore) through the same fail-closed discipline CR-009/CR-010
apply to auth/population config.

**GL-005-a — Langfuse egress fail-close (F-070).** In production, a trace
egressing to `cloud.langfuse.com` (the field's own baked default,
`Settings.langfuse_host`) would ship a real child's resolved identity +
message text to external SaaS — the exact thing ADR-013's accepted-risk
basis ("data never leaves the operator's own infrastructure") depends on
never happening. `resolve_langfuse_host_gate` refuses to boot in that case.

**GL-005-b — LMS-write-target fail-close.** In production, every write this
service ever performs (`save_submission`, and CR-011's own
`register_student`/`reset_test_student` once built) must land on the evalix
prod-restore (`lms.evalix.xyz`) — "a throwaway copy that feeds nothing back
into live production" (F-043) — never a live-production TAP LMS.
`resolve_lms_write_target_gate` refuses to boot if `FRAPPE_API_URL` points
anywhere else, INCLUDING when it is simply unset (unset must never be read
as "not production, skip the check" — the same unset-is-not-a-pass
discipline CR-009/CR-010's own gates apply).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from app.config.settings import Settings

# Compose-local self-hosted Langfuse: the service-name URL
# (`http://langfuse:3000`) on the Docker Compose network, or a bare
# localhost/127.0.0.1 value for a non-compose local run. Mirrors
# `app.ingress.contact_allowlist._LOCAL_GLIFIC_API_HOSTS`'s shape, but this
# is the INVERSE check (production must resolve TO one of these, not away
# from them).
_SELF_HOSTED_LANGFUSE_HOSTS = frozenset({"langfuse", "localhost", "127.0.0.1"})

# The one host every LMS write in production must target — the evalix
# prod-restore, never a live-production TAP LMS. A single literal value, not
# a set, per ADR-013 GL-005-b's own wording ("unless it points at the evalix
# restore host `lms.evalix.xyz`").
_EVALIX_LMS_RESTORE_HOST = "lms.evalix.xyz"


def _langfuse_host_is_self_hosted(langfuse_host: str) -> bool:
    """True only if `langfuse_host`'s hostname is a known compose-local
    self-hosted value. Empty/unset counts as NOT self-hosted — an operator
    who hasn't set `LANGFUSE_HOST` yet (so it still reads the baked
    `https://cloud.langfuse.com` default) gets no free pass either.
    """
    if not langfuse_host:
        return False
    return (urlsplit(langfuse_host).hostname or "") in _SELF_HOSTED_LANGFUSE_HOSTS


def _frappe_api_url_targets_evalix_restore(frappe_api_url: str) -> bool:
    """True only if `frappe_api_url`'s hostname is exactly
    `lms.evalix.xyz`. Empty/unset counts as NOT the restore host — a
    `FRAPPE_API_URL` that hasn't been configured yet must fail this check,
    not silently pass it (GL-005-b's own "unset must never be treated as
    not production, skip the check" requirement).
    """
    if not frappe_api_url:
        return False
    return (urlsplit(frappe_api_url).hostname or "") == _EVALIX_LMS_RESTORE_HOST


def resolve_langfuse_host_gate(settings: Settings) -> None:
    """GL-005-a (F-070). Called once at app startup (`app/main.py`'s
    `lifespan`), immediately after `resolve_contact_allowlist` and BEFORE
    any client (Redis/LMS/Glific/ModelRouter) is constructed.

    - `settings.environment == "production"` AND `settings.langfuse_host`
      does not resolve to a known compose-local self-hosted value (host not
      in `{"langfuse", "localhost", "127.0.0.1"}`): raises `RuntimeError`.
      This is the ONLY environment value this gate fires for — deliberately
      narrower than CR-009/CR-010's own `!= "local"` gates (see this
      module's docstring for why).
    - Anything else (any non-`"production"` environment, including an
      unset/empty one, regardless of `langfuse_host`'s value): no raise.
    """
    # environment compared case/whitespace-normalized so a typo like
    # " Production " can't silently bypass this structural gate
    # (code-review 2026-07-27 Low — ENVIRONMENT normalization).
    env = settings.environment.strip().lower()
    if env == "production" and not _langfuse_host_is_self_hosted(
        settings.langfuse_host
    ):
        raise RuntimeError(
            f"LANGFUSE_HOST={settings.langfuse_host!r} does not resolve to the "
            "compose-local self-hosted Langfuse instance (expected a host in "
            "{'langfuse', 'localhost', '127.0.0.1'}) while ENVIRONMENT="
            "'production' -- refusing to boot with observability egress "
            "pointed at external SaaS (ADR-013 GL-005-a / F-070). A real "
            "child's resolved identity and message text must never leave "
            "this operator's own infrastructure in production."
        )


def resolve_lms_write_target_gate(settings: Settings) -> None:
    """GL-005-b. Called once at app startup (`app/main.py`'s `lifespan`),
    immediately after `resolve_langfuse_host_gate` and BEFORE any client is
    constructed.

    - `settings.environment == "production"` AND `settings.frappe_api_url`
      does not resolve to exactly `lms.evalix.xyz` (including when it is
      unset/empty): raises `RuntimeError`. Elevated from a deploy-checklist
      item to a structural boot gate at ADR-013's acceptance (Nithun,
      2026-07-25) — same production-only trigger condition as GL-005-a, not
      CR-009/CR-010's `!= "local"` shape.
    - Anything else (any non-`"production"` environment): no raise.
    """
    # environment compared case/whitespace-normalized (same as GL-005-a).
    env = settings.environment.strip().lower()
    if env == "production" and not _frappe_api_url_targets_evalix_restore(
        settings.frappe_api_url
    ):
        raise RuntimeError(
            f"FRAPPE_API_URL={settings.frappe_api_url!r} does not point at the "
            "evalix prod-restore host (lms.evalix.xyz) while ENVIRONMENT="
            "'production' -- refusing to boot with LMS writes (register_student/"
            "save_submission/reset_test_student) pointed at anything other than "
            "the throwaway restore that feeds nothing back into live production "
            "(ADR-013 GL-005-b). This must never resolve to a live-production "
            "TAP LMS."
        )
