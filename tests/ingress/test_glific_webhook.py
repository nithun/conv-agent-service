"""Ingress webhook route tests (CR-004 Task 3/5): accepted real payload
shape, malformed payloads (4xx not 5xx), fast-ack latency budget, dedupe
through the real path (AC-3a), and the top-level distress gate short-
circuiting BEFORE enqueue (AC-9's ingress half — the full e2e-through-
worker distress test lands with Task 8).

CR-009 (2026-07-17): every test above this point goes through the `client`/
`client_with_escalation_worker` fixtures, which auto-sign requests to
`/webhook/glific` (see `tests/ingress/conftest.py`) — so none of them
needed to change for the new signature-verification dependency; they all
continue to exercise POST-auth behavior exactly as before. The
`# ---- CR-009` section below adds the auth-specific route-level tests
(AC-3/AC-4/AC-6, "rejection happens before Redis"), using the `raw_client`
fixture (no auto-sign) for full control over the `X-Glific-Signature`
header.
"""

from __future__ import annotations

import json
import time

import pytest

import app.delivery.glific_client as glific_client_module
import app.ingress.glific_webhook as glific_webhook_module
import app.llm.router as llm_router_module
from app.ingress.contact_allowlist import ContactAllowlist
from app.queue.escalation_queue import ESCALATION_STREAM_KEY
from app.queue.settings import stream_key
from tests.ingress._webhook_signing import sign_webhook_body
from tests.ingress.conftest import (
    FIXTURE_ALLOWLISTED_PHONE,
    TEST_WEBHOOK_SECRET,
)


@pytest.mark.asyncio
async def test_valid_payload_acks_200_and_enqueues(client) -> None:
    payload = {
        "contact_id": "2",
        "phone": "9876543210_1",
        "contact_name": "Buddy Test",
        "text": "hello there",
        "organization_id": 1,
    }
    response = await client.post("/webhook/glific", json=payload)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    entries = await client._transport.app.state.redis.xrange(stream_key("2"))
    assert len(entries) == 1
    assert entries[0][1]["text"] == "hello there"


@pytest.mark.asyncio
async def test_malformed_json_body_returns_4xx_not_5xx(client) -> None:
    response = await client.post(
        "/webhook/glific", content=b"not json", headers={"content-type": "application/json"}
    )
    assert 400 <= response.status_code < 500


@pytest.mark.asyncio
async def test_missing_required_fields_returns_4xx_not_5xx(client) -> None:
    response = await client.post("/webhook/glific", json={"text": "hi, no contact info"})
    assert 400 <= response.status_code < 500


@pytest.mark.asyncio
async def test_empty_body_returns_4xx_not_5xx(client) -> None:
    response = await client.post(
        "/webhook/glific", content=b"", headers={"content-type": "application/json"}
    )
    assert 400 <= response.status_code < 500


@pytest.mark.asyncio
async def test_dedupe_duplicate_message_id_single_processing_via_real_ingress(client) -> None:
    """AC-3(a), run against the REAL ingress route (not a direct call to
    is_duplicate_message) — a duplicate message_id delivered twice produces
    exactly one enqueued turn.
    """
    payload = {
        "contact_id": "3",
        "phone": "919999999999",
        "message_id": "fixed-msg-id-1",
        "text": "hello",
    }

    first = await client.post("/webhook/glific", json=payload)
    second = await client.post("/webhook/glific", json=payload)

    assert first.status_code == 200
    assert first.json().get("duplicate") is not True
    assert second.status_code == 200
    assert second.json().get("duplicate") is True

    entries = await client._transport.app.state.redis.xrange(stream_key("3"))
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_ordering_rapid_messages_same_contact_via_real_ingress(client) -> None:
    """AC-3(b)/L-003, run against the REAL ingress route: >=2 rapid messages
    from the same contact_id are enqueued in the order sent.
    """
    contact_id = "4"
    for i in range(5):
        payload = {
            "contact_id": contact_id,
            "phone": "917834811114",
            "message_id": f"msg-{i}",
            "text": f"message {i}",
        }
        response = await client.post("/webhook/glific", json=payload)
        assert response.status_code == 200

    entries = await client._transport.app.state.redis.xrange(stream_key(contact_id))
    texts = [e[1]["text"] for e in entries]
    assert texts == [f"message {i}" for i in range(5)]


@pytest.mark.asyncio
async def test_distress_message_does_not_enqueue_for_normal_processing(client) -> None:
    """Top-level distress gate (BR-003/AD-002 data flow): a distress-hit
    message is intercepted BEFORE enqueue — the worker's state machine
    never sees it as a normal turn. It is instead enqueued onto the
    dedicated escalation stream (code-review fix, 2026-07-05, Finding 1) —
    the Glific-facing side effects run later, in the worker, never inline
    on this ack path.
    """
    payload = {
        "contact_id": "5",
        "phone": "919999999999",
        "message_id": "distress-1",
        "text": "I want to kill myself",
    }
    response = await client.post("/webhook/glific", json=payload)

    assert response.status_code == 200
    assert response.json().get("escalated") is True

    entries = await client._transport.app.state.redis.xrange(stream_key("5"))
    assert entries == []

    escalation_entries = await client._transport.app.state.redis.xrange(ESCALATION_STREAM_KEY)
    assert len(escalation_entries) == 1
    assert escalation_entries[0][1]["contact_id"] == "5"


@pytest.mark.asyncio
async def test_fast_ack_latency_budget_under_200ms(client) -> None:
    """Re-verifies the fast-ack budget on this new path (ADR-010 Invariant
    2) — not assumed equivalent to tap-buddy's because both "ack fast."
    """
    payload = {
        "contact_id": "6",
        "phone": "919999999999",
        "message_id": "latency-check-1",
        "text": "hello",
    }
    start = time.monotonic()
    response = await client.post("/webhook/glific", json=payload)
    elapsed_ms = (time.monotonic() - start) * 1000

    assert response.status_code == 200
    assert elapsed_ms < 200, f"webhook ack took {elapsed_ms:.1f}ms, budget is 200ms"


@pytest.mark.asyncio
async def test_fast_ack_latency_budget_under_concurrent_load(client) -> None:
    """p95 webhook response time under a defined concurrent load (WBS 1.4's
    original DoD, now CR-004's literal re-verification requirement).
    """
    import asyncio

    async def _send(i: int) -> float:
        payload = {
            "contact_id": f"load-{i}",
            "phone": "919999999999",
            "message_id": f"load-msg-{i}",
            "text": "hello",
        }
        start = time.monotonic()
        response = await client.post("/webhook/glific", json=payload)
        assert response.status_code == 200
        return (time.monotonic() - start) * 1000

    latencies = await asyncio.gather(*[_send(i) for i in range(50)])
    latencies.sort()
    p95_index = int(len(latencies) * 0.95)
    p95 = latencies[p95_index]
    assert p95 < 200, f"p95 webhook latency under concurrent load was {p95:.1f}ms"


# ---- CR-009: X-Glific-Signature verification -------------------------------


@pytest.mark.asyncio
async def test_correctly_signed_request_acks_200_and_proceeds_exactly_as_before(
    raw_client,
) -> None:
    """AC-3: a request with a correctly-signed, fresh X-Glific-Signature
    header returns 200 and proceeds exactly as before this CR (dedupe/
    distress-gate/enqueue all still run) — verified through the REAL
    /webhook/glific route, not just the isolated dependency
    (tests/ingress/test_webhook_auth.py).
    """
    payload = {
        "contact_id": "60",
        "phone": "919999999999",
        "message_id": "auth-valid-1",
        "text": "hello there",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body(TEST_WEBHOOK_SECRET, body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ok"

    entries = await raw_client._transport.app.state.redis.xrange(stream_key("60"))
    assert len(entries) == 1
    assert entries[0][1]["text"] == "hello there"


@pytest.mark.asyncio
async def test_correctly_signed_request_over_raw_bytes_with_unusual_formatting_still_verifies(
    raw_client,
) -> None:
    """Route-level counterpart to the isolated raw-body test
    (test_webhook_auth.py): a body whose whitespace/key-order would NOT
    survive a json.dumps(json.loads(body)) round-trip byte-identically must
    still verify AND parse correctly through the real payload parser.
    """
    body = (
        b'{  "message_id" :  "auth-rawbytes-1", "contact_id":"61",'
        b'"phone":"919999999999" , "text":"hello raw bytes"}'
    )
    header = sign_webhook_body(TEST_WEBHOOK_SECRET, body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 200
    entries = await raw_client._transport.app.state.redis.xrange(stream_key("61"))
    assert len(entries) == 1
    assert entries[0][1]["text"] == "hello raw bytes"


@pytest.mark.asyncio
async def test_missing_signature_header_returns_401_and_writes_nothing_to_redis(
    raw_client,
) -> None:
    """AC-4(a)."""
    payload = {
        "contact_id": "62",
        "phone": "919999999999",
        "message_id": "auth-missing-1",
        "text": "hello",
    }
    body = json.dumps(payload).encode()

    response = await raw_client.post(
        "/webhook/glific", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    entries = await raw_client._transport.app.state.redis.xrange(stream_key("62"))
    assert entries == []


@pytest.mark.asyncio
async def test_malformed_signature_header_returns_401_and_writes_nothing_to_redis(
    raw_client,
) -> None:
    """AC-4(b)."""
    payload = {
        "contact_id": "63",
        "phone": "919999999999",
        "message_id": "auth-malformed-1",
        "text": "hello",
    }
    body = json.dumps(payload).encode()

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": "not-even-shaped"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    entries = await raw_client._transport.app.state.redis.xrange(stream_key("63"))
    assert entries == []


@pytest.mark.asyncio
async def test_wrong_signature_returns_401_and_writes_nothing_to_redis(raw_client) -> None:
    """AC-4(c)."""
    payload = {
        "contact_id": "64",
        "phone": "919999999999",
        "message_id": "auth-wrong-1",
        "text": "hello",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body("definitely-not-the-configured-secret", body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    entries = await raw_client._transport.app.state.redis.xrange(stream_key("64"))
    assert entries == []


@pytest.mark.asyncio
async def test_stale_timestamp_returns_401_and_writes_nothing_to_redis(raw_client) -> None:
    """AC-4(d), past skew."""
    payload = {
        "contact_id": "65",
        "phone": "919999999999",
        "message_id": "auth-stale-1",
        "text": "hello",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body(TEST_WEBHOOK_SECRET, body, timestamp=int(time.time()) - 400)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    entries = await raw_client._transport.app.state.redis.xrange(stream_key("65"))
    assert entries == []


@pytest.mark.asyncio
async def test_future_skewed_timestamp_returns_401_and_writes_nothing_to_redis(raw_client) -> None:
    """AC-4(d), future skew."""
    payload = {
        "contact_id": "66",
        "phone": "919999999999",
        "message_id": "auth-future-1",
        "text": "hello",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body(TEST_WEBHOOK_SECRET, body, timestamp=int(time.time()) + 400)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    entries = await raw_client._transport.app.state.redis.xrange(stream_key("66"))
    assert entries == []


@pytest.mark.asyncio
async def test_rejected_distress_text_never_reaches_the_distress_gate(
    raw_client, monkeypatch
) -> None:
    """A wrongly-signed request carrying distress-triggering text must be
    rejected at the signature dependency, BEFORE `detect_distress` ever
    runs — the highest-consequence edge case CR-009's own Edge cases
    section names (a wrong/stale secret must never silently drop a
    distressed child's message past the point where anyone would notice,
    but it also must never let an unauthenticated caller trigger a real
    escalation Ticket).
    """
    calls = {"count": 0}
    original_detect = glific_webhook_module.detect_distress

    def _spy_detect(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["count"] += 1
        return original_detect(*args, **kwargs)

    monkeypatch.setattr(glific_webhook_module, "detect_distress", _spy_detect)

    payload = {
        "contact_id": "67",
        "phone": "919999999999",
        "message_id": "auth-distress-1",
        "text": "I want to kill myself",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body("wrong-secret-entirely", body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 401
    assert calls["count"] == 0
    escalation_entries = await raw_client._transport.app.state.redis.xrange(
        ESCALATION_STREAM_KEY
    )
    assert escalation_entries == []


# ---- CR-010: contact allowlist ---------------------------------------------


@pytest.mark.asyncio
async def test_allowlisted_contact_id_acks_200_and_proceeds_exactly_as_before(raw_client) -> None:
    """AC-4 (contact_id variant): a correctly-signed request from an
    allowlisted contact_id returns 200 and proceeds unchanged
    (dedupe/distress-gate/enqueue all still run).
    """
    payload = {
        "contact_id": "7",  # in conftest's FIXTURE_ALLOWLISTED_CONTACT_IDS
        "phone": "919999999999",
        "message_id": "ac4-contact-id-1",
        "text": "hello there",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body(TEST_WEBHOOK_SECRET, body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 200
    entries = await raw_client._transport.app.state.redis.xrange(stream_key("7"))
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_allowlisted_phone_acks_200_and_proceeds_exactly_as_before(raw_client) -> None:
    """AC-4 (phone variant): the contact_id itself is NOT on the
    allowlist, only the phone is — `ContactAllowlist.is_allowed` matches
    EITHER field.
    """
    payload = {
        "contact_id": "not-on-any-allowlist-fixture",
        "phone": FIXTURE_ALLOWLISTED_PHONE,
        "message_id": "ac4-phone-1",
        "text": "hello there",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body(TEST_WEBHOOK_SECRET, body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 200
    entries = await raw_client._transport.app.state.redis.xrange(
        stream_key("not-on-any-allowlist-fixture")
    )
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_non_allowlisted_contact_rejected_403_with_zero_side_effects(
    raw_client, monkeypatch
) -> None:
    """AC-5: adversarial fixture (L-017) — a fully valid, correctly SIGNED
    payload from a realistic non-allowlisted contact (neither contact_id
    nor phone on the allowlist, not a malformed body). Checklists AC-5a
    through AC-5f separately per L-021 (an enumerated 5-item "no side
    effect" deliverable does not get one blanket assertion).
    """
    send_text_calls = {"count": 0}
    create_ticket_calls = {"count": 0}
    generate_calls = {"count": 0}

    async def _spy_send_text(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        send_text_calls["count"] += 1
        return {"id": "should-never-be-called"}

    async def _spy_create_ticket(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        create_ticket_calls["count"] += 1
        return {"id": "should-never-be-called"}

    async def _spy_generate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        generate_calls["count"] += 1
        raise AssertionError("model_router.generate must never be called for a rejected contact")

    monkeypatch.setattr(glific_client_module.GlificClient, "send_text", _spy_send_text)
    monkeypatch.setattr(glific_client_module.GlificClient, "create_ticket", _spy_create_ticket)
    monkeypatch.setattr(llm_router_module.ModelRouter, "generate", _spy_generate)

    payload = {
        "contact_id": "999999",
        "phone": "9199999999",
        "message_id": "adversarial-1",
        "text": "hello there, this is a completely normal message",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body(TEST_WEBHOOK_SECRET, body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    # AC-5a
    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}

    # AC-5b: zero writes to the turn stream.
    turn_entries = await raw_client._transport.app.state.redis.xrange(stream_key("999999"))
    assert turn_entries == []

    # AC-5c: zero writes to the escalation stream (this payload's text is
    # not distress-triggering, confirming the "normal" rejection path is
    # also side-effect-free — the distress-specific N2 variant is the next
    # test).
    escalation_entries = await raw_client._transport.app.state.redis.xrange(
        ESCALATION_STREAM_KEY
    )
    assert escalation_entries == []

    # AC-5d/5e/5f: none of the real side-effecting calls ever ran.
    assert send_text_calls["count"] == 0
    assert create_ticket_calls["count"] == 0
    assert generate_calls["count"] == 0


@pytest.mark.asyncio
async def test_non_allowlisted_contact_with_distress_text_still_zero_escalation_entries(
    raw_client,
) -> None:
    """AC-5c's N2 variant — the highest-consequence edge case CR-010's own
    Edge cases section names: a non-allowlisted contact's message
    containing a live distress keyword must STILL be rejected before the
    top-level distress gate ever runs (Depends() dependencies run before
    the entire handler body, including detect_distress). Zero escalation
    entries proves rejection happens before the distress gate, not merely
    before the normal turn enqueue.
    """
    payload = {
        "contact_id": "999999",
        "phone": "9199999999",
        "message_id": "adversarial-distress-1",
        "text": "I want to kill myself",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body(TEST_WEBHOOK_SECRET, body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 403
    escalation_entries = await raw_client._transport.app.state.redis.xrange(
        ESCALATION_STREAM_KEY
    )
    assert escalation_entries == []
    turn_entries = await raw_client._transport.app.state.redis.xrange(stream_key("999999"))
    assert turn_entries == []


@pytest.mark.asyncio
async def test_invalid_signature_and_non_allowlisted_contact_returns_401_never_403(
    raw_client,
) -> None:
    """AC-9: dependency ORDER is enforced — an INVALID signature combined
    with a non-allowlisted contact_id must return 401 (the signature
    failure), never 403, proving `verify_glific_webhook_signature` runs
    strictly before `verify_contact_allowlisted`. An unsigned request from
    a non-allowlisted contact is the adversarial case: if the allowlist
    ever ran first, this would leak allowlist-membership information to an
    unauthenticated caller.
    """
    payload = {
        "contact_id": "999999",
        "phone": "9199999999",
        "message_id": "order-check-1",
        "text": "hello",
    }
    body = json.dumps(payload).encode()
    # No X-Glific-Signature header at all — raw_client sends no auto-sign.

    response = await raw_client.post(
        "/webhook/glific", content=body, headers={"content-type": "application/json"}
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    turn_entries = await raw_client._transport.app.state.redis.xrange(stream_key("999999"))
    assert turn_entries == []


@pytest.mark.asyncio
async def test_wrong_signature_and_non_allowlisted_contact_still_returns_401(raw_client) -> None:
    """AC-9's second flavor: a well-formed but WRONG signature (not merely
    missing) combined with a non-allowlisted contact still returns 401,
    not 403.
    """
    payload = {
        "contact_id": "999999",
        "phone": "9199999999",
        "message_id": "order-check-2",
        "text": "hello",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body("definitely-not-the-configured-secret", body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}


def test_route_dependency_order_is_signature_then_allowlist() -> None:
    """Static structural guard: the route's `dependencies=` list must carry
    `verify_glific_webhook_signature` BEFORE `verify_contact_allowlisted` —
    architecture-reviewer's endorsed order must never be silently
    reshuffled by a future ingress change.
    """
    from app.ingress.contact_allowlist import verify_contact_allowlisted
    from app.ingress.webhook_auth import verify_glific_webhook_signature

    route = next(
        r
        for r in glific_webhook_module.router.routes
        if r.path == "/webhook/glific"  # type: ignore[attr-defined]
    )
    dependency_callables = [dep.call for dep in route.dependant.dependencies]  # type: ignore[attr-defined]

    assert dependency_callables == [verify_glific_webhook_signature, verify_contact_allowlisted]


@pytest.mark.asyncio
async def test_contact_allowlist_override_after_client_construction_is_respected(
    raw_client,
) -> None:
    """Sanity check that a test may narrow/replace the fixture's default
    allowlist directly on `app.state` (used by CR-010's own rejection
    tests implicitly via ids absent from the fixture default, exercised
    here explicitly for a contact that WOULD otherwise be allowlisted by
    the fixture default).
    """
    raw_client._transport.app.state.contact_allowlist = ContactAllowlist(tokens=frozenset())

    payload = {
        "contact_id": "7",  # normally in FIXTURE_ALLOWLISTED_CONTACT_IDS
        "phone": "919999999999",
        "message_id": "override-check-1",
        "text": "hello",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body(TEST_WEBHOOK_SECRET, body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 403
