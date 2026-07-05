"""Identity resolution tests: single-match resolves straight to
greeting-ready identity, multi-match stashes candidates for in-chat
disambiguation, confirm_identity matches by name.

Re-targeted from tap-buddy/tests/test_identity.py against
app.core.identity (CR-004 Task 9, AC-7).
"""

from __future__ import annotations

import pytest

from app.core.identity import confirm_identity, resolve_identity_by_name
from app.core.state_machine import SessionState
from app.tools.lms_mock import MockTapLmsClient


@pytest.mark.asyncio
async def test_single_candidate_resolves_identity_immediately():
    lms = MockTapLmsClient()
    resolution = await resolve_identity_by_name("contact-1", "917834811114", "Aarav", lms)
    assert resolution.needs_confirmation is False
    assert resolution.session.identity.student_id == "STU-001"
    assert resolution.session.identity.child_name == "Aarav"


@pytest.mark.asyncio
async def test_multi_candidate_needs_confirmation():
    lms = MockTapLmsClient()
    resolution = await resolve_identity_by_name("contact-2", "919999999999", "Meera", lms)
    assert resolution.needs_confirmation is True
    assert resolution.session.state == SessionState.IDENTITY_CONFIRMATION
    assert len(resolution.candidates) == 2


@pytest.mark.asyncio
async def test_unknown_phone_returns_no_candidates():
    lms = MockTapLmsClient()
    resolution = await resolve_identity_by_name("contact-3", "000", "Nobody", lms)
    assert resolution.candidates == []
    assert resolution.needs_confirmation is False


@pytest.mark.asyncio
async def test_confirm_identity_matches_by_name():
    lms = MockTapLmsClient()
    resolution = await resolve_identity_by_name("contact-4", "919999999999", "Meera", lms)
    session = resolution.session
    assert confirm_identity(session, "Kavya") is True
    assert session.identity.student_id == "STU-003"
    assert session.identity.child_name == "Kavya"


@pytest.mark.asyncio
async def test_confirm_identity_no_match_returns_false():
    lms = MockTapLmsClient()
    resolution = await resolve_identity_by_name("contact-5", "919999999999", "Meera", lms)
    session = resolution.session
    assert confirm_identity(session, "Someone Else") is False
