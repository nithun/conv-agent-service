"""Session state machine — conversational state only.

Ported from `tap-buddy/src/tap_buddy/core/state_machine.py` (CR-004 Task 7).
Channel-agnostic by construction: this module must import nothing from
FastAPI, Redis, the Glific client, or the webhook-ingress module (mirrors
tap-buddy's Task 19 isolation discipline, re-verified here by
`tests/core/test_isolation.py`). Glific owns the contact/conversation; this
module owns only the ephemeral conversational state (turn count,
re-explanation cap, ASR-failure count, mode, readiness answer, resolved
language, identity) — the session-store SHAPE moved from tap-buddy's SQLite
to this service's Redis (R-2), but this module itself is storage-agnostic;
`app/worker/session_store.py` handles Redis serialization.

State never writes LMS progress/enrollment state directly (L-001 / AC-8) —
the only LMS write is `save_submission`, invoked from turn-orchestration
(`turn_logic.py`), never from here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SESSION_TTL_SECONDS = 30 * 60  # 30-min inactivity expiry — ported from tap-buddy (R-2)
MAX_REEXPLANATIONS = 2
MAX_ASR_FAILURES = 2
# CR-007 GAP 0: one scripted re-ask cap for the language-choice turn — on
# the SECOND non-match the session defaults to English rather than asking a
# third time (never blocks the conversation). See record_language_choice_attempt.
MAX_LANGUAGE_CHOICE_ATTEMPTS = 1


class SessionState(StrEnum):
    IDENTITY_CONFIRMATION = "identity_confirmation"
    # CR-007 GAP 0: entered from IDENTITY_CONFIRMATION only when identity
    # resolution didn't yield a tap_language (today's live-real case — the
    # LMS never returns one, see identity.py/turn_processor.py). Self-
    # retiring: once tap_language is populated at resolution time, this
    # state is structurally unreachable for that contact.
    LANGUAGE_CHOICE = "language_choice"
    GREETING = "greeting"
    MODE_CHOICE = "mode_choice"
    CONTENT_INTRO = "content_intro"
    ACTIVITY_IN_PROGRESS = "activity_in_progress"
    DOUBT_CLEARING = "doubt_clearing"
    REDIRECT = "redirect"
    CLOSE = "close"
    ESCALATED = "escalated"


class EscalationReason(StrEnum):
    DISTRESS = "distress"
    ASR_FAILURE = "asr_failure"
    UNRESOLVED_DOUBT = "unresolved_doubt"


class InvalidTransition(Exception):
    """Raised when a transition is attempted that the state machine forbids."""


# Explicit allowed-transition table. `doubt_clearing` is re-entrant (self-loop)
# capped by `reexplanation_count`; `escalated` is reachable from any non-terminal
# state (distress/ASR-failure/unresolved-doubt can occur at any point).
_ALLOWED_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.IDENTITY_CONFIRMATION: {
        SessionState.GREETING,
        SessionState.LANGUAGE_CHOICE,
        SessionState.ESCALATED,
    },
    SessionState.LANGUAGE_CHOICE: {SessionState.GREETING, SessionState.ESCALATED},
    SessionState.GREETING: {SessionState.MODE_CHOICE, SessionState.ESCALATED},
    SessionState.MODE_CHOICE: {SessionState.CONTENT_INTRO, SessionState.ESCALATED},
    SessionState.CONTENT_INTRO: {SessionState.ACTIVITY_IN_PROGRESS, SessionState.ESCALATED},
    SessionState.ACTIVITY_IN_PROGRESS: {
        SessionState.DOUBT_CLEARING,
        SessionState.REDIRECT,
        SessionState.CLOSE,
        SessionState.ESCALATED,
    },
    SessionState.DOUBT_CLEARING: {
        SessionState.DOUBT_CLEARING,  # re-entrant, capped externally
        SessionState.ACTIVITY_IN_PROGRESS,
        SessionState.REDIRECT,
        SessionState.ESCALATED,
    },
    SessionState.REDIRECT: {
        SessionState.ACTIVITY_IN_PROGRESS,
        SessionState.CLOSE,
        SessionState.ESCALATED,
    },
    SessionState.CLOSE: set(),
    SessionState.ESCALATED: set(),
}


@dataclass
class StudentIdentity:
    """The resolved student, plus disambiguation bookkeeping."""

    student_id: str | None = None
    child_name: str | None = None
    grade: str | None = None
    tap_language: str | None = None
    candidates: list[dict[str, Any]] = field(
        default_factory=list
    )  # raw LMS candidates, pre-confirmation


@dataclass
class SessionData:
    """Conversational state for one `(glific_contact_id)`.

    Persisted to Redis (`app/worker/session_store.py`), one hash per
    glific_contact_id — the storage-shape successor to tap-buddy's SQLite
    `SessionData` row (R-2).
    """

    glific_contact_id: str
    phone: str
    state: SessionState = SessionState.GREETING
    identity: StudentIdentity = field(default_factory=StudentIdentity)
    content_reference: dict[str, Any] | None = None  # resolved LMS "today's content" payload
    mode: str | None = None  # "voice" | "text"
    turn_count: int = 0
    reexplanation_count: int = 0
    asr_failure_count: int = 0
    # CR-007 GAP 0: unmatched-reply counter for the LANGUAGE_CHOICE turn.
    # Dataclass default 0 — an already-serialized Redis session hash from
    # before this field existed deserializes it as 0 with no migration
    # needed (mirrors asr_failure_count's existing shape).
    language_choice_attempts: int = 0
    # Code review fix (2026-07-17 High finding): the `message_id` whose
    # unmatched reply this session already counted toward
    # `language_choice_attempts` — makes the increment idempotent per
    # inbound message. A transient `send_text` failure on the LANGUAGE_CHOICE
    # branch (state doesn't change on the re-ask path, so a redelivered job
    # re-enters this same branch) must not double-count ONE real user
    # reply. Dataclass default None — mirrors `language_choice_attempts`'
    # own missing-hash-field tolerance for a pre-existing serialized
    # session hash.
    language_choice_last_message_id: str | None = None
    # F-039 (retry-safety, 2026-07-27): the `message_id` for which this
    # session already opened a Glific Ticket for an ASR-failure escalation
    # — makes `open_ticket_for_escalation`'s call on the CAP-REACHED audio
    # path idempotent per inbound message, the same
    # `payload.message_id`-tracking shape `language_choice_last_message_id`
    # already established for the LANGUAGE_CHOICE counter (2026-07-17). A
    # job redelivery for the SAME message_id (e.g. a transient `send_text`
    # failure before the ticket-open line, or a crash between a successful
    # ticket-open and the queue's ack) must not open a SECOND Ticket for the
    # same escalation. Dataclass default None — mirrors
    # `language_choice_last_message_id`'s own missing-hash-field tolerance
    # for a pre-existing serialized session hash.
    asr_escalation_ticket_message_id: str | None = None
    readiness_response: str | None = None
    resolved_locale: dict[str, Any] | None = None  # {"stt_locale":..., "tts_voice":...}
    escalation_reason: EscalationReason | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def is_expired(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.updated_at) > SESSION_TTL_SECONDS

    def touch(self, now: float | None = None) -> None:
        self.updated_at = now if now is not None else time.time()


def new_session(
    glific_contact_id: str,
    phone: str,
    needs_identity_confirmation: bool,
) -> SessionData:
    """Create a fresh session, entering `identity_confirmation` only when the
    phone resolves to more than one LMS candidate.
    """
    initial_state = (
        SessionState.IDENTITY_CONFIRMATION if needs_identity_confirmation else SessionState.GREETING
    )
    return SessionData(glific_contact_id=glific_contact_id, phone=phone, state=initial_state)


def can_transition(current: SessionState, target: SessionState) -> bool:
    return target in _ALLOWED_TRANSITIONS.get(current, set())


def transition(
    session: SessionData, target: SessionState, *, now: float | None = None
) -> SessionData:
    """Apply a validated state transition, bumping `turn_count` and `updated_at`.

    Raises InvalidTransition if the target isn't reachable from the current state.
    """
    if not can_transition(session.state, target):
        raise InvalidTransition(f"{session.state} -> {target} is not an allowed transition")
    session.state = target
    session.turn_count += 1
    session.touch(now)
    return session


def escalate(
    session: SessionData, reason: EscalationReason, *, now: float | None = None
) -> SessionData:
    """Force a transition to `escalated` regardless of current state (any
    non-terminal state can escalate on distress / ASR-failure / unresolved
    doubt), and record the reason on the session.
    """
    if session.state in (SessionState.CLOSE, SessionState.ESCALATED):
        return session
    session.state = SessionState.ESCALATED
    session.escalation_reason = reason
    session.touch(now)
    return session


def record_reexplanation(
    session: SessionData, *, now: float | None = None
) -> EscalationReason | None:
    """Bump the re-explanation counter. Returns an EscalationReason if the cap
    is exceeded (3rd unresolved doubt forces escalation, not a 3rd
    re-explanation), else None and the session stays in `doubt_clearing`.
    """
    session.reexplanation_count += 1
    session.touch(now)
    if session.reexplanation_count > MAX_REEXPLANATIONS:
        escalate(session, EscalationReason.UNRESOLVED_DOUBT, now=now)
        return EscalationReason.UNRESOLVED_DOUBT
    return None


def record_asr_failure(session: SessionData, *, now: float | None = None) -> bool:
    """Bump the ASR-failure counter. Returns True once the cap (2 consecutive
    failures) is reached — caller should fall back to text input, this
    function does NOT force escalation (ASR fallback is a UX degrade, not a
    safety escalation) unless the caller explicitly chooses to escalate.
    """
    session.asr_failure_count += 1
    session.touch(now)
    return session.asr_failure_count >= MAX_ASR_FAILURES


def reset_asr_failures(session: SessionData) -> None:
    session.asr_failure_count = 0


def record_language_choice_attempt(session: SessionData, *, now: float | None = None) -> bool:
    """Bumps the language-choice unmatched-reply counter. Returns True once
    the re-ask cap (MAX_LANGUAGE_CHOICE_ATTEMPTS) is EXCEEDED — caller
    should default `tap_language` to English and proceed to GREETING rather
    than re-ask again (GAP 0: never blocks the conversation).

    Increment+touch shape mirrors `record_asr_failure`; the `>` comparison
    (not `>=`) mirrors `record_reexplanation`'s style instead, because
    MAX_LANGUAGE_CHOICE_ATTEMPTS counts ALLOWED re-asks (1), not a
    consecutive-failure-before-fallback threshold: the 1st unmatched reply
    should still get its one scripted re-ask; only the 2nd (exceeding the
    cap of 1) defaults to English.

    Unconditional — this function does not itself know about message-level
    idempotency. The caller (`app/worker/turn_processor.py`'s LANGUAGE_CHOICE
    branch) is responsible for only calling this once per distinct
    `payload.message_id`, via `session.language_choice_last_message_id`
    (code review fix, 2026-07-17 High finding) — calling this twice for the
    same inbound message double-counts it.
    """
    session.language_choice_attempts += 1
    session.touch(now)
    return session.language_choice_attempts > MAX_LANGUAGE_CHOICE_ATTEMPTS
