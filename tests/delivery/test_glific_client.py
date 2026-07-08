"""Glific GraphQL client tests (CR-004 Task 13/14): auth + token reuse,
forced renewal on 401, createAndSendMessage payload shape + F-003
sendMessage regression guard, media-send call, createTicket call, live
smoke test (marked integration, deferred to Phase 7).

Retargeted from tap-buddy/tests/test_glific_client.py against
app.delivery.glific_client.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.delivery.glific_client import GlificApiError, GlificClient

BASE_URL = "https://glific.test.example"


def _session_response(token="token-1", renewal="renew-1"):
    return httpx.Response(
        200,
        json={
            "data": {
                "access_token": token,
                "renewal_token": renewal,
                "token_expiry_time": "2099-01-01T00:00:00Z",
            }
        },
    )


@pytest.fixture
def client():
    return GlificClient(BASE_URL, "917834811114", "Secret1234!")


@pytest.mark.asyncio
@respx.mock
async def test_login_and_token_reuse_across_calls(client):
    login_route = respx.post(f"{BASE_URL}/api/v1/session").mock(return_value=_session_response())
    graphql_route = respx.post(f"{BASE_URL}/api").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "createAndSendMessage": {
                        "message": {"id": "msg-1", "body": "hi", "type": "TEXT"},
                        "errors": None,
                    }
                }
            },
        )
    )

    await client.send_text("contact-1", "hi")
    await client.send_text("contact-1", "hi again")

    assert login_route.call_count == 1
    assert graphql_route.call_count == 2
    last_headers = graphql_route.calls[-1].request.headers
    assert last_headers["authorization"] == "token-1"
    assert "Bearer" not in last_headers["authorization"]


@pytest.mark.asyncio
@respx.mock
async def test_forced_renewal_on_simulated_401(client):
    respx.post(f"{BASE_URL}/api/v1/session").mock(
        side_effect=[_session_response("token-1"), _session_response("token-2")]
    )
    graphql_route = respx.post(f"{BASE_URL}/api").mock(
        side_effect=[
            httpx.Response(401, text="unauthorized"),
            httpx.Response(
                200,
                json={
                    "data": {
                        "createAndSendMessage": {
                            "message": {"id": "msg-2", "body": "hi", "type": "TEXT"},
                            "errors": None,
                        }
                    }
                },
            ),
        ]
    )

    result = await client.send_text("contact-1", "hi")
    assert result["id"] == "msg-2"
    assert graphql_route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_create_and_send_message_payload_shape(client):
    respx.post(f"{BASE_URL}/api/v1/session").mock(return_value=_session_response())
    graphql_route = respx.post(f"{BASE_URL}/api").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "createAndSendMessage": {
                        "message": {"id": "msg-3", "body": "hello", "type": "TEXT"},
                        "errors": None,
                    }
                }
            },
        )
    )

    await client.send_text("contact-42", "hello")

    request_body = graphql_route.calls[0].request.content
    parsed = json.loads(request_body)
    assert "createAndSendMessage" in parsed["query"]
    assert "sendMessage(" not in parsed["query"]
    variables = parsed["variables"]["input"]
    assert variables["receiverId"] == "contact-42"
    assert variables["body"] == "hello"
    assert variables["type"] == "TEXT"
    assert variables["flow"] == "OUTBOUND"


@pytest.mark.asyncio
@respx.mock
async def test_media_send_call(client):
    respx.post(f"{BASE_URL}/api/v1/session").mock(return_value=_session_response())
    respx.post(f"{BASE_URL}/api").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": {
                        "createMessageMedia": {
                            "messageMedia": {"id": "media-1", "url": "http://x/audio.mp3"},
                            "errors": None,
                        }
                    }
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": {
                        "createAndSendMessage": {
                            "message": {"id": "msg-4", "body": "", "type": "AUDIO"},
                            "errors": None,
                        }
                    }
                },
            ),
        ]
    )

    result = await client.send_audio("contact-9", "http://x/audio.mp3")
    assert result["id"] == "msg-4"


@pytest.mark.asyncio
@respx.mock
async def test_create_ticket_call(client):
    """Task 13: the createTicket mutation, schema-introspection-verified
    against the live dev Glific instance (AD-002 External-contracts
    update) — this test confirms the CLIENT sends the verified shape, not
    the live mutation call itself (that's Phase 7's job).
    """
    respx.post(f"{BASE_URL}/api/v1/session").mock(return_value=_session_response())
    graphql_route = respx.post(f"{BASE_URL}/api").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "createTicket": {
                        "ticket": {"id": "ticket-1", "topic": "Distress escalation", "body": "..."},
                        "errors": None,
                    }
                }
            },
        )
    )

    result = await client.create_ticket("contact-5", "Distress escalation", "Details here")

    assert result["id"] == "ticket-1"
    request_body = graphql_route.calls[0].request.content
    parsed = json.loads(request_body)
    assert "createTicket" in parsed["query"]
    variables = parsed["variables"]["input"]
    assert variables["contactId"] == "contact-5"
    assert variables["topic"] == "Distress escalation"
    assert variables["body"] == "Details here"


@pytest.mark.asyncio
@respx.mock
async def test_graphql_error_raises(client):
    respx.post(f"{BASE_URL}/api/v1/session").mock(return_value=_session_response())
    respx.post(f"{BASE_URL}/api").mock(
        return_value=httpx.Response(200, json={"data": None, "errors": [{"message": "boom"}]})
    )
    with pytest.raises(GlificApiError):
        await client.send_text("contact-1", "hi")


def test_never_calls_send_message_regression_guard():
    """F-003 regression guard: the Glific client module must never
    reference a mutation literally named sendMessage anywhere in its
    source (that mutation does not exist in the live schema). Ported
    forward from tap-buddy's test_glific_client.py — re-targeted at this
    stack's delivery module, per CR-004's "regression-guard carry-forward"
    test-plan item.
    """
    client_path = Path(__file__).parent.parent.parent / "app" / "delivery" / "glific_client.py"
    source = client_path.read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "sendMessage(" not in node.value, (
                "found forbidden sendMessage( mutation reference"
            )


@pytest.mark.asyncio
@respx.mock
async def test_host_header_override_sent_when_configured():
    """F-021 local-dev-only workaround: when host_header_override is set,
    every request (auth + GraphQL) sends that value as the Host header,
    while still connecting to BASE_URL's own address (respx intercepts by
    URL, matching that the connection target is unchanged).
    """
    login_route = respx.post(f"{BASE_URL}/api/v1/session").mock(return_value=_session_response())
    graphql_route = respx.post(f"{BASE_URL}/api").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "createAndSendMessage": {
                        "message": {"id": "msg-override", "body": "hi", "type": "TEXT"},
                        "errors": None,
                    }
                }
            },
        )
    )

    overridden_client = GlificClient(
        BASE_URL,
        "917834811114",
        "Secret1234!",
        host_header_override="localhost:4000",
    )
    await overridden_client.send_text("contact-1", "hi")
    await overridden_client.aclose()

    assert login_route.calls[0].request.headers["host"] == "localhost:4000"
    assert graphql_route.calls[0].request.headers["host"] == "localhost:4000"


@pytest.mark.asyncio
@respx.mock
async def test_host_header_absent_by_default(client):
    """Default construction (no host_header_override, matching every
    existing caller/env-var-unset behavior) must send the Host header httpx
    derives from BASE_URL itself, not a fixed override — this must never
    silently activate anywhere it isn't explicitly opted into.
    """
    respx.post(f"{BASE_URL}/api/v1/session").mock(return_value=_session_response())
    graphql_route = respx.post(f"{BASE_URL}/api").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "createAndSendMessage": {
                        "message": {"id": "msg-default", "body": "hi", "type": "TEXT"},
                        "errors": None,
                    }
                }
            },
        )
    )

    await client.send_text("contact-1", "hi")

    sent_host = graphql_route.calls[0].request.headers["host"]
    assert sent_host != "localhost:4000"
    assert "glific.test.example" in sent_host
    assert client.host_header_override is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_glific_smoke():
    """Authenticates against the local dev Glific instance and sends one
    real text message to a test contact. Not run by default — CR-004 Phase
    7 (Task 16) is where this gets exercised for real, against
    docs/glific-dev-endpoint.md's instance and a real test contact id.
    """
    import os

    base_url = os.environ.get("GLIFIC_API_URL", "http://localhost:4000")
    phone = os.environ.get("GLIFIC_PHONE", "917834811114")
    password = os.environ.get("GLIFIC_PASSWORD", "Secret1234!")
    test_contact_id = os.environ.get("GLIFIC_TEST_CONTACT_ID")
    if not test_contact_id:
        pytest.skip("set GLIFIC_TEST_CONTACT_ID to run this smoke test")
    # F-021 local-dev-only workaround: when running this smoke test from
    # inside a Docker container against the local dev Glific instance, set
    # GLIFIC_HOST_HEADER_OVERRIDE=localhost:4000 in the test .env to bypass
    # SubdomainPlug's hostname allowlist gap. Empty/unset elsewhere (e.g.
    # from the host shell, or against staging) — never defaulted here.
    host_header_override = os.environ.get("GLIFIC_HOST_HEADER_OVERRIDE") or None

    client = GlificClient(
        base_url, phone, password, host_header_override=host_header_override
    )
    result = await client.send_text(test_contact_id, "tap-agent-service live smoke test message")
    assert result["id"]
    await client.aclose()
