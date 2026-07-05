"""Loader for language_map.yaml — the LMS tap_language -> Google STT/TTS
locale mapping, config-driven, not hardcoded in application code.

Ported verbatim from `tap-buddy/src/tap_buddy/config/language_map.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).parent / "language_map.yaml"


@dataclass
class LocaleEntry:
    tap_language: str
    stt_locale: str
    tts_voice: str
    tts_language_code: str
    supported: bool
    fallback_reason: str | None = None


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        loaded: dict[str, Any] = yaml.safe_load(f)
        return loaded


def _entry_from_dict(name: str, d: dict[str, Any]) -> LocaleEntry:
    return LocaleEntry(
        tap_language=name,
        stt_locale=d["stt_locale"],
        tts_voice=d["tts_voice"],
        tts_language_code=d["tts_language_code"],
        supported=d.get("supported", False),
        fallback_reason=d.get("fallback_reason"),
    )


def all_entries() -> dict[str, LocaleEntry]:
    data = _load()
    return {k: _entry_from_dict(k, v) for k, v in data.items() if not k.startswith("_")}


def fallback_language(secondary: bool = False) -> str:
    data = _load()
    key = "secondary" if secondary else "primary"
    value: str = data["_fallback"][key]
    return value


def resolve_locale(tap_language: str | None) -> LocaleEntry:
    """Resolve an LMS tap_language value to its locale entry. Missing,
    unrecognized, or explicitly unsupported languages resolve to the
    primary fallback (English) per language_map.yaml's `_fallback.primary`.
    """
    entries = all_entries()
    if tap_language and tap_language in entries and entries[tap_language].supported:
        return entries[tap_language]
    return entries[fallback_language()]
