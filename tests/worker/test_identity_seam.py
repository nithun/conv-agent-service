"""AC-7/R-7 (Task 9): identity/session-ownership seam confirmation — Redis
session state is keyed by `glific_contact_id` (or equivalent) and never
independently re-derives or overrides contact/conversation identity.
Glific remains sole authority for who the contact is; the agent's Redis
state holds only conversational session data.
"""

from __future__ import annotations

import dataclasses

import fakeredis.aioredis
import pytest

from app.config.settings import Settings
from app.core.state_machine import SessionData, StudentIdentity, new_session
from app.worker.session_store import RedisSessionStore, session_key


def test_session_key_is_derived_from_glific_contact_id_only() -> None:
    """The Redis key itself is a direct function of glific_contact_id — no
    other identity source is consulted to build it.
    """
    assert session_key("42") == "tap_agent:session:42"
    assert session_key("simulator-3") == "tap_agent:session:simulator-3"


def test_session_data_has_no_competing_identity_field() -> None:
    """`SessionData`'s own field set carries no field that could function
    as a second, competing identity source for the CONTACT/CONVERSATION
    (as opposed to the STUDENT, which is a legitimate LMS-side concept
    resolved FROM the Glific-provided phone — StudentIdentity's
    student_id is about the LMS record, not about which Glific contact/
    conversation this is).
    """
    field_names = {f.name for f in dataclasses.fields(SessionData)}
    # The only Glific-provided identity fields are glific_contact_id and
    # phone (both sourced from the webhook payload, never invented here).
    forbidden_competing_fields = {
        "conversation_id",
        "glific_session_id",
        "internal_contact_id",
    }
    assert not (field_names & forbidden_competing_fields)
    assert "glific_contact_id" in field_names
    assert "phone" in field_names


@pytest.mark.asyncio
async def test_loaded_session_preserves_the_same_glific_contact_id_it_was_saved_under() -> None:
    """A round-trip through RedisSessionStore never rewrites or re-derives
    glific_contact_id — what comes back is exactly what was saved.
    """
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    settings = Settings(_env_file=None)
    store = RedisSessionStore(redis, settings)

    session = new_session("contact-99", "919999999999", needs_identity_confirmation=False)
    session.identity = StudentIdentity(student_id="STU-001", child_name="Aarav")
    await store.save_session(session)

    loaded = await store.load_session("contact-99")
    assert loaded is not None
    assert loaded.glific_contact_id == "contact-99"
    # The LMS-resolved student_id lives under `identity`, a distinct,
    # clearly-scoped sub-structure — never promoted to stand in for
    # glific_contact_id anywhere in the stored shape.
    assert loaded.identity.student_id == "STU-001"
    assert loaded.glific_contact_id != loaded.identity.student_id
