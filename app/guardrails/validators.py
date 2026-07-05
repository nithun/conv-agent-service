"""Guardrails AI validator classes wrapping the deterministic checks in
`app/core/guardrails_checks.py` (CR-004 Task 11, R-5, P-004, ADR-007).

Each validator wraps ONE pure check function — the checks themselves stay
in `app/core/guardrails_checks.py` (channel-agnostic, isolation-tested);
this module is purely the Guardrails AI adapter layer, per P-004's shape
("a Guardrails AI validator registered in the output-validation pass
between LLM output and Glific delivery, with a test feeding a violating
output and asserting it is blocked/transformed").

The 3 keyword detectors (distress/doubt/off-turn) remain deterministic
Python gates used directly by `app/core/turn_logic.py` — NOT re-wrapped as
Guardrails AI validators here, because they are pre-LLM-call
short-circuits (they decide whether to call the model at all), not
post-LLM-output checks on a candidate reply. Guardrails AI's validator
contract is specifically "validate this piece of text," which fits the 4
reply-shape checks below, not a pre-call routing decision.
"""

from __future__ import annotations

from typing import Any

from guardrails.validator_base import (
    FailResult,
    PassResult,
    ValidationResult,
    Validator,
    register_validator,
)

from app.core.guardrails_checks import (
    ValidationFailure,
    check_length,
    check_no_answer_leakage,
    check_no_fabricated_content,
    check_single_question,
)


@register_validator(name="tap_agent/reply-length", data_type="string")
class ReplyLength(Validator):  # type: ignore[misc]
    """At most 3 sentences per reply (P-004: a violating-output test feeds
    a too-long reply and asserts it's blocked).
    """

    def __init__(self, max_sentences: int = 3, **kwargs: Any):
        super().__init__(max_sentences=max_sentences, **kwargs)
        self._max_sentences = max_sentences

    def validate(self, value: Any, metadata: dict[str, Any]) -> ValidationResult:
        failure = check_length(value, max_sentences=self._max_sentences)
        if failure is not None:
            return FailResult(
                error_message=f"reply exceeds {self._max_sentences} sentences ({failure.value})"
            )
        return PassResult()


@register_validator(name="tap_agent/single-question", data_type="string")
class SingleQuestion(Validator):  # type: ignore[misc]
    """At most one question per reply."""

    def validate(self, value: Any, metadata: dict[str, Any]) -> ValidationResult:
        failure = check_single_question(value)
        if failure is not None:
            return FailResult(error_message=f"reply asks more than one question ({failure.value})")
        return PassResult()


@register_validator(name="tap_agent/no-answer-leakage", data_type="string")
class NoAnswerLeakage(Validator):  # type: ignore[misc]
    """F-011: no `correct_option` parameter — the type-level guard
    (`QuizQuestionDetails` carrying no `correct_option` field) is the sole
    real protection; this validator is retained as a shape/seam for a
    future, deliberately-scoped fix, honestly documented as a no-op today
    (see `app.core.guardrails_checks.check_no_answer_leakage`'s docstring).
    """

    def validate(self, value: Any, metadata: dict[str, Any]) -> ValidationResult:
        failure = check_no_answer_leakage(value)
        if failure is not None:
            return FailResult(error_message=f"reply leaks an answer ({failure.value})")
        return PassResult()


@register_validator(name="tap_agent/no-fabricated-content", data_type="string")
class NoFabricatedContent(Validator):  # type: ignore[misc]
    """Rejects replies referencing `[[field]]` markers not present in the
    session's actual LMS content reference — `allowed_content_fields` is
    passed via `metadata` (Guardrails AI's per-call metadata dict), not a
    constructor argument, since it varies per turn (the session's current
    content), not per validator instance.
    """

    def validate(self, value: Any, metadata: dict[str, Any]) -> ValidationResult:
        allowed_content_fields: set[str] = metadata.get("allowed_content_fields", set())
        failure = check_no_fabricated_content(value, allowed_content_fields)
        if failure is not None:
            return FailResult(
                error_message=f"reply references unavailable content ({failure.value})"
            )
        return PassResult()


def run_all_validators(
    reply: str, *, allowed_content_fields: set[str] | None = None
) -> list[ValidationFailure]:
    """Convenience aggregate mirroring `app.core.guardrails_checks.run_guardrails`'s
    shape but going through the Guardrails AI validator classes themselves
    (not calling the pure functions directly) — this is what a caller uses
    if it wants "the Guardrails AI validation pass," as distinct from the
    pure-function checks used internally by `app.core.turn_logic`.
    """
    allowed_content_fields = allowed_content_fields or set()
    failures: list[ValidationFailure] = []
    metadata = {"allowed_content_fields": allowed_content_fields}

    validators: list[Validator] = [
        ReplyLength(),
        SingleQuestion(),
        NoAnswerLeakage(),
        NoFabricatedContent(),
    ]
    for validator in validators:
        result = validator.validate(reply, metadata)
        if isinstance(result, FailResult):
            failures.append(_failure_for_validator(validator))
    return failures


def _failure_for_validator(validator: Validator) -> ValidationFailure:
    mapping: dict[type[Validator], ValidationFailure] = {
        ReplyLength: ValidationFailure.TOO_LONG,
        SingleQuestion: ValidationFailure.MULTIPLE_QUESTIONS,
        NoAnswerLeakage: ValidationFailure.ANSWER_LEAKAGE,
        NoFabricatedContent: ValidationFailure.FABRICATED_CONTENT,
    }
    return mapping[type(validator)]
