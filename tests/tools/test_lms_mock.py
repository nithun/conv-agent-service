"""Fixture-backed LMS client tests — same 4-method surface as the real
client, deterministic fixtures for offline dev + tests.

Re-targeted from tap-buddy/tests/test_lms_mock.py against
app.tools.lms_mock (CR-004 Task 12).
"""

from __future__ import annotations

import pytest

from app.tools.lms_mock import MockTapLmsClient


@pytest.mark.asyncio
async def test_mock_single_candidate_phone():
    client = MockTapLmsClient()
    result = await client.get_student_by_phone_and_name("917834811114", "Aarav")
    assert result.success is True
    assert len(result.candidates) == 1


@pytest.mark.asyncio
async def test_mock_multi_candidate_phone():
    client = MockTapLmsClient()
    result = await client.get_student_by_phone_and_name("919999999999", "Meera")
    assert result.success is True
    assert len(result.candidates) == 2


@pytest.mark.asyncio
async def test_mock_unknown_phone():
    client = MockTapLmsClient()
    result = await client.get_student_by_phone_and_name("000", "Nobody")
    assert result.success is False
    assert result.candidates == []


@pytest.mark.asyncio
async def test_mock_get_next_content():
    client = MockTapLmsClient()
    content = await client.get_next_content("STU-001")
    assert content is not None
    assert content.content_type == "video"


@pytest.mark.asyncio
async def test_mock_get_next_content_missing():
    client = MockTapLmsClient()
    assert await client.get_next_content("STU-999") is None


@pytest.mark.asyncio
async def test_mock_get_quiz_question_details():
    client = MockTapLmsClient()
    details = await client.get_quiz_question_details("Q-201", "English")
    assert details is not None
    assert details.hint is not None


@pytest.mark.asyncio
async def test_mock_save_submission_records_call():
    client = MockTapLmsClient()
    result = await client.save_submission("STU-001", "CONTENT-101", "done")
    assert result.success is True
    assert len(client.submissions) == 1
    assert client.submissions[0]["student_id"] == "STU-001"


@pytest.mark.asyncio
async def test_mock_save_submission_rejects_unknown_content_id():
    """F-030: the live CR-005 contract validates `content_id` against the
    Assignment doctype — unknown ids get
    `{"success": false, "status": "invalid_assignment", ...}` with no
    Submission created. Previously this fixture accepted ANY content_id and
    always returned success=True, so mocked tests could not exercise the
    rejection path the real client already maps (`app/tools/lms_client.py`
    `save_submission`).
    """
    client = MockTapLmsClient()
    result = await client.save_submission("STU-001", "NOT-A-REAL-CONTENT-ID", "done")
    assert result.success is False
    assert result.status == "invalid_assignment"
    assert result.message is not None
    # No Submission recorded for a rejected content id.
    assert client.submissions == []


@pytest.mark.asyncio
async def test_mock_save_submission_accepts_any_content_id_the_mock_has_vended():
    """Every content_id the fixture's own get_next_content data can hand
    out must still be accepted — the validation is against "vended"
    Assignment ids, not a hardcoded single value.
    """
    client = MockTapLmsClient()
    for content_id in ("CONTENT-101", "CONTENT-102", "CONTENT-103"):
        result = await client.save_submission("STU-001", content_id, "done")
        assert result.success is True
        assert result.status == "submitted"


# ---- register_test_student / reset_test_student (CR-011/ADR-013 6/7) ------


@pytest.mark.asyncio
async def test_mock_register_test_student_creates_new_id():
    client = MockTapLmsClient()
    result = await client.register_test_student(name1="New Tester", phone="9000000001")
    assert result.success is True
    assert result.created_student is True
    assert result.created_pe is True
    assert result.student_id
    assert len(client.register_test_student_calls) == 1
    assert client.register_test_student_calls[0]["name1"] == "New Tester"


@pytest.mark.asyncio
async def test_mock_register_test_student_idempotent_on_phone_and_name():
    client = MockTapLmsClient()
    first = await client.register_test_student(name1="New Tester", phone="9000000001")
    second = await client.register_test_student(name1="New Tester", phone="9000000001")
    assert first.student_id == second.student_id
    assert first.created_student is True
    assert second.created_student is False


@pytest.mark.asyncio
async def test_mock_reset_test_student_records_call():
    client = MockTapLmsClient()
    result = await client.reset_test_student("STU-001")
    assert result.success is True
    assert result.student_id == "STU-001"
    assert result.pe_reset is True
    assert client.reset_test_student_calls == [{"student_id": "STU-001", "push_to_glific": False}]


# ---- get_test_student_by_phone (CR-013/ADR-014 Ask A) ----------------------


@pytest.mark.asyncio
async def test_mock_get_test_student_by_phone_default_is_zero_match():
    """The fixture's `test_batch_students` dict is SEPARATE from
    `students` and defaults empty — a phone that resolves via
    `get_student_by_phone_and_name`'s DEFAULT_FIXTURE_STUDENTS must NOT
    also resolve here unless a test opts in explicitly.
    """
    client = MockTapLmsClient()
    result = await client.get_test_student_by_phone("917834811114")
    assert result.success is True
    assert result.matched_student_count == 0
    assert result.students == []
    assert client.test_student_by_phone_calls == ["917834811114"]


@pytest.mark.asyncio
async def test_mock_get_test_student_by_phone_single_match():
    from app.tools.lms_client import StudentCandidate

    client = MockTapLmsClient(
        test_batch_students={
            "9000000011": [
                StudentCandidate(
                    student_id="ST00491460", child_name="CR011 Tester One", tap_language="English"
                )
            ]
        }
    )
    result = await client.get_test_student_by_phone("9000000011")
    assert result.matched_student_count == 1
    assert result.students[0].student_id == "ST00491460"
