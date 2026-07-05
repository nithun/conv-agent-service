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
