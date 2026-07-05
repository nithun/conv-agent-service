"""Live-integration-pass finding (2026-07-05, Phase 7): a downstream Glific
failure during the distress handler's close-reply send/Ticket-open must
never turn the fast-ack webhook response into a 500 (L-002) — the webhook's
"ack fast" contract does not have a carve-out for "unless the escalation
delivery had a problem." Caught live against the real dev Glific instance
returning an unexpected 403 (see Phase 7 live-integration-pass entry).
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_distress_handler_exception_does_not_500_the_webhook_response(client) -> None:
    async def _failing_distress_handler(payload):
        raise RuntimeError("simulated Glific outage during distress handling")

    client._transport.app.state.distress_handler = _failing_distress_handler

    payload = {
        "contact_id": "30",
        "phone": "919999999999",
        "message_id": "resilience-1",
        "text": "I want to kill myself",
    }
    response = await client.post("/webhook/glific", json=payload)

    assert response.status_code == 200
    assert response.json()["escalated"] is True
