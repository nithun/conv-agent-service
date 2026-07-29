"""In-memory fixture LMS client (CR-004 Task 12), ported from
`tap-buddy/src/tap_buddy/clients/lms_mock.py`. Implements the same
method surface as `TapLmsClient` (duck-typed, not a subclass, to keep the
real client free of test-only branching) — including CR-011/ADR-013's
`register_test_student`/`reset_test_student` (tasks 6/7, added 2026-07-27)
and CR-013/ADR-014's `get_test_student_by_phone` (added 2026-07-29).
"""

from __future__ import annotations

from app.tools.lms_client import (
    ContentReference,
    PhoneLookupResult,
    QuizQuestionDetails,
    QuizStartResult,
    RegisterTestStudentResult,
    ResetTestStudentResult,
    StudentCandidate,
    StudentLookupResult,
    SubmissionResult,
)

DEFAULT_FIXTURE_STUDENTS: dict[str, list[StudentCandidate]] = {
    "917834811114": [
        StudentCandidate(
            student_id="STU-001",
            child_name="Aarav",
            grade="5",
            tap_language="Hindi",
        )
    ],
    "919999999999": [
        StudentCandidate(
            student_id="STU-002", child_name="Meera", grade="4", tap_language="English"
        ),
        StudentCandidate(student_id="STU-003", child_name="Kavya", grade="6", tap_language="Tamil"),
    ],
}

DEFAULT_FIXTURE_CONTENT: dict[str, ContentReference] = {
    "STU-001": ContentReference(
        content_id="CONTENT-101",
        title="Creative problem-solving",
        content_type="video",
        raw={"duration_minutes": 3},
    ),
    "STU-002": ContentReference(
        content_id="CONTENT-102",
        title="Fractions quiz",
        content_type="quiz",
        raw={"question_id": "Q-201"},
    ),
    "STU-003": ContentReference(
        content_id="CONTENT-103",
        title="Water cycle video",
        content_type="video",
        raw={"duration_minutes": 4},
    ),
}

DEFAULT_FIXTURE_QUIZ_QUESTIONS: dict[str, QuizQuestionDetails] = {
    "Q-201": QuizQuestionDetails(
        question_id="Q-201",
        question_text="What is 1/2 + 1/4?",
        hint="Try finding a common denominator first.",
        options=["1/4", "3/4", "1", "2/6"],
        raw={},
    )
}


class MockTapLmsClient:
    """Fixture-backed stand-in for TapLmsClient. All methods async to match
    the real client's interface.
    """

    def __init__(
        self,
        students: dict[str, list[StudentCandidate]] | None = None,
        content: dict[str, ContentReference] | None = None,
        quiz_questions: dict[str, QuizQuestionDetails] | None = None,
        test_batch_students: dict[str, list[StudentCandidate]] | None = None,
    ):
        self.students = students if students is not None else dict(DEFAULT_FIXTURE_STUDENTS)
        self.content = content if content is not None else dict(DEFAULT_FIXTURE_CONTENT)
        self.quiz_questions = (
            quiz_questions if quiz_questions is not None else dict(DEFAULT_FIXTURE_QUIZ_QUESTIONS)
        )
        # CR-013/ADR-014 Ask A: a DELIBERATELY SEPARATE fixture dict from
        # `self.students` above — the real `get_test_student_by_phone`
        # endpoint is a different, batch-scoped query, not a phone-only
        # projection of `get_student_by_phone_and_name`'s candidate pool.
        # Defaults to EMPTY (never DEFAULT_FIXTURE_STUDENTS) so every
        # existing by-name test's zero-match/ask-name behavior is preserved
        # unless a test opts in explicitly.
        self.test_batch_students = (
            test_batch_students if test_batch_students is not None else {}
        )
        self.submissions: list[dict[str, str]] = []
        self.quiz_starts: list[dict[str, str]] = []
        # CR-011/ADR-013 tasks 6/7 fixtures.
        self.register_test_student_calls: list[dict[str, str | None]] = []
        self.reset_test_student_calls: list[dict[str, object]] = []
        self._registered_test_students: dict[tuple[str, str], str] = {}
        self._next_test_student_seq = 1
        # CR-013/ADR-014 Ask A: records every phone probed, for the
        # regression test proving an EXISTING session never re-triggers
        # this lookup.
        self.test_student_by_phone_calls: list[str] = []

    async def aclose(self) -> None:
        return None

    async def get_student_by_phone_and_name(
        self, phone_number: str, buddy_name: str
    ) -> StudentLookupResult:
        candidates = self.students.get(phone_number, [])
        if not candidates:
            return StudentLookupResult(success=False, candidates=[], error="No student found")
        return StudentLookupResult(success=True, candidates=candidates)

    async def get_next_content(self, student_id: str) -> ContentReference | None:
        return self.content.get(student_id)

    async def start_quiz(
        self, student_id: str, course_level: str, quiz_id: str
    ) -> QuizStartResult | None:
        self.quiz_starts.append(
            {"student_id": student_id, "course_level": course_level, "quiz_id": quiz_id}
        )
        return QuizStartResult(
            quiz_attempt_id="ATTEMPT-001",
            question_id="Q-201",
            question_text="What helps a small idea grow?",
            options=["Saving money", "Ignoring it", "Spending fast", "Giving up"],
            total_questions=3,
            raw={"status": "quiz_started", "question_index": 1},
        )

    async def get_quiz_question_details(
        self, question_id: str, language: str
    ) -> QuizQuestionDetails | None:
        return self.quiz_questions.get(question_id)

    async def save_submission(
        self, student_id: str, content_id: str, submission: str
    ) -> SubmissionResult:
        """Mirrors the live CR-005 `invalid_assignment` contract
        (`docs/change-requests/CR-005-mock-lms-feedback-pipeline-available.md`
        "Contract change on the same branch"): `content_id` is now validated
        against the Assignment doctype server-side — unknown ids get
        `{"success": false, "status": "invalid_assignment", ...}` with NO
        Submission created. This fixture validates against the content ids
        it has actually vended via `get_next_content` (`self.content`'s
        values) rather than accepting any string, so mocked tests can
        exercise the rejection path the real client already maps
        (`app/tools/lms_client.py` `save_submission`). F-030.
        """
        valid_assignment_ids = {c.content_id for c in self.content.values()}
        if content_id not in valid_assignment_ids:
            return SubmissionResult(
                success=False,
                status="invalid_assignment",
                message=f"content_id {content_id!r} is not a recognized Assignment",
            )
        self.submissions.append(
            {"student_id": student_id, "content_id": content_id, "submission": submission}
        )
        return SubmissionResult(success=True, status="submitted")

    async def register_test_student(
        self,
        name1: str,
        phone: str,
        *,
        glific_id: str | None = None,
        grade: str | None = None,
        gender: str | None = None,
    ) -> RegisterTestStudentResult:
        """Fixture mirror of the real endpoint's idempotency-on-(phone,
        name1) contract — a repeat call for the SAME pair returns the same
        `student_id` with `created_student=False`, same as live.
        """
        self.register_test_student_calls.append(
            {
                "name1": name1,
                "phone": phone,
                "glific_id": glific_id,
                "grade": grade,
                "gender": gender,
            }
        )
        key = (phone, name1)
        existing_id = self._registered_test_students.get(key)
        created = existing_id is None
        if existing_id is None:
            existing_id = f"TEST-STU-{self._next_test_student_seq:03d}"
            self._next_test_student_seq += 1
            self._registered_test_students[key] = existing_id
        return RegisterTestStudentResult(
            success=True,
            student_id=existing_id,
            pe_name=f"PE-{existing_id}",
            created_student=created,
            created_pe=created,
            status_code=200,
        )

    async def reset_test_student(
        self, student_id: str, *, push_to_glific: bool = False
    ) -> ResetTestStudentResult:
        self.reset_test_student_calls.append(
            {"student_id": student_id, "push_to_glific": push_to_glific}
        )
        return ResetTestStudentResult(
            success=True, student_id=student_id, submissions_deleted=0, pe_reset=True
        )

    async def get_test_student_by_phone(self, phone: str) -> PhoneLookupResult:
        """CR-013/ADR-014 Ask A fixture. Reads `self.test_batch_students`
        (a SEPARATE fixture dict from `self.students` — see `__init__`),
        most-recent-first ordering assumed already correct in whatever list
        a test provides (this fixture does not re-sort).
        """
        self.test_student_by_phone_calls.append(phone)
        candidates = self.test_batch_students.get(phone, [])
        return PhoneLookupResult(
            success=True,
            matched_student_count=len(candidates),
            students=list(candidates),
        )
