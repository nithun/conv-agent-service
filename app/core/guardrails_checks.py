"""Deterministic guardrail check functions — the pure logic ported from
`tap-buddy/src/tap_buddy/core/guardrails.py` (CR-004 Task 11).

These functions are never prompt-only (L-006). This module holds the raw
checks; `app/guardrails/validators.py` wraps each as a Guardrails AI
validator class (per P-004/ADR-007) — this file itself does not import
guardrails-ai so the pure-Python logic stays independently testable and
mirrors tap-buddy's original structure for easy comparison.

F-011 resolution (Nithun, 2026-07-05, decisions.jsonl
"f-011-resolution-final-remove-not-wire"): `check_no_answer_leakage` NO
LONGER accepts a `correct_option` parameter. tap-buddy's version had a dead
`correct_option: str | None` param that its only live caller never
populated, plus a "belt-and-suspenders" docstring overstating a second
layer of protection that never actually fired. That parameter and the
overstated claim are REMOVED here, not wired to a real value — even though
F-012 confirmed TAP LMS's `get_quiz_question_details` DOES return
`correct_option`/`correct_option_text` live, Nithun explicitly decided
against wiring it in (this port's scope stays narrower; F-011/F-012 remain
open on the "should we build server-side-verified leakage protection"
question). The type-level guard — `QuizQuestionDetails` (`app/tools/lms_client.py`)
carries NO `correct_option` field — remains the sole, honestly-documented
protection against a tool response ever including the answer verbatim in
what reaches this check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from app.config.distress_keywords import get_distress_patterns


class ValidationFailure(StrEnum):
    TOO_LONG = "too_long"
    MULTIPLE_QUESTIONS = "multiple_questions"
    NO_QUESTION = "no_question"
    ANSWER_LEAKAGE = "answer_leakage"
    FABRICATED_CONTENT = "fabricated_content"


@dataclass
class GuardrailResult:
    ok: bool
    failures: list[ValidationFailure]
    reply: str


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।])\s+")

# Double/curly quotes only — ASCII single quotes double as apostrophes
# ("It's... Let's...") and would swallow the text between them.
_QUOTED_SEGMENT_RE = re.compile(r'"[^"]*"|“[^”]*”')


def _split_sentences(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    parts = [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]
    return parts


def check_length(reply: str, max_sentences: int = 3) -> ValidationFailure | None:
    """Response length: 1-3 sentences.

    Quoted segments (content titles like "Can a small idea grow?") are
    masked before splitting so their punctuation doesn't count as extra
    sentence boundaries (ported false-positive fix from tap-buddy,
    2026-07-03).
    """
    sentences = _split_sentences(_QUOTED_SEGMENT_RE.sub("«title»", reply))
    if len(sentences) > max_sentences:
        return ValidationFailure.TOO_LONG
    return None


def check_single_question(reply: str) -> ValidationFailure | None:
    """Exactly one question per turn is allowed; zero is fine (not every
    turn needs a question, e.g. distress-close messages), but never more
    than one.

    Quoted segments are stripped before counting: LMS content titles are
    often questions themselves ("Can a small idea grow?"), and a reply that
    names the title plus asks one real question was falsely rejected live
    (ported fix, 2026-07-03).
    """
    question_count = _QUOTED_SEGMENT_RE.sub("", reply).count("?")
    if question_count > 1:
        return ValidationFailure.MULTIPLE_QUESTIONS
    return None


def check_no_answer_leakage(reply: str) -> ValidationFailure | None:
    """Type-level-guard-backed check: this port carries no `correct_option`
    parameter (F-011, removed not wired — see module docstring). The check
    itself is retained as a shape/seam (so a future, deliberately-scoped fix
    can add real leakage detection against actually-available data without
    a signature change here being the blocker) but currently has no signal
    to check against, so it always passes. Honestly documented as a no-op
    today — this function is NOT a second, independent layer of leakage
    protection; the type-level guard (`QuizQuestionDetails` carrying no
    `correct_option` field) is the sole real protection.
    """
    return None


def check_no_fabricated_content(
    reply: str, allowed_content_fields: set[str]
) -> ValidationFailure | None:
    """Reject replies referencing content fields not present in the current
    state's LMS content reference. `allowed_content_fields` is the set of
    string values pulled from the session's content_reference (title,
    content_type, etc.) — a reply may only reference lesson/progress facts
    that trace back to one of those, this is a coarse marker-based check
    (any bracketed [[field]] marker not in the allow-list fails), not full
    semantic verification.
    """
    referenced = re.findall(r"\[\[(.+?)\]\]", reply)
    for field in referenced:
        if field not in allowed_content_fields:
            return ValidationFailure.FABRICATED_CONTENT
    return None


def run_guardrails(
    reply: str,
    *,
    allowed_content_fields: set[str] | None = None,
    require_question: bool = False,
) -> GuardrailResult:
    """Run all deterministic validators against a candidate reply. Returns a
    GuardrailResult; callers (turn_logic.py) decide what to do with a
    failure (regenerate, truncate, or escalate) — this module only detects.
    """
    allowed_content_fields = allowed_content_fields or set()
    failures: list[ValidationFailure] = []

    for check in (
        check_length(reply),
        check_single_question(reply),
        check_no_answer_leakage(reply),
        check_no_fabricated_content(reply, allowed_content_fields),
    ):
        if check is not None:
            failures.append(check)

    if require_question and reply.count("?") == 0:
        failures.append(ValidationFailure.NO_QUESTION)

    return GuardrailResult(ok=not failures, failures=failures, reply=reply)


def detect_distress(text: str, locale_key: str | None) -> bool:
    """Keyword/pattern-based distress detector (abuse, self-harm, fear,
    sickness, safety), matched against the resolved language locale so the
    detector isn't English-only. `locale_key` is the LMS tap_language value
    (e.g. "Hindi", "English") used to select the keyword list from the
    distress_keywords config.
    """
    patterns = get_distress_patterns(locale_key)
    lowered = text.lower()
    return any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in patterns)


# ---- doubt / confusion signal ----------------------------------------------
#
# Deterministic keyword/intent check, same shape as detect_distress — never
# an LLM free-text-trust decision. A confusion signal transitions
# activity_in_progress -> doubt_clearing; the LLM's tool-call/text content is
# NOT the trigger, only this keyword match is.

_DOUBT_SIGNAL_PATTERNS: tuple[str, ...] = (
    r"\bi\s+(?:\w+\s+){0,2}don'?t\s+understand\b",  # tolerates "still"/"really" etc. in between
    r"\bdidn'?t\s+understand\b",
    r"\bdon'?t\s+get\s+it\b",
    r"\bdoesn'?t\s+make\s+sense\b",
    r"\bi'?m\s+(?:\w+\s+){0,2}confused\b",  # tolerates "so"/"still"/"really" etc. in between
    r"\bconfusing\b",
    r"\bwhat\s+does\s+that\s+mean\b",
    r"\bcan\s+you\s+explain\b",
    r"\bexplain\s+again\b",
    r"\bnot\s+clear\b",
    r"\bi\s+don'?t\s+know\s+how\b",
    r"\bstill\s+don'?t\s+get\b",
    r"\?\?+",  # "???" style confusion
)


def detect_doubt_signal(text: str) -> bool:
    """Deterministic confusion/doubt detector — a simple keyword/intent
    check (never free-text LLM trust). Language-agnostic MVP scope: matches
    the English phrasing patterns most likely to appear verbatim or via the
    resolved reply text; non-English confusion is still caught via the
    LLM's own reply content triggering re-explanation once already in
    doubt_clearing (re-entrant), and via the same keyword set applied to
    ASR transcripts (which Google STT already normalizes toward Latin/
    English loanwords for common phrases in code-switched speech).
    """
    if not text:
        return False
    lowered = text.lower()
    return any(
        re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in _DOUBT_SIGNAL_PATTERNS
    )


# ---- off-turn / unrelated input signal -------------------------------------
#
# Deterministic keyword check for input that is plausibly off-topic/
# unrelated to the current lesson turn — network complaints, meta-comments
# about the bot/session, or explicit topic changes. This is intentionally
# narrow (a coarse allow-list of clear off-turn phrasings) so it does not
# false-positive on ordinary lesson replies; ambiguous input still goes to
# the LLM as before.

_OFF_TURN_SIGNAL_PATTERNS: tuple[str, ...] = (
    r"\bnetwork\s+is\s+bad\b",
    r"\bmy\s+network\b",
    r"\bnetwork\s+issue\b",
    r"\bcan'?t\s+hear\s+you\b",
    r"\bwrong\s+number\b",
    r"\bwho\s+is\s+this\b",
    r"\bare\s+you\s+a\s+robot\b",
    r"\bare\s+you\s+a\s+bot\b",
    r"\bstop\s+messaging\b",
    r"\bwait\s+a\s+minute\b",
    r"\bhold\s+on\b",
    r"\bwrong\s+group\b",
)


def detect_off_turn_signal(text: str) -> bool:
    """Deterministic off-turn/unrelated-input detector — a keyword check,
    never LLM free-text trust, same shape as detect_doubt_signal. Callers
    short-circuit to a scripted acknowledge-then-redirect reply instead of
    invoking the LLM for these turns.
    """
    if not text:
        return False
    lowered = text.lower()
    return any(
        re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in _OFF_TURN_SIGNAL_PATTERNS
    )
