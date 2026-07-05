"""Payload-parsing tests: real Glific payload shape (accepted) and
malformed shapes (raise ValueError -> caller maps to 4xx). Ported/
retargeted from tap-buddy's webhook payload-parsing test coverage (CR-004
Task 3).
"""

from __future__ import annotations

import pytest

from app.ingress.glific_payload import parse_glific_webhook_payload


def test_parses_real_glific_flow_webhook_shape() -> None:
    """Live-verified shape (AD-002 External contracts table, tap-buddy
    2026-07-04): {"contact_id", "phone", "contact_name", "text",
    "organization_id"}.
    """
    raw = {
        "contact_id": "2",
        "phone": "9876543210_1",
        "contact_name": "Buddy Test",
        "text": "hello",
        "organization_id": 1,
    }
    payload = parse_glific_webhook_payload(raw)

    assert payload.contact_id == "2"
    # simulator "_N" suffix stripped
    assert payload.phone == "9876543210"
    assert payload.contact_name == "Buddy Test"
    assert payload.text == "hello"
    assert payload.message_type == "text"
    assert payload.message_id  # generated, since Glific has no per-message id


def test_unresolved_at_expression_treated_as_absent() -> None:
    """Glific renders unknown flow variables literally
    ("@input.uuid","@results.child_reply") — must be treated as absent, not
    passed through literally.
    """
    raw = {
        "contact_id": "3",
        "phone": "919999999999",
        "text": "@results.child_reply",
        "message_id": "@input.uuid",
    }
    payload = parse_glific_webhook_payload(raw)

    assert payload.text is None
    # message_id falls back to a generated uuid since the @-expression is
    # treated as absent.
    assert payload.message_id != "@input.uuid"


def test_simulator_phone_suffix_stripped() -> None:
    raw = {"contact_id": "4", "phone": "9876543210_3", "message_id": "m1", "text": "hi"}
    payload = parse_glific_webhook_payload(raw)
    assert payload.phone == "9876543210"


def test_real_whatsapp_phone_without_underscore_unchanged() -> None:
    raw = {"contact_id": "5", "phone": "917834811114", "message_id": "m2", "text": "hi"}
    payload = parse_glific_webhook_payload(raw)
    assert payload.phone == "917834811114"


def test_key_alias_nested_contact_and_message_dicts() -> None:
    """Defensive key-alias handling: some webhook-node configs nest under
    contact/message dicts instead of flat top-level keys.
    """
    raw = {
        "contact": {"id": "6", "phone": "919999999999", "name": "Aarav"},
        "message": {"id": "msg-123", "body": "hello there"},
    }
    payload = parse_glific_webhook_payload(raw)

    assert payload.contact_id == "6"
    assert payload.phone == "919999999999"
    assert payload.contact_name == "Aarav"
    assert payload.message_id == "msg-123"
    assert payload.text == "hello there"


def test_media_url_present_sets_audio_message_type() -> None:
    raw = {
        "contact_id": "7",
        "phone": "919999999999",
        "message_id": "m3",
        "media_url": "https://example.com/audio.ogg",
    }
    payload = parse_glific_webhook_payload(raw)
    assert payload.message_type == "audio"
    assert payload.media_url == "https://example.com/audio.ogg"


def test_missing_contact_id_raises_value_error() -> None:
    raw = {"phone": "919999999999", "message_id": "m4", "text": "hi"}
    with pytest.raises(ValueError):
        parse_glific_webhook_payload(raw)


def test_missing_phone_raises_value_error() -> None:
    raw = {"contact_id": "8", "message_id": "m5", "text": "hi"}
    with pytest.raises(ValueError):
        parse_glific_webhook_payload(raw)


def test_empty_payload_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_glific_webhook_payload({})
