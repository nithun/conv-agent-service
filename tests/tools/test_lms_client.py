"""LMS client acceptance tests: all 4 methods, mocked HTTP (respx), at
least one error-path per method, and the F-011/F-5.3
no-correct_option-attribute guard.

Re-targeted from tap-buddy/tests/test_lms_client.py against
app.tools.lms_client (CR-004 Task 12).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.tools.lms_client import (
    GET_NEXT_CONTENT_METHOD,
    GET_QUIZ_QUESTION_METHOD,
    GET_STUDENT_METHOD,
    SAVE_SUBMISSION_METHOD,
    LmsApiError,
    QuizQuestionDetails,
    TapLmsClient,
)

BASE_URL = "https://lms.test.example"


@pytest.fixture
def client():
    c = TapLmsClient(BASE_URL, "test-api-key")
    yield c


@pytest.mark.asyncio
@respx.mock
async def test_get_student_by_phone_and_name_success(client):
    respx.post(f"{BASE_URL}/api/method/{GET_STUDENT_METHOD}").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "student_id": "STU-001",
                    "child_name": "Aarav",
                    "grade": "5",
                    "tap_language": "Hindi",
                }
            },
        )
    )
    result = await client.get_student_by_phone_and_name("917834811114", "Aarav")
    assert result.success is True
    assert len(result.candidates) == 1
    assert result.candidates[0].student_id == "STU-001"
    assert result.candidates[0].tap_language == "Hindi"


@pytest.mark.asyncio
@respx.mock
async def test_get_student_by_phone_and_name_not_found(client):
    respx.post(f"{BASE_URL}/api/method/{GET_STUDENT_METHOD}").mock(
        return_value=httpx.Response(
            200, json={"message": {"success": False, "error": "No student found"}}
        )
    )
    result = await client.get_student_by_phone_and_name("000", "Nobody")
    assert result.success is False
    assert result.candidates == []
    assert result.error == "No student found"


@pytest.mark.asyncio
@respx.mock
async def test_get_student_by_phone_and_name_error_path_5xx(client):
    respx.post(f"{BASE_URL}/api/method/{GET_STUDENT_METHOD}").mock(
        return_value=httpx.Response(500, text="internal error")
    )
    with pytest.raises(LmsApiError):
        await client.get_student_by_phone_and_name("917834811114", "Aarav")


@pytest.mark.asyncio
@respx.mock
async def test_get_next_content_success(client):
    respx.post(f"{BASE_URL}/api/method/{GET_NEXT_CONTENT_METHOD}").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "content_id": "CONTENT-101",
                    "title": "Creative problem-solving",
                    "content_type": "video",
                }
            },
        )
    )
    content = await client.get_next_content("STU-001")
    assert content is not None
    assert content.content_id == "CONTENT-101"
    assert content.content_type == "video"


@pytest.mark.asyncio
@respx.mock
async def test_get_next_content_error_path_4xx(client):
    respx.post(f"{BASE_URL}/api/method/{GET_NEXT_CONTENT_METHOD}").mock(
        return_value=httpx.Response(403, text="not whitelisted")
    )
    with pytest.raises(LmsApiError):
        await client.get_next_content("STU-001")


@pytest.mark.asyncio
@respx.mock
async def test_get_quiz_question_details_success_has_no_correct_option_attribute(client):
    respx.post(f"{BASE_URL}/api/method/{GET_QUIZ_QUESTION_METHOD}").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "question_text": "What is 1/2 + 1/4?",
                    "hint": "Try a common denominator.",
                    "options": ["1/4", "3/4", "1"],
                }
            },
        )
    )
    details = await client.get_quiz_question_details("Q-201", "English")
    assert details is not None
    assert details.hint == "Try a common denominator."
    # F-011/F-5.3 enforcement: the type has no correct_option attribute at
    # all, even though the LIVE endpoint returns one (F-012) — the type
    # never carries it forward.
    assert not hasattr(details, "correct_option")
    assert "correct_option" not in QuizQuestionDetails.__dataclass_fields__


@pytest.mark.asyncio
@respx.mock
async def test_get_quiz_question_details_not_found(client):
    respx.post(f"{BASE_URL}/api/method/{GET_QUIZ_QUESTION_METHOD}").mock(
        return_value=httpx.Response(
            200, json={"message": {"message": {"success": False, "error": "Question not found"}}}
        )
    )
    details = await client.get_quiz_question_details("bogus", "English")
    assert details is None


@pytest.mark.asyncio
@respx.mock
async def test_get_quiz_question_details_error_path(client):
    respx.post(f"{BASE_URL}/api/method/{GET_QUIZ_QUESTION_METHOD}").mock(
        return_value=httpx.Response(500, text="server error")
    )
    with pytest.raises(LmsApiError):
        await client.get_quiz_question_details("Q-201", "English")


@pytest.mark.asyncio
@respx.mock
async def test_save_submission_success(client):
    respx.post(f"{BASE_URL}/api/method/{SAVE_SUBMISSION_METHOD}").mock(
        return_value=httpx.Response(200, json={"success": True, "status": "submitted"})
    )
    result = await client.save_submission("STU-001", "CONTENT-101", "done")
    assert result.success is True
    assert result.status == "submitted"


@pytest.mark.asyncio
@respx.mock
async def test_save_submission_error_path(client):
    respx.post(f"{BASE_URL}/api/method/{SAVE_SUBMISSION_METHOD}").mock(
        return_value=httpx.Response(400, text="bad request")
    )
    with pytest.raises(LmsApiError):
        await client.save_submission("STU-001", "CONTENT-101", "done")


# ---- TAP_LMS_TOKEN / Authorization header (get_next_content token auth) ---


@pytest.mark.asyncio
@respx.mock
async def test_frappe_token_sends_authorization_header_on_get_next_content():
    token_client = TapLmsClient(BASE_URL, "test-api-key", frappe_token="real-frappe-token")
    route = respx.post(f"{BASE_URL}/api/method/{GET_NEXT_CONTENT_METHOD}").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "content_id": "CONTENT-101",
                    "title": "Creative problem-solving",
                    "content_type": "video",
                }
            },
        )
    )
    await token_client.get_next_content("STU-001")
    assert route.calls.last.request.headers["authorization"] == "token real-frappe-token"


@pytest.mark.asyncio
@respx.mock
async def test_frappe_token_sent_on_all_four_methods_when_set():
    """The task's contract: harmless (and applied) to send the token header
    on all 4 calls, not just get_next_content.
    """
    token_client = TapLmsClient(BASE_URL, "test-api-key", frappe_token="real-frappe-token")

    student_route = respx.post(f"{BASE_URL}/api/method/{GET_STUDENT_METHOD}").mock(
        return_value=httpx.Response(200, json={"message": {"success": False, "error": "nf"}})
    )
    quiz_route = respx.post(f"{BASE_URL}/api/method/{GET_QUIZ_QUESTION_METHOD}").mock(
        return_value=httpx.Response(
            200, json={"message": {"message": {"success": False, "error": "nf"}}}
        )
    )
    submit_route = respx.post(f"{BASE_URL}/api/method/{SAVE_SUBMISSION_METHOD}").mock(
        return_value=httpx.Response(200, json={"success": True, "status": "submitted"})
    )

    await token_client.get_student_by_phone_and_name("917834811114", "Aarav")
    await token_client.get_quiz_question_details("Q-201", "English")
    await token_client.save_submission("STU-001", "CONTENT-101", "done")

    for route in (student_route, quiz_route, submit_route):
        assert route.calls.last.request.headers["authorization"] == "token real-frappe-token"


@pytest.mark.asyncio
@respx.mock
async def test_no_frappe_token_omits_authorization_header():
    """Default construction (no frappe_token) never sends the header at
    all — proves the header is opt-in, not silently required.
    """
    no_token_client = TapLmsClient(BASE_URL, "test-api-key")
    route = respx.post(f"{BASE_URL}/api/method/{GET_NEXT_CONTENT_METHOD}").mock(
        return_value=httpx.Response(
            200, json={"message": {"success": False, "status": "not_found"}}
        )
    )
    await no_token_client.get_next_content("STU-999")
    assert "authorization" not in route.calls.last.request.headers


@pytest.mark.asyncio
@respx.mock
async def test_get_next_content_with_token_unknown_student_not_found_shape():
    """Live-verified contract (tap-buddy 2026-07-03): with a valid token, an
    unknown student returns {"success": false, "status": "not_found",
    "error_detail": "Student not found"} — same business-failure shape the
    other 3 endpoints use, handled by the existing success===False branch.
    Re-verification against THIS stack is Phase 7's job.
    """
    token_client = TapLmsClient(BASE_URL, "test-api-key", frappe_token="real-frappe-token")
    respx.post(f"{BASE_URL}/api/method/{GET_NEXT_CONTENT_METHOD}").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "success": False,
                    "status": "not_found",
                    "error_detail": "Student not found",
                }
            },
        )
    )
    content = await token_client.get_next_content("STU-does-not-exist")
    assert content is None


def test_client_has_exactly_five_public_methods_no_scope_creep():
    """Guards against scope creep: no InteractionLog writes, no new
    whitelisted APIs — the client's public surface is exactly these 5
    methods (+ aclose for lifecycle). `start_quiz` joined 2026-07-17 under
    F-028 (nithun-directed design; provisionally registered by the
    tap-lms-frappe station, [[ANSWERS-TAPLMS-2026-07-17]] Q3, formal
    registration folds into CR-031). A 6th requires a PRD/CR.
    """
    public_methods = {
        name
        for name in dir(TapLmsClient)
        if not name.startswith("_") and callable(getattr(TapLmsClient, name))
    }
    expected = {
        "get_student_by_phone_and_name",
        "get_next_content",
        "get_quiz_question_details",
        "save_submission",
        "start_quiz",
        "aclose",
    }
    assert public_methods == expected


# ---- start_quiz (F-028, 5th endpoint) + answer-leak scrubbing --------------


@pytest.mark.asyncio
@respx.mock
async def test_start_quiz_parses_live_shape_and_scrubs_the_answer_leak() -> None:
    # Fixture mirrors the LIVE response shape from ANSWERS-TAPLMS-2026-07-17
    # Q3 — including the live-confirmed correct_option leak, which must
    # never be stored client-side (CR-031's server fix is parked).
    respx.post(f"{BASE_URL}/api/method/tap_lms.summer_program.student_progression_sp.start_quiz").mock(
        return_value=httpx.Response(
            200,
            json={
                "success": True,
                "status": "quiz_started",
                "quiz_attempt_id": "SQA-0001",
                "question_index": 1,
                "question_id": "Can a loan start your business? \U0001f4bcQN821599",
                "question_text": "What is a loan?",
                "option_a": "Borrowed money",
                "option_b": "A gift",
                "option_c": "A tax",
                "option_d": "A fine",
                "correct_option": "a",
                "correct_option_text": "Borrowed money",
                "total_questions": 3,
                "passing_score": 2,
            },
        )
    )
    client = TapLmsClient(BASE_URL, "k")
    result = await client.start_quiz("ST00491459", "Smart Savings-Financial Literacy-Level 2",
                                     "Can a loan start your business? \U0001f4bc-Basic")
    await client.aclose()

    assert result is not None
    assert result.question_id.endswith("QN821599")
    assert result.quiz_attempt_id == "SQA-0001"
    assert result.options == ["Borrowed money", "A gift", "A tax", "A fine"]
    assert result.total_questions == 3
    # THE guard: the answer key exists nowhere on the result object.
    assert not hasattr(result, "correct_option")
    assert "correct_option" not in result.raw
    assert "correct_option_text" not in result.raw
    assert "Borrowed money" == result.options[0]  # options themselves are fine


@pytest.mark.asyncio
@respx.mock
async def test_start_quiz_business_failure_returns_none() -> None:
    respx.post(f"{BASE_URL}/api/method/tap_lms.summer_program.student_progression_sp.start_quiz").mock(
        return_value=httpx.Response(200, json={"success": False, "status": "quiz_not_found"})
    )
    client = TapLmsClient(BASE_URL, "k")
    assert await client.start_quiz("ST-X", "CL-X", "no-such-quiz") is None
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_get_quiz_question_details_raw_is_scrubbed_of_correct_option() -> None:
    respx.post(f"{BASE_URL}/api/method/tap_lms.api.get_quiz_question_details").mock(
        return_value=httpx.Response(
            200,
            json={
                "message": {
                    "question_text": "What is a loan?",
                    "hint": "Think about borrowing.",
                    "options": ["a", "b"],
                    "correct_option": "a",
                    "correct_option_text": "Borrowed money",
                }
            },
        )
    )
    client = TapLmsClient(BASE_URL, "k")
    details = await client.get_quiz_question_details("Q-1", "English")
    await client.aclose()
    assert details is not None
    assert "correct_option" not in details.raw
    assert "correct_option_text" not in details.raw
    assert details.hint == "Think about borrowing."
