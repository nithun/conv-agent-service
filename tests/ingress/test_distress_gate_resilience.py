"""Live-integration-pass finding (2026-07-05, Phase 7): a downstream Redis
failure during the escalation-enqueue round-trip must never turn the
fast-ack webhook response into a 500 (L-002) — the webhook's "ack fast"
contract does not have a carve-out for "unless the escalation enqueue had a
problem."

Code-review fix (2026-07-05, Finding 1): the ingress route no longer calls
`distress_handler` (real Glific network calls) inline — it only does a
second cheap XADD onto the escalation stream
(`app/queue/escalation_queue.py::enqueue_escalation`). This test now
exercises resilience to a failure in THAT enqueue call (the only
Glific/Redis-adjacent side effect left on the pre-ack path for a distress
hit), preserving the original property this test protected (an exception
downstream of the deterministic distress detection must never 500 the
webhook).
"""

from __future__ import annotations

import pytest

import app.ingress.glific_webhook as glific_webhook_module


@pytest.mark.asyncio
async def test_escalation_enqueue_failure_does_not_500_the_webhook_response(
    client, monkeypatch
) -> None:
    async def _failing_enqueue_escalation(redis, payload):
        raise RuntimeError("simulated Redis outage during escalation enqueue")

    monkeypatch.setattr(
        glific_webhook_module, "enqueue_escalation", _failing_enqueue_escalation
    )

    payload = {
        "contact_id": "30",
        "phone": "919999999999",
        "message_id": "resilience-1",
        "text": "I want to kill myself",
    }
    response = await client.post("/webhook/glific", json=payload)

    assert response.status_code == 200
    assert response.json()["escalated"] is True
