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

import app.ingress.glific_webhook as glific_webhook_module
from app.ingress.contact_allowlist import ContactAllowlist
from app.queue.escalation_queue import ESCALATION_STREAM_KEY
from app.queue.settings import stream_key
from tests.ingress._webhook_signing import sign_webhook_body
from tests.ingress.conftest import TEST_WEBHOOK_SECRET


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
async def test_non_dict_json_body_returns_4xx_not_5xx(client) -> None:
    """F-067: well-formed JSON whose top level is not an object (a list,
    string, or number) used to AttributeError inside the parser and 500.
    It is malformed the same way a missing field is — 4xx, never 5xx.
    """
    for body in ([1, 2, 3], "hi", 3):
        response = await client.post("/webhook/glific", json=body)
        assert 400 <= response.status_code < 500, f"body {body!r} -> {response.status_code}"


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


# ---- CR-010 (historical, allowlist ACTIVE): allowlisted contact_id/phone --
# still succeed under the old, now-superseded gate shape. Kept as regression
# coverage that adding CR-011's open-population removal never narrows who
# may reach the route.


@pytest.mark.asyncio
async def test_allowlist_fixture_contact_id_still_acks_200_and_proceeds(raw_client) -> None:
    """A contact_id that WOULD have been allowlisted under CR-010 still
    reaches 200 today — CR-011 only widens the population, it never
    narrows it.
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


# ---- CR-011 (2026-07-25, ADR-013): allowlist Depends() removed -------------


@pytest.mark.asyncio
async def test_signature_valid_never_before_seen_contact_is_not_rejected_403(
    raw_client,
) -> None:
    """CR-011's own Acceptance criterion, verbatim: "a signature-valid
    request from a contact_id NOT in any configured allowlist value still
    succeeds (200/enqueued) once signature-valid." Adversarial fixture
    (L-017): a realistic, well-formed, never-before-seen contact_id/phone
    pair — neither is in `FIXTURE_ALLOWLISTED_CONTACT_IDS`/
    `FIXTURE_ALLOWLISTED_PHONE`, and this is exactly the payload shape
    CR-010's own now-removed rejection test used to 403 on.
    """
    payload = {
        "contact_id": "999999",
        "phone": "9199999999",
        "message_id": "cr011-open-population-1",
        "text": "hello there, this is a completely normal message",
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
    entries = await raw_client._transport.app.state.redis.xrange(stream_key("999999"))
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_signature_valid_never_before_seen_contact_with_distress_text_still_escalates(
    raw_client,
) -> None:
    """The distress-gate consequence ADR-013 records explicitly (not a
    regression, but a documented behavior change from CR-010's own
    boundary): because population is no longer allowlist-bounded, a
    never-before-seen contact whose message trips the distress gate now
    DOES reach it and gets escalated — the opposite of what CR-010's own
    N2 rejection test proved.
    """
    payload = {
        "contact_id": "999999",
        "phone": "9199999999",
        "message_id": "cr011-open-population-distress-1",
        "text": "I want to kill myself",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body(TEST_WEBHOOK_SECRET, body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 200
    assert response.json().get("escalated") is True
    escalation_entries = await raw_client._transport.app.state.redis.xrange(
        ESCALATION_STREAM_KEY
    )
    assert len(escalation_entries) == 1
    assert escalation_entries[0][1]["contact_id"] == "999999"


@pytest.mark.asyncio
async def test_invalid_signature_from_never_before_seen_contact_still_returns_401(
    raw_client,
) -> None:
    """The signature-verification dependency (CR-009) is UNCHANGED and
    still runs — removing the allowlist gate does not also remove
    authenticity verification. An unsigned request is still rejected 401,
    regardless of population.
    """
    payload = {
        "contact_id": "999999",
        "phone": "9199999999",
        "message_id": "cr011-no-sig-1",
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


def test_route_dependency_order_is_signature_verification_only() -> None:
    """Static structural guard, CR-011's own Acceptance criterion: the
    route's `dependencies=` list no longer includes
    `verify_contact_allowlisted` — `verify_glific_webhook_signature`
    (CR-009) remains and is the ONLY entry.
    """
    from app.ingress.webhook_auth import verify_glific_webhook_signature

    route = next(
        r
        for r in glific_webhook_module.router.routes
        if r.path == "/webhook/glific"  # type: ignore[attr-defined]
    )
    dependency_callables = [dep.call for dep in route.dependant.dependencies]  # type: ignore[attr-defined]

    assert dependency_callables == [verify_glific_webhook_signature]


@pytest.mark.asyncio
async def test_contact_allowlist_state_override_no_longer_affects_the_route(
    raw_client,
) -> None:
    """Proves `app.ingress.contact_allowlist` is genuinely DORMANT on this
    route, not merely un-imported: setting `app.state.contact_allowlist` to
    an EMPTY set — which under CR-010 would have 403'd every request,
    including this one's normally-allowlisted contact_id — now has zero
    effect, because the dependency reading that state is no longer wired
    onto `/webhook/glific` at all.
    """
    raw_client._transport.app.state.contact_allowlist = ContactAllowlist(tokens=frozenset())

    payload = {
        "contact_id": "7",  # normally in FIXTURE_ALLOWLISTED_CONTACT_IDS
        "phone": "919999999999",
        "message_id": "cr011-dormant-override-check-1",
        "text": "hello",
    }
    body = json.dumps(payload).encode()
    header = sign_webhook_body(TEST_WEBHOOK_SECRET, body)

    response = await raw_client.post(
        "/webhook/glific",
        content=body,
        headers={"content-type": "application/json", "X-Glific-Signature": header},
    )

    assert response.status_code == 200
