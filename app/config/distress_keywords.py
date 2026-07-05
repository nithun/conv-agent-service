"""Loader for distress_keywords.yaml — pattern config, not code.

Ported verbatim (behavior + data) from
`tap-buddy/src/tap_buddy/config/distress_keywords.py` per CR-004/ADR-010
Invariant 4 (BR-002's already-correct all-10-language keyword coverage
ports forward as-is, no code fix needed).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml

logger = logging.getLogger("app.config.distress_keywords")

_CONFIG_PATH = Path(__file__).parent / "distress_keywords.yaml"
_FALLBACK_LANGUAGE = "English"
_SECONDARY_FALLBACK_LANGUAGE = "Hindi"


@lru_cache(maxsize=1)
def _load() -> dict[str, list[str]]:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        loaded: dict[str, list[str]] = yaml.safe_load(f)
        return loaded


def get_distress_patterns(locale_key: str | None) -> list[str]:
    """Returns the regex pattern list for the given tap_language value.

    Safety net: a language with no dedicated entry in
    distress_keywords.yaml never silently degrades to a single (likely
    wrong-script) list. Instead it runs BOTH the English and Hindi lists
    combined, and logs a warning so the coverage gap is visible rather than
    silent. `locale_key` itself is not PII and is safe to log (it is a
    fixed LMS `tap_language` enum value, not free text).
    """
    data = _load()
    if locale_key and locale_key in data:
        return data[locale_key]
    logger.warning(
        "distress_keywords: no dedicated pattern list for language=%r; "
        "falling back to combined English+Hindi coverage",
        locale_key,
    )
    return data[_FALLBACK_LANGUAGE] + data[_SECONDARY_FALLBACK_LANGUAGE]


def supported_languages() -> list[str]:
    return list(_load().keys())
