"""Identity resolution: phone -> LMS candidates -> session bootstrap.

Ported unchanged in seam shape from
`tap-buddy/src/tap_buddy/core/identity.py` (CR-004 Task 9, AC-7/R-7).
Glific's webhook-provided phone number is the input,
`get_student_by_phone_and_name` resolves the student, Glific remains
authority for the contact/conversation itself — this module's output
(`SessionData`) is conversational session state ONLY, never a second,
competing source of contact/conversation identity.

No FastAPI/Glific/Redis imports — callers pass in an LMS-client-like object
(real or mock).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.config.language_map import resolve_locale
from app.core.state_machine import SessionData, StudentIdentity, new_session
from app.tools.lms_client import StudentCandidate, StudentLookupResult


class _StudentLookupClient(Protocol):
    """Structural type for the LMS-client-like object this module needs —
    satisfied by both `app.tools.lms_client.TapLmsClient` and
    `app.tools.lms_mock.MockTapLmsClient` without either importing the
    other.
    """

    async def get_student_by_phone_and_name(
        self, phone_number: str, buddy_name: str
    ) -> StudentLookupResult: ...


@dataclass
class IdentityResolution:
    session: SessionData
    needs_confirmation: bool
    candidates: list[dict[str, str]]


async def resolve_identity_by_name(
    glific_contact_id: str, phone: str, buddy_name: str, lms_client: _StudentLookupClient
) -> IdentityResolution:
    """The real identity path: phone + buddy_name -> LMS lookup.

    `get_student_by_phone_and_name` REQUIRES both phone_number and
    buddy_name (live-verified fact, ported from tap-buddy/CLAUDE.md — a
    phone-only lookup 400s with "Both phone_number and buddy_name are
    required"). So the ingress/worker flow is: on the first message from an
    unrecognized phone, the bot asks the child's name (a bot-authored
    prompt, no LMS call yet, session enters `identity_confirmation` with an
    empty candidate list) BEFORE calling this function. Once the child
    replies with a name, this function is called with that name.

    Single-match resolves straight to `greeting`. Multi-match — the LMS
    fuzzy-matching a name across siblings sharing one parent phone — stays
    in/re-enters `identity_confirmation`, asking the child to pick among the
    returned candidates (handled by confirm_identity below).
    """
    lookup = await lms_client.get_student_by_phone_and_name(phone, buddy_name)
    candidates = lookup.candidates if lookup.success else []
    needs_confirmation = len(candidates) > 1
    # Only a single unambiguous match may leave identity_confirmation: zero
    # matches must keep the session there so the child's NEXT message is
    # treated as another name attempt (ported bug fix: a failed lookup
    # producing a GREETING session with an empty identity caused the retry
    # to be consumed as a readiness answer).
    session = new_session(
        glific_contact_id, phone, needs_identity_confirmation=len(candidates) != 1
    )

    if len(candidates) == 1:
        session.identity = _identity_from_candidate(candidates[0])
    elif needs_confirmation:
        session.identity.candidates = [
            {"student_id": c.student_id, "child_name": c.child_name} for c in candidates
        ]

    return IdentityResolution(
        session=session,
        needs_confirmation=needs_confirmation,
        candidates=[{"student_id": c.student_id, "child_name": c.child_name} for c in candidates],
    )


def confirm_identity(session: SessionData, confirmed_name: str) -> bool:
    """Resolves identity_confirmation by matching the child's reply against
    the stashed candidate list. Returns True if a match was found (caller
    should then transition session.state to GREETING).
    """
    for candidate in session.identity.candidates:
        if candidate["child_name"].strip().lower() == confirmed_name.strip().lower():
            session.identity.student_id = candidate["student_id"]
            session.identity.child_name = candidate["child_name"]
            return True
    return False


def _identity_from_candidate(candidate: StudentCandidate) -> StudentIdentity:
    locale = resolve_locale(candidate.tap_language)
    return StudentIdentity(
        student_id=candidate.student_id,
        child_name=candidate.child_name,
        grade=candidate.grade,
        tap_language=locale.tap_language,
    )
