"""Guardrails AI validator classes (CR-004 Task 11, ADR-007, P-004).

`validators.py` wraps `app/core/guardrails_checks.py`'s 4 deterministic
reply-shape checks (length, single-question, no-answer-leakage,
no-fabricated-content) as Guardrails AI `Validator` subclasses, each with
its own violating-output test. The 3 keyword detectors (distress/doubt/
off-turn) stay as plain deterministic Python in `app/core/guardrails_checks.py`
— they are pre-LLM-call routing decisions, not post-output validators, so
they are not wrapped here.
"""
