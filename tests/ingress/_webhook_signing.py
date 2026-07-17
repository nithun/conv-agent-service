"""Test-only helper (CR-009 test plan): computes a valid `X-Glific-Signature`
header value for a given secret + raw body, mirroring exactly what a real
Glific dispatcher computes
(`Glific.Flows.Webhooks.Request.add_signature/3` +
`Glific.signature/3`, sibling `Glific - clone` workspace source) — so tests
never hand-encode hex digests.

Not a `test_*.py` file on purpose — pytest must not collect this as a test
module, it is imported by other test modules (`tests/ingress/conftest.py`,
`tests/ingress/test_glific_webhook.py`, `tests/ingress/test_webhook_auth.py`).
"""

from __future__ import annotations

import hashlib
import hmac
import time


def sign_webhook_body(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    """Returns a full `X-Glific-Signature: t=<ts>,v1=<hex>` header VALUE
    (not including the header name) for `body`, signed with `secret` at
    `timestamp` (defaults to now). Byte-concatenates exactly like
    `app.ingress.webhook_auth.verify_glific_webhook_signature` does —
    never a decode()+f-string re-encode of `body` — so a test can freely
    pass a body with unusual key ordering/whitespace to prove raw-byte
    (not re-serialized) verification.
    """
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = str(ts).encode("utf-8") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"
