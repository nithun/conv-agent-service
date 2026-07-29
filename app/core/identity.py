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

from app.core.state_machine import SessionData, StudentIdentity, new_session
from app.tools.lms_client import PhoneLookupResult, StudentCandidate, StudentLookupResult


class _StudentLookupClient(Protocol):
    """Structural type for the LMS-client-like object this module needs —
    satisfied by both `app.tools.lms_client.TapLmsClient` and
    `app.tools.lms_mock.MockTapLmsClient` without either importing the
    other.
    """

    async def get_student_by_phone_and_name(
        self, phone_number: str, buddy_name: str
    ) -> StudentLookupResult: ...

    # CR-013/ADR-014 — the J2 returning-by-phone lookup.
    async def get_test_student_by_phone(self, phone: str) -> PhoneLookupResult: ...


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
        # CR-007 (in-scope bug fix): carry tap_language forward so
        # confirm_identity below can populate session.identity.tap_language
        # on a successful multi-candidate confirm — StudentCandidate always
        # has the field (possibly None), previously dropped here.
        session.identity.candidates = [
            {
                "student_id": c.student_id,
                "child_name": c.child_name,
                "tap_language": c.tap_language,
            }
            for c in candidates
        ]

    return IdentityResolution(
        session=session,
        needs_confirmation=needs_confirmation,
        candidates=[{"student_id": c.student_id, "child_name": c.child_name} for c in candidates],
    )


async def resolve_returning_identity_by_phone(
    glific_contact_id: str, phone: str, lms_client: _StudentLookupClient
) -> IdentityResolution:
    """CR-013 Ask A / ADR-014 — the J2 returning-by-phone resolution: tried
    at session start, BEFORE ever asking the child's name
    (`app/worker/turn_processor.py`'s `session is None` branch). Scoped
    server-side to the dedicated test batch `BT00000025` (ADR-014 BC-B) —
    this function does no batch filtering itself, it trusts the LMS's own
    scoping (`TapLmsClient.get_test_student_by_phone`'s docstring).

    Deliberately mirrors `resolve_identity_by_name` above almost verbatim
    (same `IdentityResolution` shape, same `_identity_from_candidate` reuse,
    same candidate-stash shape) so the caller's EXISTING disambiguation seam
    (`confirm_identity` / the `IDENTITY_CONFIRMATION` + non-empty
    `candidates` branch in `turn_processor.py`) handles the multi-match case
    with zero new code (P-005) — this function only supplies a different
    LMS query as the candidate source.

    - Zero matches: returns an `IdentityResolution` with empty candidates —
      the caller (`turn_processor.py`) falls back to today's unchanged
      ask-name path (identical handling to a lookup FAILURE, L-005 failure
      honesty).
    - Exactly one match: resolves directly via `_identity_from_candidate`
      (`child_name`/`tap_language` sourced from the LMS record, never from
      user input) — no name-ask turn.
    - More than one match: stashes `session.identity.candidates` from the
      full (most-recent-first) match list — the same shape
      `resolve_identity_by_name`'s own multi-candidate branch builds.
    """
    lookup = await lms_client.get_test_student_by_phone(phone)
    candidates = lookup.students if lookup.success else []
    needs_confirmation = len(candidates) > 1
    session = new_session(
        glific_contact_id, phone, needs_identity_confirmation=len(candidates) != 1
    )

    if len(candidates) == 1:
        session.identity = _identity_from_candidate(candidates[0])
    elif needs_confirmation:
        session.identity.candidates = [
            {
                "student_id": c.student_id,
                "child_name": c.child_name,
                "tap_language": c.tap_language,
            }
            for c in candidates
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
            # CR-007 (in-scope bug fix): previously only the single-match
            # path ever populated tap_language, leaving the candidate-
            # confirm path's greeting reply running with tap_language=None
            # even when the matched candidate carried a language. Stores
            # the RAW candidate value (see _identity_from_candidate's
            # docstring for why this is raw, not resolve_locale'd, despite
            # the CR text describing this as mirroring that function's
            # "existing" — pre-this-CR, eager-resolving — shape).
            session.identity.tap_language = candidate.get("tap_language")
            return True
    return False


def _identity_from_candidate(candidate: StudentCandidate) -> StudentIdentity:
    """CR-007 deviation (feature-builder, live-verified against this repo's
    own code, not the CR's literal text): stores the RAW `candidate.tap_language`
    (possibly None) rather than eagerly resolving it through `resolve_locale`
    as this function did pre-CR-007. Two reasons this is necessary, not
    optional:

    1. GAP 0's whole premise is "when session.identity.tap_language is
       falsy, offer the language-choice turn" (turn_processor.py). The old
       eager-resolve_locale call coerced a missing LMS language to the
       STRING "English" right here — session.identity.tap_language was
       therefore NEVER actually falsy after a successful resolution, making
       the language-choice turn structurally unreachable. Storing the raw
       value (None when the LMS supplies nothing, matching live reality
       per this CR's own root-cause finding) is what makes GAP 0's trigger
       condition real.
    2. Punjabi is explicitly scoped as "fine as scripted TEXT" despite
       `language_map.yaml` marking it `supported: false` for STT/TTS (this
       CR's Proposed behavior section). Eager resolve_locale would have
       coerced a Punjabi candidate's tap_language to "English" immediately
       (resolve_locale's own supported-gate), permanently hiding Punjabi
       scripted replies. Every downstream consumer of
       `session.identity.tap_language` (turn_logic.py's `resolve_locale`
       calls, `app/config/scripted_replies.py`'s loader, `detect_distress`)
       already independently tolerates/falls back on a falsy or unsupported
       value — this function no longer needs to pre-resolve it.
    """
    return StudentIdentity(
        student_id=candidate.student_id,
        child_name=candidate.child_name,
        grade=candidate.grade,
        tap_language=candidate.tap_language,
    )
