"""State machine acceptance tests: full happy path (single-match +
multi-match identity), re-explanation cap, ASR-failure cap, session expiry.

Re-targeted from tap-buddy/tests/test_state_machine.py against
app.core.state_machine (CR-004 Task 7) — equivalent coverage, not a literal
file copy.
"""

from __future__ import annotations

import time

import pytest

from app.core.state_machine import (
    MAX_ASR_FAILURES,
    MAX_REEXPLANATIONS,
    EscalationReason,
    InvalidTransition,
    SessionState,
    can_transition,
    escalate,
    new_session,
    record_asr_failure,
    record_reexplanation,
    transition,
)


def test_single_match_skips_identity_confirmation():
    session = new_session("contact-1", "917834811114", needs_identity_confirmation=False)
    assert session.state == SessionState.GREETING


def test_multi_match_enters_identity_confirmation():
    session = new_session("contact-2", "919999999999", needs_identity_confirmation=True)
    assert session.state == SessionState.IDENTITY_CONFIRMATION


def test_happy_path_single_match_full_transition_sequence():
    session = new_session("contact-3", "917834811114", needs_identity_confirmation=False)
    assert session.state == SessionState.GREETING

    transition(session, SessionState.MODE_CHOICE)
    assert session.state == SessionState.MODE_CHOICE
    assert session.turn_count == 1

    transition(session, SessionState.CONTENT_INTRO)
    assert session.state == SessionState.CONTENT_INTRO

    transition(session, SessionState.ACTIVITY_IN_PROGRESS)
    assert session.state == SessionState.ACTIVITY_IN_PROGRESS

    transition(session, SessionState.REDIRECT)
    assert session.state == SessionState.REDIRECT

    transition(session, SessionState.CLOSE)
    assert session.state == SessionState.CLOSE
    assert session.turn_count == 5


def test_happy_path_multi_match_resolves_then_proceeds():
    session = new_session("contact-4", "919999999999", needs_identity_confirmation=True)
    assert session.state == SessionState.IDENTITY_CONFIRMATION

    transition(session, SessionState.GREETING)
    assert session.state == SessionState.GREETING

    transition(session, SessionState.MODE_CHOICE)
    transition(session, SessionState.CONTENT_INTRO)
    transition(session, SessionState.ACTIVITY_IN_PROGRESS)
    transition(session, SessionState.CLOSE)
    assert session.state == SessionState.CLOSE


def test_doubt_clearing_is_re_entrant():
    session = new_session("contact-5", "917834811114", needs_identity_confirmation=False)
    transition(session, SessionState.MODE_CHOICE)
    transition(session, SessionState.CONTENT_INTRO)
    transition(session, SessionState.ACTIVITY_IN_PROGRESS)
    transition(session, SessionState.DOUBT_CLEARING)
    assert session.state == SessionState.DOUBT_CLEARING
    transition(session, SessionState.DOUBT_CLEARING)
    assert session.state == SessionState.DOUBT_CLEARING


def test_reexplanation_cap_forces_escalation_not_third_reexplanation():
    session = new_session("contact-6", "917834811114", needs_identity_confirmation=False)
    transition(session, SessionState.MODE_CHOICE)
    transition(session, SessionState.CONTENT_INTRO)
    transition(session, SessionState.ACTIVITY_IN_PROGRESS)
    transition(session, SessionState.DOUBT_CLEARING)

    assert record_reexplanation(session) is None
    assert session.reexplanation_count == 1
    assert session.state == SessionState.DOUBT_CLEARING

    assert record_reexplanation(session) is None
    assert session.reexplanation_count == 2
    assert session.state == SessionState.DOUBT_CLEARING

    reason = record_reexplanation(session)
    assert reason == EscalationReason.UNRESOLVED_DOUBT
    assert session.state == SessionState.ESCALATED
    assert session.escalation_reason == EscalationReason.UNRESOLVED_DOUBT
    assert session.reexplanation_count == MAX_REEXPLANATIONS + 1


def test_asr_failure_cap_signals_fallback_after_two_consecutive():
    session = new_session("contact-7", "917834811114", needs_identity_confirmation=False)

    assert record_asr_failure(session) is False
    assert session.asr_failure_count == 1

    assert record_asr_failure(session) is True
    assert session.asr_failure_count == MAX_ASR_FAILURES
    assert session.state != SessionState.ESCALATED


def test_escalate_reachable_from_any_non_terminal_state():
    session = new_session("contact-8", "917834811114", needs_identity_confirmation=False)
    escalate(session, EscalationReason.DISTRESS)
    assert session.state == SessionState.ESCALATED
    assert session.escalation_reason == EscalationReason.DISTRESS


def test_escalate_is_idempotent_once_terminal():
    session = new_session("contact-9", "917834811114", needs_identity_confirmation=False)
    escalate(session, EscalationReason.DISTRESS)
    updated_at_before = session.updated_at
    escalate(session, EscalationReason.ASR_FAILURE)
    assert session.escalation_reason == EscalationReason.DISTRESS
    assert session.updated_at == updated_at_before


def test_invalid_transition_raises():
    session = new_session("contact-10", "917834811114", needs_identity_confirmation=False)
    with pytest.raises(InvalidTransition):
        transition(session, SessionState.CLOSE)


def test_can_transition_helper():
    assert can_transition(SessionState.GREETING, SessionState.MODE_CHOICE) is True
    assert can_transition(SessionState.GREETING, SessionState.CLOSE) is False


def test_session_expiry_after_ttl():
    session = new_session("contact-11", "917834811114", needs_identity_confirmation=False)
    now = time.time()
    session.updated_at = now - (31 * 60)
    assert session.is_expired(now=now) is True


def test_session_not_expired_within_ttl():
    session = new_session("contact-12", "917834811114", needs_identity_confirmation=False)
    now = time.time()
    session.updated_at = now - (10 * 60)
    assert session.is_expired(now=now) is False
