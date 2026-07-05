"""AC-9/Task 8 (BR-003, Invariant 4): the top-level distress gate is proven
reachable from the LIVE ingress entrypoint for a message arriving in EVERY
early state (identity_confirmation, greeting, mode_choice) — per L-008, an
e2e test through the actual `/webhook/glific` route, not a direct call to
`handle_distress`. Mirrors tap-buddy's `test_distress_at_*` suite, redone
against this new entrypoint.
"""

from __future__ import annotations

import pytest

from app.core.state_machine import EscalationReason, SessionState
from app.worker.session_store import session_key


@pytest.mark.asyncio
async def test_distress_at_identity_confirmation_no_candidates_escalates(
    client_with_distress_handler,
) -> None:
    """A brand-new contact (no session at all yet) sending a distressing
    message must still escalate — the escalation record's identity may be
    null, that's acceptable (safety over attribution).
    """
    client, fake_glific = client_with_distress_handler
    payload = {
        "contact_id": "10",
        "phone": "919999999999",
        "message_id": "d1",
        "text": "I want to kill myself",
    }
    response = await client.post("/webhook/glific", json=payload)

    assert response.status_code == 200
    assert response.json()["escalated"] is True
    assert len(fake_glific.sent_texts) == 1
    assert fake_glific.sent_texts[0][0] == "10"
    assert len(fake_glific.tickets) == 1

    redis = client._transport.app.state.redis
    session_fields = await redis.hgetall(session_key("10"))
    assert session_fields["state"] == SessionState.ESCALATED.value
    assert session_fields["escalation_reason"] == EscalationReason.DISTRESS.value


@pytest.mark.asyncio
async def test_distress_at_greeting_escalates_and_does_not_store_as_readiness(
    client_with_distress_handler,
) -> None:
    """A session already in GREETING (post-identity-resolution) that sends
    a distress message must escalate — the text must NOT be captured as
    the normal readiness_response.
    """
    client, fake_glific = client_with_distress_handler
    session_store = client._transport.app.state.session_store

    from app.core.state_machine import StudentIdentity, new_session

    session = new_session("11", "919999999999", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-1", child_name="Aarav")
    await session_store.save_session(session)

    payload = {
        "contact_id": "11",
        "phone": "919999999999",
        "message_id": "d2",
        "text": "my dad keeps hitting me",
    }
    response = await client.post("/webhook/glific", json=payload)

    assert response.status_code == 200
    assert response.json()["escalated"] is True

    loaded = await session_store.load_session("11")
    assert loaded is not None
    assert loaded.state == SessionState.ESCALATED
    assert loaded.readiness_response is None


@pytest.mark.asyncio
async def test_distress_at_mode_choice_escalates(client_with_distress_handler) -> None:
    client, fake_glific = client_with_distress_handler
    session_store = client._transport.app.state.session_store

    from app.core.state_machine import StudentIdentity, new_session, transition

    session = new_session("12", "919999999999", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-1", child_name="Aarav")
    transition(session, SessionState.MODE_CHOICE)
    await session_store.save_session(session)

    payload = {
        "contact_id": "12",
        "phone": "919999999999",
        "message_id": "d3",
        "text": "I'm so scared right now",
    }
    response = await client.post("/webhook/glific", json=payload)

    assert response.status_code == 200
    assert response.json()["escalated"] is True

    loaded = await session_store.load_session("12")
    assert loaded is not None
    assert loaded.state == SessionState.ESCALATED
    assert loaded.escalation_reason == EscalationReason.DISTRESS


@pytest.mark.asyncio
async def test_distress_reply_always_sent_even_if_ticket_creation_fails(
    client_with_distress_handler,
) -> None:
    """A child-safety close reply must go out even if the Ticket call
    itself fails — the reply already tells the child a human is coming.
    """
    client, fake_glific = client_with_distress_handler

    async def _failing_create_ticket(*args, **kwargs):
        raise RuntimeError("simulated Glific Ticketing outage")

    fake_glific.create_ticket = _failing_create_ticket  # type: ignore[method-assign]

    payload = {
        "contact_id": "13",
        "phone": "919999999999",
        "message_id": "d4",
        "text": "I want to kill myself",
    }
    response = await client.post("/webhook/glific", json=payload)

    assert response.status_code == 200
    assert len(fake_glific.sent_texts) == 1  # close reply still sent
