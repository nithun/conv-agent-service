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
from app.tools.lms_client import StudentCandidate
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


# ---- CR-007: tap_language provenance through identity resolution ----------


@pytest.mark.asyncio
async def test_single_match_stores_the_raw_candidate_language():
    """CR-007 deviation (feature-builder): _identity_from_candidate no
    longer eagerly resolves through resolve_locale — session.identity
    carries the RAW LMS-supplied tap_language verbatim (here, Aarav's
    fixture value "Hindi", a real supported language, so this also
    incidentally proves parity with the old eager-resolved value).
    """
    lms = MockTapLmsClient()
    resolution = await resolve_identity_by_name("contact-6", "917834811114", "Aarav", lms)
    assert resolution.session.identity.tap_language == "Hindi"


@pytest.mark.asyncio
async def test_single_match_with_no_lms_language_leaves_tap_language_none():
    """GAP 0's trigger condition: when the LMS supplies no language at all
    (today's live-real case), session.identity.tap_language must be None —
    NOT silently coerced to "English" — so turn_processor.py's falsy check
    can offer the LANGUAGE_CHOICE turn.
    """
    lms = MockTapLmsClient(
        students={
            "911111111111": [
                StudentCandidate(student_id="STU-100", child_name="Priya", tap_language=None)
            ]
        }
    )
    resolution = await resolve_identity_by_name("contact-7", "911111111111", "Priya", lms)
    assert resolution.session.identity.tap_language is None


@pytest.mark.asyncio
async def test_single_match_with_unsupported_language_stores_it_raw_not_coerced_to_english():
    """Punjabi is `supported: false` in language_map.yaml (STT/TTS gap) but
    CR-007 scopes it as fine for scripted TEXT — the raw value must survive
    identity resolution, not get force-resolved to "English" here.
    """
    lms = MockTapLmsClient(
        students={
            "913333333333": [
                StudentCandidate(student_id="STU-200", child_name="Simran", tap_language="Punjabi")
            ]
        }
    )
    resolution = await resolve_identity_by_name("contact-8", "913333333333", "Simran", lms)
    assert resolution.session.identity.tap_language == "Punjabi"


@pytest.mark.asyncio
async def test_multi_candidate_stash_carries_tap_language():
    lms = MockTapLmsClient()  # 919999999999 -> Meera (English), Kavya (Tamil)
    resolution = await resolve_identity_by_name("contact-9", "919999999999", "Meera", lms)
    languages = {
        c["child_name"]: c.get("tap_language") for c in resolution.session.identity.candidates
    }
    assert languages == {"Meera": "English", "Kavya": "Tamil"}


@pytest.mark.asyncio
async def test_confirm_identity_sets_tap_language_from_the_matched_candidate():
    """AC-7: confirm_identity populates session.identity.tap_language on a
    successful multi-candidate confirm when the matched candidate carries
    one — matching _identity_from_candidate's single-match behavior.
    """
    lms = MockTapLmsClient()
    resolution = await resolve_identity_by_name("contact-10", "919999999999", "Meera", lms)
    session = resolution.session
    assert confirm_identity(session, "Kavya") is True
    assert session.identity.tap_language == "Tamil"


@pytest.mark.asyncio
async def test_confirm_identity_leaves_tap_language_none_when_candidate_has_none():
    lms = MockTapLmsClient(
        students={
            "914444444444": [
                StudentCandidate(student_id="STU-300", child_name="Aman", tap_language=None),
                StudentCandidate(student_id="STU-301", child_name="Zoya", tap_language="Urdu"),
            ]
        }
    )
    resolution = await resolve_identity_by_name("contact-11", "914444444444", "Aman", lms)
    session = resolution.session
    assert confirm_identity(session, "Aman") is True
    assert session.identity.tap_language is None
