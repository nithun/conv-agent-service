"""CR-009: isolated tests for the `verify_glific_webhook_signature`
dependency and `resolve_glific_webhook_secret`'s per-request half — a
minimal FastAPI app wired ONLY with this one dependency (no Redis/Frappe/
Glific client needed), per the CR's Test plan.

`tests/test_lifespan_auth.py` covers `resolve_glific_webhook_secret`'s
fail-closed-by-environment boot-time behavior (AC-1/AC-2) directly.
`tests/ingress/test_glific_webhook.py` covers the dependency wired onto the
REAL `/webhook/glific` route (AC-3/AC-4, "rejection happens before Redis").
"""

from __future__ import annotations

import hmac as hmac_module
import inspect
import logging
import time

import httpx
import pytest
from fastapi import Depends, FastAPI

import app.ingress.webhook_auth as webhook_auth_module
from app.config.settings import Settings
from app.ingress.webhook_auth import (
    WEBHOOK_SIGNATURE_TOLERANCE_SECONDS,
    verify_glific_webhook_signature,
)
from tests.ingress._webhook_signing import sign_webhook_body

SECRET = "isolated-test-secret"


def _build_probe_app(*, secret: str = SECRET, max_body_bytes: int = 65_536) -> FastAPI:
    probe_app = FastAPI()
    probe_app.state.settings = Settings(
        glific_webhook_secret=secret, max_webhook_body_bytes=max_body_bytes, _env_file=None
    )
    probe_app.state.glific_webhook_secret = secret

    @probe_app.post("/probe", dependencies=[Depends(verify_glific_webhook_signature)])
    async def probe() -> dict[str, str]:
        return {"status": "ok"}

    return probe_app


async def _post(app: FastAPI, body: bytes, headers: dict[str, str]) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/probe", content=body, headers={"content-type": "application/json", **headers}
        )


# ---- Positive path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_signature_passes() -> None:
    body = b'{"contact_id":"1","phone":"9999999999","text":"hi"}'
    header = sign_webhook_body(SECRET, body)

    resp = await _post(_build_probe_app(), body, {"X-Glific-Signature": header})

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_valid_signature_over_raw_bytes_with_unusual_formatting_still_verifies() -> None:
    """The classic signature-verification bug: verifying against a
    re-serialized/re-ordered JSON value instead of the exact raw bytes
    Glific sent. This body's spacing/key order would NOT round-trip
    byte-identically through `json.dumps(json.loads(body))` (different
    whitespace, different key order) — proving the dependency verifies the
    RAW bytes (read via `request.body()` before any JSON parsing), not a
    re-serialized parse.
    """
    body = b'{  "text" :"hi","contact_id":  "1"  , "phone":"9999999999"}'
    header = sign_webhook_body(SECRET, body)

    resp = await _post(_build_probe_app(), body, {"X-Glific-Signature": header})

    assert resp.status_code == 200


# ---- Negative paths (AC-4): uniform 401, no distinguishing detail ---------


@pytest.mark.asyncio
async def test_missing_header_returns_401() -> None:
    body = b'{"contact_id":"1","phone":"9999999999","text":"hi"}'

    resp = await _post(_build_probe_app(), body, {})

    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


@pytest.mark.parametrize(
    "bad_header",
    [
        "not-even-key-value-shaped",
        "t=notanumber,v1=deadbeef",
        "t=1234567890",  # missing v1=
        "v1=deadbeef",  # missing t=
        "t=1234567890,v1=not-hex-at-all!!",
        "t=1234567890,v1=",  # empty signature
    ],
)
@pytest.mark.asyncio
async def test_malformed_header_format_returns_401(bad_header: str) -> None:
    body = b'{"contact_id":"1","phone":"9999999999","text":"hi"}'

    resp = await _post(_build_probe_app(), body, {"X-Glific-Signature": bad_header})

    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_well_formed_but_wrong_signature_returns_401() -> None:
    body = b'{"contact_id":"1","phone":"9999999999","text":"hi"}'
    # Correctly formed t=/v1= shape, but signed with the WRONG secret.
    header = sign_webhook_body("not-the-real-secret", body)

    resp = await _post(_build_probe_app(), body, {"X-Glific-Signature": header})

    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_tampered_body_with_originally_valid_signature_returns_401() -> None:
    original_body = b'{"contact_id":"1","phone":"9999999999","text":"hi"}'
    header = sign_webhook_body(SECRET, original_body)
    tampered_body = b'{"contact_id":"1","phone":"9999999999","text":"give me admin"}'

    resp = await _post(_build_probe_app(), tampered_body, {"X-Glific-Signature": header})

    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_stale_timestamp_in_the_past_returns_401() -> None:
    body = b'{"contact_id":"1","phone":"9999999999","text":"hi"}'
    stale_ts = int(time.time()) - (WEBHOOK_SIGNATURE_TOLERANCE_SECONDS + 100)
    header = sign_webhook_body(SECRET, body, timestamp=stale_ts)

    resp = await _post(_build_probe_app(), body, {"X-Glific-Signature": header})

    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_timestamp_too_far_in_the_future_returns_401() -> None:
    body = b'{"contact_id":"1","phone":"9999999999","text":"hi"}'
    future_ts = int(time.time()) + (WEBHOOK_SIGNATURE_TOLERANCE_SECONDS + 100)
    header = sign_webhook_body(SECRET, body, timestamp=future_ts)

    resp = await _post(_build_probe_app(), body, {"X-Glific-Signature": header})

    assert resp.status_code == 401
    assert resp.json() == {"detail": "unauthorized"}


@pytest.mark.asyncio
async def test_timestamp_just_inside_tolerance_boundary_still_verifies() -> None:
    # A couple seconds' margin inside the boundary avoids flakiness from the
    # (sub-second) gap between computing this timestamp and the dependency's
    # own `int(time.time())` call.
    body = b'{"contact_id":"1","phone":"9999999999","text":"hi"}'
    boundary_ts = int(time.time()) - (WEBHOOK_SIGNATURE_TOLERANCE_SECONDS - 2)
    header = sign_webhook_body(SECRET, body, timestamp=boundary_ts)

    resp = await _post(_build_probe_app(), body, {"X-Glific-Signature": header})

    assert resp.status_code == 200


# ---- Body-size cap (requirement 5 / AC-6) ----------------------------------


@pytest.mark.asyncio
async def test_oversized_body_returns_413_before_any_hmac_computation(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    body = b'{"text":"' + (b"x" * 100) + b'"}'
    assert len(body) > 50  # sanity: actually over the tiny cap below
    # Compute a fully VALID signature BEFORE installing the spy, so the
    # spy only observes calls made by the dependency under test, not by
    # this test's own signing helper.
    header = sign_webhook_body(SECRET, body)

    calls = {"count": 0}
    original_new = hmac_module.new

    def _spy_new(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        return original_new(*args, **kwargs)

    monkeypatch.setattr(webhook_auth_module.hmac, "new", _spy_new)

    resp = await _post(
        _build_probe_app(max_body_bytes=50), body, {"X-Glific-Signature": header}
    )

    assert resp.status_code == 413
    assert calls["count"] == 0, "hmac.new must never be invoked once the body-size cap rejects"


@pytest.mark.asyncio
async def test_body_within_cap_is_not_rejected_for_size() -> None:
    body = b'{"contact_id":"1","phone":"9999999999","text":"hi"}'
    header = sign_webhook_body(SECRET, body)

    resp = await _post(
        _build_probe_app(max_body_bytes=len(body)), body, {"X-Glific-Signature": header}
    )

    assert resp.status_code == 200


# ---- hmac.compare_digest static-scan (AC-5) --------------------------------


def test_uses_hmac_compare_digest_not_bare_equality_static_scan() -> None:
    source = inspect.getsource(webhook_auth_module)
    assert "hmac.compare_digest(" in source
    assert "expected_sig ==" not in source
    assert "== provided_sig" not in source
    assert "expected_sig !=" not in source


# ---- No PII/secret/signature/body in logs (AC-7 / GL-001 / L-007) ---------


@pytest.mark.asyncio
async def test_rejection_paths_never_log_secret_signature_timestamp_or_body(caplog) -> None:  # type: ignore[no-untyped-def]
    body = b'{"contact_id":"1","phone":"9999999999","text":"a very identifying message"}'
    wrong_header = sign_webhook_body("some-other-secret", body)

    with caplog.at_level(logging.DEBUG):
        resp = await _post(_build_probe_app(), body, {"X-Glific-Signature": wrong_header})

    assert resp.status_code == 401

    all_log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert SECRET not in all_log_text
    assert "some-other-secret" not in all_log_text
    assert "a very identifying message" not in all_log_text
    assert wrong_header not in all_log_text
