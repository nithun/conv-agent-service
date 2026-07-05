"""In-memory fixture LMS client (CR-004 Task 12), ported from
`tap-buddy/src/tap_buddy/clients/lms_mock.py`. Implements the same
4-method surface as `TapLmsClient` (duck-typed, not a subclass, to keep the
real client free of test-only branching).
"""

from __future__ import annotations

from app.tools.lms_client import (
    ContentReference,
    QuizQuestionDetails,
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
    """Fixture-backed stand-in for TapLmsClient. All 4 methods async to match
    the real client's interface.
    """

    def __init__(
        self,
        students: dict[str, list[StudentCandidate]] | None = None,
        content: dict[str, ContentReference] | None = None,
        quiz_questions: dict[str, QuizQuestionDetails] | None = None,
    ):
        self.students = students if students is not None else dict(DEFAULT_FIXTURE_STUDENTS)
        self.content = content if content is not None else dict(DEFAULT_FIXTURE_CONTENT)
        self.quiz_questions = (
            quiz_questions if quiz_questions is not None else dict(DEFAULT_FIXTURE_QUIZ_QUESTIONS)
        )
        self.submissions: list[dict[str, str]] = []

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

    async def get_quiz_question_details(
        self, question_id: str, language: str
    ) -> QuizQuestionDetails | None:
        return self.quiz_questions.get(question_id)

    async def save_submission(
        self, student_id: str, content_id: str, submission: str
    ) -> SubmissionResult:
        self.submissions.append(
            {"student_id": student_id, "content_id": content_id, "submission": submission}
        )
        return SubmissionResult(success=True, status="submitted")
