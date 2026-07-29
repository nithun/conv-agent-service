"""TAP LMS (Frappe) client — the COMPLETE LMS integration surface.

Ported from `tap-buddy/src/tap_buddy/clients/lms_client.py` (CR-004 Task
12). 8 whitelisted endpoints (`start_quiz` added 2026-07-17 under F-028,
permanently registered via CR-031/D-8; `register_test_student`/
`reset_test_student` added 2026-07-27 under CR-011/ADR-013, tasks 6/7;
`get_test_student_by_phone` added 2026-07-29 under CR-013/ADR-014, the J2
returning-by-phone wire-up). No other method exists on this client, and none
should ever be added without a corresponding PRD/CR — `save_submission`,
`register_test_student`, and `reset_test_student` are the three
unconditional write paths (state authority, L-001 — all three are
existing/newly-whitelisted Frappe endpoints, never an agent-invented state);
`start_quiz` creates/resumes a quiz attempt (deterministic, once-per-quiz,
resume-idempotent — see its docstring); `get_test_student_by_phone` is a
pure read, scoped SERVER-SIDE to the dedicated test batch `BT00000025`
(ADR-014 BC-B — this client does no batch filtering itself, it trusts the
LMS's own scoping).

Live-verified dotted paths (tap-buddy/CLAUDE.md, 2026-07-03, against
lms.evalix.xyz with a real API key — re-verification against THIS stack is
Phase 7's job per L-009, not assumed carried over just because the paths
are unchanged):
  - get_student_by_phone_and_name -> tap_lms.api.get_student_by_phone_and_name
  - get_quiz_question_details     -> tap_lms.api.get_quiz_question_details
  - save_submission                -> tap_lms.summer_program.save_submission.save_submission
  - get_next_content               -> tap_lms.summer_program.student_progression_sp.get_next_content
    (requires Frappe token auth via `Authorization: token <value>` —
    allow_guest=False on the live server, NOT a whitelist gap. See
    Settings.tap_lms_token.)

CR-011/ADR-013 (2026-07-27 CONTRACT UPDATE, F-071 reply, live-verified on
lms.evalix.xyz 2026-07-25 — AD-002's VERIFIED external-contracts rows are
the authoritative source, not the CR body's original `register_student`
assumption):
  - register_test_student -> tap_lms.summer_program.cr011_test_flow.register_test_student
    Creates Student+ProgramEnrollment in a server-hardcoded TEST batch
    (course/batch are NEVER passed by this client) and returns the new
    `student_id` directly — a SINGLE call, not a create-then-re-resolve
    round trip. Idempotent on (phone, name1). Response envelope is the
    standard Frappe `{"message": {...}}` shape — NOT get_next_content's
    flat `@glific_response` shape.
  - reset_test_student     -> tap_lms.summer_program.cr011_test_flow.reset_test_student
    Full-zero reset (also deletes Submission rows), unconditional — no
    dummy-only/eligibility guard (Option B, ADR-013). Same `{"message":
    {...}}` envelope.
  Both use the SAME `Authorization: token <key>:<secret>` header the other
  5 methods already send via `frappe_token` — no new credential.

CR-013/ADR-014 (2026-07-29, HANDOVER-tap-agent-2026-07-29-cr013-returning-and-language-REPLY.md,
Ask A — deployed + live-verified on lms.evalix.xyz same day):
  - get_test_student_by_phone -> tap_lms.summer_program.cr011_test_flow.get_test_student_by_phone
    Read-only, SAME `Authorization: token` credential, scoped SERVER-SIDE to
    test batch `BT00000025` (real non-test enrollments are ignored —
    ADR-014 BC-B). Payload `{"phone": ...}` (10-digit or 91-prefixed both
    accepted). Response envelope is the standard Frappe `{"message": {...}}`
    shape: `{status_code, success, phone, matched_student_count, students}`
    — `students` is a list, MOST-RECENT-FIRST by `last_activity`, each
    `{student_id, name1, phone, language, status, grade, pe_name, batch,
    last_activity}`. `language` is `null` after a `reset_test_student`
    reset (CR-013 decision 4) — the caller's existing GAP-0 check handles a
    falsy `tap_language` unchanged. Deployed on tap-lms-frappe's
    server-local branch `returning-user-language-field` @ `e22ba91` (not
    yet pushed to GitHub, held checked out for the test window).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

GET_STUDENT_METHOD = "tap_lms.api.get_student_by_phone_and_name"
GET_NEXT_CONTENT_METHOD = "tap_lms.summer_program.student_progression_sp.get_next_content"
GET_QUIZ_QUESTION_METHOD = "tap_lms.api.get_quiz_question_details"
SAVE_SUBMISSION_METHOD = "tap_lms.summer_program.save_submission.save_submission"
START_QUIZ_METHOD = "tap_lms.summer_program.student_progression_sp.start_quiz"
# CR-011/ADR-013 tasks 6/7 (2026-07-27 CONTRACT UPDATE, F-071 reply).
REGISTER_TEST_STUDENT_METHOD = "tap_lms.summer_program.cr011_test_flow.register_test_student"
RESET_TEST_STUDENT_METHOD = "tap_lms.summer_program.cr011_test_flow.reset_test_student"
# CR-013/ADR-014 Ask A (2026-07-29 CONTRACT, deployed + live-verified).
GET_TEST_STUDENT_BY_PHONE_METHOD = (
    "tap_lms.summer_program.cr011_test_flow.get_test_student_by_phone"
)


class LmsApiError(Exception):
    """Raised when the LMS returns a non-2xx or a business-logic failure envelope."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class StudentCandidate:
    student_id: str
    child_name: str
    grade: str | None = None
    tap_language: str | None = None


@dataclass
class StudentLookupResult:
    """Result of get_student_by_phone_and_name. `candidates` holds every
    match (usually one); the caller decides whether disambiguation
    (identity_confirmation) is needed based on len(candidates) > 1.
    """

    success: bool
    candidates: list[StudentCandidate]
    error: str | None = None


@dataclass
class ContentReference:
    content_id: str
    title: str
    content_type: str  # "video" | "quiz" | "assignment"
    raw: dict[str, Any]


@dataclass
class QuizQuestionDetails:
    """Deliberately has NO `correct_option` attribute — F-011's type-level
    guard. TAP LMS's live endpoint DOES return `correct_option`/
    `correct_option_text` (F-012 correction), but this type has nowhere to
    put it even if the raw response includes it, which is the enforcement
    mechanism: any code constructing this dataclass from the live response
    must explicitly choose not to carry that field forward.
    """

    question_id: str
    question_text: str
    hint: str | None
    options: list[str]
    raw: dict[str, Any]


@dataclass
class SubmissionResult:
    success: bool
    status: str
    message: str | None = None


@dataclass
class RegisterTestStudentResult:
    """CR-011/ADR-013 Task 7 auto-provision write result. `student_id` is
    the newly-created (or, on a repeat call for the same phone+name1,
    pre-existing — `created_student=False`) `Student` record, returned
    directly by the endpoint — no separate re-resolve call needed (2026-07-27
    CONTRACT UPDATE supersedes CR-011's originally-assumed two-step shape).
    """

    success: bool
    student_id: str
    pe_name: str | None = None
    created_student: bool = False
    created_pe: bool = False
    status_code: int | None = None


@dataclass
class ResetTestStudentResult:
    """CR-011/ADR-013 Task 6 TESTAIRESET write result. Full-zero reset
    (also deletes `Submission` rows) — no dummy-only/eligibility guard
    (Option B).
    """

    success: bool
    student_id: str
    submissions_deleted: int = 0
    pe_reset: bool = False


@dataclass
class PhoneLookupResult:
    """CR-013 Ask A (ADR-014 BC-B) — phone-only returning-tester lookup,
    scoped SERVER-SIDE to the dedicated test batch `BT00000025` (never a
    general-population query). `students` is MOST-RECENT-FIRST by
    `last_activity` (the LMS's own ordering) — `students[0]` is "last
    interacted". The caller (`app/core/identity.py`) decides the
    zero/one/many branching; this client never does.
    """

    success: bool
    matched_student_count: int
    students: list[StudentCandidate]
    error: str | None = None


_ANSWER_LEAK_KEY_PREFIX = "correct_option"


def _scrub_answer_leak(raw: dict[str, Any]) -> dict[str, Any]:
    """Drops every `correct_option*` key from a quiz-related response dict.

    The live server LEAKS the answer on FOUR quiz surfaces —
    `get_quiz_question_details`, `start_quiz`, quiz-resume, and
    `submit_answer`'s next-question payload (live-confirmed by the
    tap-lms-frappe station, [[ANSWERS-TAPLMS-2026-07-17]] Q3/Q4). Decision
    of record D-7 ([[HANDOVER-TAP-AGENT-2026-07-17]], nithun, 2026-07-17):
    the answer fields are BY DESIGN (the Glific flow grades inline) and no
    server-side change is coming — this client-side scrub is therefore the
    PERMANENT, sanctioned pattern (F-011/F-012 discipline), not a stopgap:
    the answer must never even be STORED on our side, let alone reach a
    prompt.
    """
    return {k: v for k, v in raw.items() if not k.startswith(_ANSWER_LEAK_KEY_PREFIX)}


@dataclass
class QuizStartResult:
    """Deliberately has NO `correct_option` attribute (same type-level
    guard as `QuizQuestionDetails`), and `raw` is SCRUBBED via
    `_scrub_answer_leak` before construction — the live `start_quiz`
    response includes the answer key.
    """

    quiz_attempt_id: str
    question_id: str
    question_text: str
    options: list[str]
    total_questions: int | None
    raw: dict[str, Any]


class TapLmsClient:
    """The complete TAP LMS integration surface — exactly 8 methods. Do not
    add a 9th without a PRD/CR. (The 5th, `start_quiz`, was added
    2026-07-17 under F-028 — nithun-directed design; registration made
    PERMANENT by CR-031's approval, decision D-8,
    [[HANDOVER-TAP-AGENT-2026-07-17]]. The 6th/7th, `register_test_student`/
    `reset_test_student`, were added 2026-07-27 under CR-011/ADR-013 tasks
    6/7 — both existing, already-live-verified Frappe endpoints, registered
    in AD-002's external-contracts table as VERIFIED rows, F-071 reply. The
    8th, `get_test_student_by_phone`, was added 2026-07-29 under CR-013/
    ADR-014 — the J2 returning-by-phone wire-up.)
    """

    def __init__(
        self, base_url: str, api_key: str, timeout: float = 45.0, frappe_token: str | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # get_next_content is allow_guest=False on the live server and
        # requires Frappe token auth — optional TAP_LMS_TOKEN, sent as
        # `Authorization: token <value>` when set. Harmless to send on all 4
        # calls (the other 3 endpoints don't require it but don't reject the
        # extra header either), so it's applied client-wide.
        self.frappe_token = frappe_token
        headers = {"Authorization": f"token {frappe_token}"} if frappe_token else {}
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {"api_key": self.api_key, **payload}
        try:
            resp = await self._client.post(f"/api/method/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise LmsApiError(f"LMS request failed: {exc}") from None
        if resp.status_code >= 400:
            raise LmsApiError(
                f"LMS returned {resp.status_code} for {method}: {resp.text[:300]}",
                status_code=resp.status_code,
            )
        try:
            body: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise LmsApiError(f"LMS returned non-JSON body for {method}") from exc
        return body

    async def get_student_by_phone_and_name(
        self, phone_number: str, buddy_name: str
    ) -> StudentLookupResult:
        body = await self._call(
            GET_STUDENT_METHOD, {"phone_number": phone_number, "buddy_name": buddy_name}
        )
        message = body.get("message", {})
        if isinstance(message, dict) and message.get("success") is False:
            return StudentLookupResult(success=False, candidates=[], error=message.get("error"))
        # Live success shape: the student payload nests under
        # message["data"] as a single dict with student_id/buddy_name/
        # current_grade keys. Fall back to the flat message for fixture/mock
        # shapes, and accept a list under either for shared-phone
        # multi-child responses.
        payload = message.get("data", message) if isinstance(message, dict) else message
        raw_candidates = payload if isinstance(payload, list) else [payload]
        candidates = [
            StudentCandidate(
                student_id=str(c.get("student_id") or c.get("name") or ""),
                child_name=c.get("child_name") or c.get("buddy_name") or "",
                grade=c.get("grade") or c.get("current_grade"),
                tap_language=c.get("tap_language") or c.get("language"),
            )
            for c in raw_candidates
            if isinstance(c, dict)
        ]
        return StudentLookupResult(success=True, candidates=candidates)

    async def get_next_content(self, student_id: str) -> ContentReference | None:
        body = await self._call(GET_NEXT_CONTENT_METHOD, {"student_id": student_id})
        # This endpoint is wrapped in tap_lms's @glific_response decorator,
        # which returns flat JSON with no Frappe "message" envelope. Fall
        # back to the flat body when the envelope is absent.
        message_raw = body.get("message")
        message: dict[str, Any] = message_raw if isinstance(message_raw, dict) else body
        if message.get("success") is False:
            return None
        return ContentReference(
            content_id=str(message.get("content_id") or message.get("name") or ""),
            title=message.get("title", ""),
            content_type=message.get("content_type", ""),
            raw=message,
        )

    async def get_quiz_question_details(
        self, question_id: str, language: str
    ) -> QuizQuestionDetails | None:
        body = await self._call(
            GET_QUIZ_QUESTION_METHOD, {"question_id": question_id, "language": language}
        )
        message = body.get("message", {})
        inner = message.get("message", message) if isinstance(message, dict) else message
        if isinstance(inner, dict) and inner.get("success") is False:
            return None
        return QuizQuestionDetails(
            question_id=question_id,
            question_text=inner.get("question_text", ""),
            hint=inner.get("hint"),
            options=inner.get("options", []),
            # Scrubbed (2026-07-17): the live response carries
            # correct_option/correct_option_text (F-012) — previously the
            # type guard kept them off the attribute surface but the leak
            # still sat in `raw`; now it never gets stored at all.
            raw=_scrub_answer_leak(inner),
        )

    async def start_quiz(
        self, student_id: str, course_level: str, quiz_id: str
    ) -> QuizStartResult | None:
        """Starts (or resumes) the student's quiz attempt and returns the
        CURRENT question — the authoritative `question_id` source for the
        doubt-clearing hint path (F-028). NOT a pure read: it creates/
        resumes a StudentQuizAttempt server-side, after which
        `get_next_content` returns `quiz_in_progress` for this student —
        call it once per quiz content and stash the result on the session.
        `quiz_id` is the quiz-type `get_next_content` payload's
        `content_id` (there is no separate quiz_id field — live-verified,
        [[ANSWERS-TAPLMS-2026-07-17]] Q3). The server ignores a `language`
        param (resolves from ProgramEnrollment/Student), so none is sent.
        """
        body = await self._call(
            START_QUIZ_METHOD,
            {"student_id": student_id, "course_level": course_level, "quiz_id": quiz_id},
        )
        # Same envelope tolerance as get_next_content (summer_program
        # endpoints return flat JSON via @glific_response; fixtures/mocks
        # may nest under "message").
        message_raw = body.get("message")
        message: dict[str, Any] = message_raw if isinstance(message_raw, dict) else body
        if message.get("success") is False:
            return None
        options = [
            str(message[key])
            for key in ("option_a", "option_b", "option_c", "option_d")
            if message.get(key)
        ]
        total_raw = message.get("total_questions")
        return QuizStartResult(
            quiz_attempt_id=str(message.get("quiz_attempt_id") or ""),
            question_id=str(message.get("question_id") or ""),
            question_text=message.get("question_text", ""),
            options=options,
            total_questions=int(total_raw) if total_raw is not None else None,
            raw=_scrub_answer_leak(message),
        )

    async def save_submission(
        self, student_id: str, content_id: str, submission: str
    ) -> SubmissionResult:
        body = await self._call(
            SAVE_SUBMISSION_METHOD,
            {"student_id": student_id, "content_id": content_id, "submission": submission},
        )
        message = body.get("message", body)
        if isinstance(message, dict):
            return SubmissionResult(
                success=bool(message.get("success", False)),
                status=message.get("status", "unknown"),
                message=message.get("error_detail") or message.get("user_message"),
            )
        return SubmissionResult(
            success=False, status="unknown", message="unrecognized response shape"
        )

    async def register_test_student(
        self,
        name1: str,
        phone: str,
        *,
        glific_id: str | None = None,
        grade: str | None = None,
        gender: str | None = None,
    ) -> RegisterTestStudentResult:
        """CR-011/ADR-013 Task 7 — the auto-provision write for a genuinely
        not-found identity. A SINGLE call: creates `Student`+
        `ProgramEnrollment` in a server-hardcoded TEST batch (course/batch
        are NEVER part of this payload — the server owns that mapping) and
        returns the new `student_id` directly (2026-07-27 CONTRACT UPDATE;
        supersedes CR-011's originally-assumed two-step register-then-
        re-resolve shape via the frozen, PE-less `register_student`).
        Idempotent on `(phone, name1)` — a repeat call for the same pair
        returns the SAME `student_id` with `created_student=False`, so the
        "found contact continues" path may also call this safely.
        Response envelope is the standard Frappe `{"message": {...}}` shape
        (read `response["message"]`) — NOT `get_next_content`'s flat
        `@glific_response` shape.
        """
        payload: dict[str, Any] = {"name1": name1, "phone": phone}
        if glific_id is not None:
            payload["glific_id"] = glific_id
        if grade is not None:
            payload["grade"] = grade
        if gender is not None:
            payload["gender"] = gender
        body = await self._call(REGISTER_TEST_STUDENT_METHOD, payload)
        message = body.get("message")
        if not isinstance(message, dict):
            raise LmsApiError(
                f"register_test_student: unexpected response shape (no 'message' dict): "
                f"{body!r}"
            )
        return RegisterTestStudentResult(
            success=bool(message.get("success", False)),
            student_id=str(message.get("student_id") or ""),
            pe_name=message.get("pe_name"),
            created_student=bool(message.get("created_student", False)),
            created_pe=bool(message.get("created_pe", False)),
            status_code=message.get("status_code"),
        )

    async def reset_test_student(
        self, student_id: str, *, push_to_glific: bool = False
    ) -> ResetTestStudentResult:
        """CR-011/ADR-013 Task 6 — the TESTAIRESET write. Full-zero reset
        (also deletes the student's `Submission` rows) — unconditional, NO
        dummy-only/eligibility guard (Option B, Nithun's explicit choice).
        The caller (`app/worker/turn_processor.py`) is responsible for
        passing the SESSION-resolved `student_id` — never a model-supplied
        or re-derived one (L-014); this client does not enforce that
        itself, it only makes the call. `push_to_glific` defaults False —
        evalix feeds nothing back, so there is no callback to push.
        """
        body = await self._call(
            RESET_TEST_STUDENT_METHOD,
            {"student_id": student_id, "push_to_glific": push_to_glific},
        )
        message = body.get("message")
        if not isinstance(message, dict):
            raise LmsApiError(
                f"reset_test_student: unexpected response shape (no 'message' dict): {body!r}"
            )
        return ResetTestStudentResult(
            success=bool(message.get("success", False)),
            student_id=str(message.get("student_id") or student_id),
            submissions_deleted=int(message.get("submissions_deleted") or 0),
            pe_reset=bool(message.get("pe_reset", False)),
        )

    async def get_test_student_by_phone(self, phone: str) -> PhoneLookupResult:
        """CR-013 Ask A / ADR-014 BC-B — the J2 returning-by-phone lookup.

        Read-only, scoped SERVER-SIDE to test batch `BT00000025` (this
        client trusts the LMS's own scoping; it does no filtering here).
        Called at session start, BEFORE ever asking the child's name
        (`app/worker/turn_processor.py`'s `session is None` branch, via
        `app/core/identity.py::resolve_returning_identity_by_phone`).

        Response envelope: standard Frappe `{"message": {...}}` — read
        `response["message"]`, NOT `get_next_content`'s flat
        `@glific_response` shape. `students` is most-recent-first by
        `last_activity`; each entry's `name1`/`language` map onto
        `StudentCandidate.child_name`/`tap_language` (the SAME shape
        `get_student_by_phone_and_name` already produces, so
        `identity.py`'s `_identity_from_candidate` and the multi-candidate
        disambiguation seam work unchanged). `language` comes back `null`
        for a student whose `TESTAIRESET` reset already cleared it
        (CR-013 decision 4) — the caller's existing GAP-0 falsy check
        handles that unchanged.

        Raises `LmsApiError` on a non-2xx/non-JSON response or an
        unrecognized envelope — the caller (`turn_processor.py`) MUST catch
        this and fall back to today's ask-name path (L-005 failure honesty:
        never silently treat a lookup failure as "definitely a new
        tester").
        """
        body = await self._call(GET_TEST_STUDENT_BY_PHONE_METHOD, {"phone": phone})
        message = body.get("message")
        if not isinstance(message, dict):
            raise LmsApiError(
                f"get_test_student_by_phone: unexpected response shape "
                f"(no 'message' dict): {body!r}"
            )
        if message.get("success") is False:
            return PhoneLookupResult(
                success=False,
                matched_student_count=0,
                students=[],
                error=message.get("error"),
            )
        raw_students = message.get("students")
        students_raw = raw_students if isinstance(raw_students, list) else []
        students = [
            StudentCandidate(
                student_id=str(s.get("student_id") or ""),
                child_name=s.get("name1") or "",
                grade=s.get("grade"),
                tap_language=s.get("language"),
            )
            for s in students_raw
            if isinstance(s, dict)
        ]
        matched_count_raw = message.get("matched_student_count")
        matched_count = int(matched_count_raw) if matched_count_raw is not None else len(students)
        return PhoneLookupResult(
            success=True,
            matched_student_count=matched_count,
            students=students,
        )
