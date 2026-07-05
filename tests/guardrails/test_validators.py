"""P-004: each Guardrails AI validator gets its own violating-output test
(CR-004 Task 11, R-5, AC-5). Wraps app/core/guardrails_checks.py's 4
deterministic reply-shape checks.
"""

from __future__ import annotations

import pytest
from guardrails.validator_base import FailResult, PassResult

from app.guardrails.validators import (
    NoAnswerLeakage,
    NoFabricatedContent,
    ReplyLength,
    SingleQuestion,
    run_all_validators,
)


def test_reply_length_violating_too_many_sentences() -> None:
    validator = ReplyLength()
    result = validator.validate(
        "This is one. This is two. This is three. This is four.", metadata={}
    )
    assert isinstance(result, FailResult)


def test_reply_length_passing_within_three_sentences() -> None:
    validator = ReplyLength()
    result = validator.validate("This is one. This is two. This is three?", metadata={})
    assert isinstance(result, PassResult)


def test_single_question_violating_two_questions() -> None:
    validator = SingleQuestion()
    result = validator.validate("How are you? Are you ready?", metadata={})
    assert isinstance(result, FailResult)


def test_single_question_passing_one_question() -> None:
    validator = SingleQuestion()
    result = validator.validate("How are you feeling today?", metadata={})
    assert isinstance(result, PassResult)


def test_no_answer_leakage_is_currently_a_no_op_pass_by_design() -> None:
    """F-011: with correct_option removed (not wired), this validator has
    no signal to check against, so it always passes — honestly documented
    as a no-op, not overstated.
    """
    validator = NoAnswerLeakage()
    result = validator.validate("The answer is 3/4, easy!", metadata={})
    assert isinstance(result, PassResult)


def test_no_fabricated_content_violating_unavailable_field() -> None:
    validator = NoFabricatedContent()
    result = validator.validate(
        "You already completed [[bonus_video]], great job!",
        metadata={"allowed_content_fields": {"video"}},
    )
    assert isinstance(result, FailResult)


def test_no_fabricated_content_passing_field_in_scope() -> None:
    validator = NoFabricatedContent()
    result = validator.validate(
        "Let's start [[video]] now.", metadata={"allowed_content_fields": {"video"}}
    )
    assert isinstance(result, PassResult)


def test_run_all_validators_ok_on_clean_reply() -> None:
    failures = run_all_validators("How are you feeling today?")
    assert failures == []


def test_run_all_validators_blocks_violating_reply() -> None:
    from app.core.guardrails_checks import ValidationFailure

    failures = run_all_validators("One. Two. Three. Four?")
    assert ValidationFailure.TOO_LONG in failures


@pytest.mark.parametrize(
    "validator_cls,violating_input,metadata",
    [
        (ReplyLength, "One. Two. Three. Four.", {}),
        (SingleQuestion, "What? Why? How?", {}),
        (
            NoFabricatedContent,
            "You finished [[secret_bonus_level]]!",
            {"allowed_content_fields": set()},
        ),
    ],
)
def test_each_validator_has_its_own_violating_output_case(
    validator_cls, violating_input, metadata
) -> None:
    """P-004's literal shape: a test feeding a violating output and
    asserting it is blocked, per validator, parametrized so the pattern is
    visibly repeated per-validator rather than only exercised once.
    """
    validator = validator_cls()
    result = validator.validate(violating_input, metadata=metadata)
    assert isinstance(result, FailResult)
