"""Loader for scripted_replies.yaml — child-facing scripted (non-LLM) reply
text and mode/language-choice matching keywords, keyed by LMS `tap_language`
value (CR-007 GAP 1/2/0). Mirrors distress_keywords.py/language_map.py's
`lru_cache`d config-driven-language loader pattern exactly.

Deliberate divergence from `distress_keywords.get_distress_patterns`'s
English+Hindi-combined fallback: a scripted reply is ONE sentence shown to
a child — concatenating two languages into one reply would read as
garbled, not "extra coverage" — so the fallback here is English ALONE.

Translations are model-authored (CR-007) — a native-speaker/TAP-team
review is a tracked follow-up (F-034), not a precondition for this loader
(mirrors the pre-existing distress_keywords.yaml caveat, F-009).

`tap_language`/`key` are fixed LMS-enum-shaped values, not free text — safe
to log (mirrors get_distress_patterns' comment).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config.language_map import all_entries

logger = logging.getLogger("app.config.scripted_replies")

_CONFIG_PATH = Path(__file__).parent / "scripted_replies.yaml"
_FALLBACK_LANGUAGE = "English"
_LANGUAGE_CHOICE_KEY = "_language_choice"

# The 10 language-eligible scripted-reply keys (AC-1/AC-2) — every language
# block must carry all of these; the structural key-parity test asserts it.
SCRIPTED_REPLY_KEYS: tuple[str, ...] = (
    "greeting_direct",
    "greeting_candidate_confirm",
    "readiness_mode_choice",
    "mode_choice_reask",
    "fallback",
    "distress_close",
    "clarification",
    "asr_fallback",
    "unresolved_doubt_escalation",
    "off_turn_nudge",
)

MODE_KEYWORD_FIELDS: tuple[str, str] = ("mode_voice_keywords", "mode_text_keywords")
LANGUAGE_NAME_KEYWORDS_FIELD = "language_name_keywords"


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        loaded: dict[str, Any] = yaml.safe_load(f)
        return loaded


def get_scripted_reply(key: str, tap_language: str | None) -> str:
    """Looks up a scripted reply `key` for `tap_language`. Falls back to
    the English block's value for that key whenever the language is
    missing OR the language's block doesn't carry that key — never
    concatenates two languages into one reply (see module docstring).
    """
    data = _load()
    language_block = data.get(tap_language) if tap_language else None
    if language_block and key in language_block:
        return str(language_block[key])
    logger.warning(
        "scripted_replies: no %r entry for language=%r; falling back to English",
        key,
        tap_language,
    )
    return str(data[_FALLBACK_LANGUAGE][key])


def get_mode_choice_keywords(tap_language: str | None) -> dict[str, list[str]]:
    """Returns {"voice": [...], "text": [...]} — the session language's own
    keyword lists UNIONED with English's (English stays a universal
    fallback trigger regardless of the session's resolved language, per
    CR-007 Scope of change).
    """
    data = _load()
    english = data[_FALLBACK_LANGUAGE]
    language_block = data.get(tap_language) if tap_language else None

    voice: list[str] = list(english.get("mode_voice_keywords", []))
    text: list[str] = list(english.get("mode_text_keywords", []))

    if language_block and tap_language != _FALLBACK_LANGUAGE:
        for kw in language_block.get("mode_voice_keywords", []):
            if kw not in voice:
                voice.append(kw)
        for kw in language_block.get("mode_text_keywords", []):
            if kw not in text:
                text.append(kw)

    return {"voice": voice, "text": text}


def get_language_choice_keywords() -> list[tuple[str, list[str]]]:
    """Returns [(language_name, keyword_list), ...] in `language_map.yaml`'s
    DECLARED order (English first) — the order the LANGUAGE_CHOICE turn
    matches against; first substring match wins (CR-007 Edge cases:
    deterministic, not a defect if an ambiguous reply resolves to the
    earlier-ordered language).
    """
    data = _load()
    ordered_languages = list(all_entries().keys())
    return [
        (language, list(data[language].get(LANGUAGE_NAME_KEYWORDS_FIELD, [])))
        for language in ordered_languages
        if language in data
    ]


def language_choice_prompt() -> str:
    return str(_load()[_LANGUAGE_CHOICE_KEY]["prompt"])


def language_choice_reask() -> str:
    return str(_load()[_LANGUAGE_CHOICE_KEY]["reask"])


def all_language_keys() -> list[str]:
    """Every language block key present in scripted_replies.yaml, excluding
    the reserved `_language_choice` meta block (mirrors
    distress_keywords.supported_languages()) — used by the structural
    key-parity test (AC-1).
    """
    return [k for k in _load() if not k.startswith("_")]
