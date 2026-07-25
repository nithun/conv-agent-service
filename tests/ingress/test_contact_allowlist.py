"""CR-010: isolated tests for the `verify_contact_allowlisted` dependency,
`resolve_contact_allowlist`'s token-parsing behavior, and the
architecture-reviewer C-6.1 parser-normalization regression coverage — a
minimal FastAPI app wired ONLY with this one dependency (no Redis/Frappe/
Glific client needed), mirroring `tests/ingress/test_webhook_auth.py`'s
`_build_probe_app` idiom exactly.

`tests/test_lifespan_contact_allowlist.py` covers `resolve_contact_allowlist`'s
fail-closed-by-environment boot-time behavior (AC-1/AC-1b/AC-2/AC-3) directly.
`tests/test_real_lifespan_contact_allowlist.py` covers the REAL `lifespan`
wiring proof (C-6.2). `tests/ingress/test_glific_webhook.py` covers the
dependency wired onto the REAL `/webhook/glific` route (AC-4/AC-5/AC-9,
"rejection happens before Redis, before the distress gate").
"""

from __future__ import annotations

import logging

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.ingress.contact_allowlist import (
    ContactAllowlist,
    verify_contact_allowlisted,
)

ALLOWED_CONTACT_ID = "1"
ALLOWED_PHONE = "9999999999"


_DEFAULT_TOKENS = frozenset({ALLOWED_CONTACT_ID, ALLOWED_PHONE})


def _build_probe_app(*, tokens: frozenset[str] = _DEFAULT_TOKENS) -> FastAPI:
    probe_app = FastAPI()
    probe_app.state.contact_allowlist = ContactAllowlist(tokens=tokens)
    probe_app.state.contact_allowlist_rejection_count = 0

    @probe_app.post("/probe", dependencies=[Depends(verify_contact_allowlisted)])
    async def probe() -> dict[str, str]:
        return {"status": "ok"}

    return probe_app


async def _post(app: FastAPI, body: dict[str, object] | bytes) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        if isinstance(body, bytes):
            return await client.post(
                "/probe", content=body, headers={"content-type": "application/json"}
            )
        return await client.post("/probe", json=body)


# ---- Core pass/reject behavior (task 2) ------------------------------------


@pytest.mark.asyncio
async def test_allowlisted_contact_id_passes() -> None:
    payload = {"contact_id": ALLOWED_CONTACT_ID, "phone": "911111111111", "text": "hi"}

    resp = await _post(_build_probe_app(), payload)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_allowlisted_phone_passes() -> None:
    payload = {"contact_id": "999", "phone": ALLOWED_PHONE, "text": "hi"}

    resp = await _post(_build_probe_app(), payload)

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_non_allowlisted_contact_rejects_with_403_forbidden() -> None:
    """Adversarial fixture (L-017): a realistic, well-formed, non-malformed
    contact_id/phone pair that is simply not on the allowlist — not a
    truncated/garbage payload.
    """
    payload = {"contact_id": "999999", "phone": "9199999999", "text": "hi there"}

    resp = await _post(_build_probe_app(), payload)

    assert resp.status_code == 403
    assert resp.json() == {"detail": "forbidden"}


@pytest.mark.asyncio
async def test_malformed_body_passes_through_silently_no_exception_from_dependency() -> None:
    """Malformed/unparseable payloads are NOT this dependency's concern —
    it must not itself raise; the route handler's own JSON/schema parsing
    is what 400s these (unchanged by this CR). Proven here by observing
    the probe route's 200 despite the body having no identifiable
    contact_id/phone at all.
    """
    resp = await _post(_build_probe_app(), b"not json at all")
    assert resp.status_code == 200

    resp2 = await _post(_build_probe_app(), {"text": "hi, no contact info"})
    assert resp2.status_code == 200


# ---- Non-PII observability (task 4 / AC-7) ---------------------------------


@pytest.mark.asyncio
async def test_rejection_log_never_contains_raw_phone_or_raw_contact_id(caplog) -> None:  # type: ignore[no-untyped-def]
    payload = {
        "contact_id": "5551234999",
        "phone": "919888777666",
        "text": "a very identifying message",
    }

    with caplog.at_level(logging.DEBUG):
        resp = await _post(_build_probe_app(), payload)

    assert resp.status_code == 403

    all_log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "5551234999" not in all_log_text
    assert "919888777666" not in all_log_text


@pytest.mark.asyncio
async def test_rejection_counter_increments_once_per_rejection_unaffected_by_allowed_requests() -> (
    None
):
    app = _build_probe_app()
    rejected_payload = {"contact_id": "999999", "phone": "9199999999", "text": "hi"}
    allowed_payload = {"contact_id": ALLOWED_CONTACT_ID, "phone": "911111111111", "text": "hi"}

    for _ in range(3):
        resp = await _post(app, rejected_payload)
        assert resp.status_code == 403

    assert app.state.contact_allowlist_rejection_count == 3

    resp = await _post(app, allowed_payload)
    assert resp.status_code == 200
    assert app.state.contact_allowlist_rejection_count == 3

    resp = await _post(app, rejected_payload)
    assert resp.status_code == 403
    assert app.state.contact_allowlist_rejection_count == 4


# ---- C-6.1: pin the parser normalization the allowlist match depends on ---


@pytest.mark.asyncio
async def test_simulator_suffix_stripped_phone_matches_the_unsuffixed_token() -> None:
    """`"9876543210_1"` (the Glific simulator `_N` phone suffix) must match
    an allowlist token of the unsuffixed `"9876543210"` — the exact
    behavior `parse_glific_webhook_payload` already performs for LMS
    lookup, now also security-relevant for allowlist matching.
    """
    app = _build_probe_app(tokens=frozenset({"9876543210"}))
    payload = {"contact_id": "not-on-the-list", "phone": "9876543210_1", "text": "hi"}

    resp = await _post(app, payload)

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_at_expression_phone_never_becomes_a_literal_token_match() -> None:
    """An unresolved Glific flow variable in the top-level `phone` field
    (`"@input.phone"`) must NOT resolve to a literal, matchable string —
    even if an allowlist somehow contained that exact literal (a
    misconfiguration this test guards against). The nested `contact.phone`
    fallback supplies a real value so the payload still parses (an
    `@`-expression with NO fallback resolves to an empty phone, which
    fails `parse_glific_webhook_payload` entirely with a ValueError — a
    400 at the route handler, a different, already-covered path, not this
    dependency's malformed-passthrough branch being mistaken for a
    match). The resolved phone must be the nested fallback value, never
    the literal `"@input.phone"` string, so it never matches the
    pathological allowlist entry below.
    """
    app = _build_probe_app(tokens=frozenset({"@input.phone"}))
    payload = {
        "contact_id": "999999",
        "phone": "@input.phone",
        "contact": {"phone": "919000000001"},
        "text": "hi",
    }

    resp = await _post(app, payload)

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_at_expression_phone_with_no_fallback_fails_parsing_not_a_false_match() -> None:
    """When an `@`-expression phone has NO nested fallback, the parser
    itself raises (empty phone) — this dependency's malformed-passthrough
    branch lets it through untouched (200 here, since this probe route
    does not itself re-parse); the REAL route handler
    (`tests/ingress/test_glific_webhook.py`) still 400s it downstream via
    its own parse call, unchanged by this CR. Documented here so this
    passthrough is never mistaken for an accidental allowlist match.
    """
    app = _build_probe_app(tokens=frozenset({"@input.phone"}))
    payload = {"contact_id": "999999", "phone": "@input.phone", "text": "hi"}

    resp = await _post(app, payload)

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_nested_contact_object_fallback_yields_same_token_as_top_level_fields() -> None:
    """The `contact.id`/`contact.phone` fallback path (used when the
    top-level `contact_id`/`phone` keys are absent) must resolve to the
    SAME token the top-level fields would — proven separately for id and
    phone, since either alone is sufficient for `is_allowed` to match.
    """
    app_id = _build_probe_app(tokens=frozenset({"50"}))
    payload_nested_id = {
        "contact": {"id": "50", "phone": "919000000000"},
        "message": {"id": "m-1"},
        "text": "hi",
    }
    resp_id = await _post(app_id, payload_nested_id)
    assert resp_id.status_code == 200

    app_phone = _build_probe_app(tokens=frozenset({"9123456789"}))
    payload_nested_phone = {
        "contact": {"id": "51", "phone": "9123456789"},
        "message": {"id": "m-2"},
        "text": "hi",
    }
    resp_phone = await _post(app_phone, payload_nested_phone)
    assert resp_phone.status_code == 200
