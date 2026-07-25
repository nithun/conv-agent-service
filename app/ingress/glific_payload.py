"""Glific webhook payload parsing — ported verbatim in behavior from
`tap-buddy/src/tap_buddy/adapters/glific_webhook.py::_parse_payload`
(CR-004 Task 3): `@`-expression-as-absent handling, simulator-phone `_N`
suffix stripping, and key-alias defensiveness, since the live flow-editor
config (a human step, not this repo) determines the final webhook body
shape.

External contract (VERIFIED, tap-buddy 2026-07-04 live dev instance, see
AD-002's External contracts table): `{"contact_id", "phone", "contact_name",
"text", "organization_id"}`; `@input.*` does NOT exist in webhook bodies —
text arrives only via a preceding wait-router result. There is no
per-message id variable in Glific's webhook context — this parser generates
one when absent (dedupe, Task 5, still protects against true BSP/HTTP
retries which resend an identical explicit id).

Security-relevant coupling (CR-010/architecture-reviewer C-6.1,
2026-07-25): `app.ingress.contact_allowlist.verify_contact_allowlisted`
matches its allowlist against the EXACT `contact_id`/`phone` strings this
function returns — the simulator `_N` phone-suffix stripping, the
`@`-expression-as-absent handling, and the `contact`-object fallback path
are therefore now load-bearing for WHO may reach the agent, not just for
LMS lookup correctness. A future edit here that changes any of those three
behaviors silently widens or narrows the contact allowlist — see
`tests/ingress/test_contact_allowlist.py`'s parser-normalization
regression tests, which pin the exact behaviors the allowlist gate relies
on.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from pydantic import BaseModel


class GlificWebhookPayload(BaseModel):
    contact_id: str
    phone: str
    message_id: str
    message_type: str = "text"  # "text" | "audio"
    text: str | None = None
    media_url: str | None = None
    contact_name: str | None = None


def _resolved(value: Any) -> Any:
    # Glific renders unknown flow variables literally ("@input.uuid",
    # "@results.child_reply") — verified live 2026-07-03/04. Treat any
    # leftover @-expression as absent.
    if isinstance(value, str) and value.startswith("@"):
        return None
    return value


def parse_glific_webhook_payload(raw: dict[str, Any]) -> GlificWebhookPayload:
    """Raises ValueError on missing required fields — caller (the FastAPI
    route) maps that to a 4xx, never a 5xx (CR-004 Task 3 acceptance
    criteria).
    """
    contact_raw = raw.get("contact")
    contact: dict[str, Any] = contact_raw if isinstance(contact_raw, dict) else {}
    message_raw = raw.get("message")
    message: dict[str, Any] = message_raw if isinstance(message_raw, dict) else {}

    contact_id = str(_resolved(raw.get("contact_id")) or contact.get("id") or "")
    phone = str(_resolved(raw.get("phone")) or contact.get("phone") or "")
    # Glific simulator contacts carry an "_N" phone suffix (9876543210_1);
    # real WhatsApp numbers never contain "_". Strip it so simulator
    # sessions resolve against the LMS.
    phone = re.sub(r"_\d+$", "", phone)
    message_id = str(
        _resolved(raw.get("message_id")) or message.get("id") or raw.get("id") or uuid4().hex
    )
    text = _resolved(raw.get("text")) or message.get("body") or raw.get("body")

    media_raw = message.get("media")
    media: dict[str, Any] = media_raw if isinstance(media_raw, dict) else {}
    media_url = raw.get("media_url") or media.get("url")
    message_type = "audio" if media_url else "text"
    contact_name = raw.get("contact_name") or contact.get("name")

    if not contact_id or not phone or not message_id:
        raise ValueError("missing required fields: contact_id, phone, message_id")

    return GlificWebhookPayload(
        contact_id=contact_id,
        phone=phone,
        message_id=message_id,
        message_type=message_type,
        text=text,
        media_url=media_url,
        contact_name=contact_name,
    )
