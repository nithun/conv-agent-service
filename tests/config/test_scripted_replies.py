"""scripted_replies.yaml/py loader tests (CR-007 GAP 1/2): fallback
contract (missing language -> English; missing key within a present
language -> English's value for that key, with a logger.warning asserted —
mirrors distress_keywords.py's fallback test pattern), structural
key-completeness against language_map.py.all_entries() (AC-1), and the
mode/language-choice keyword union behavior (AC-4/AC-5).

The AC-2/AC-3 static reachability checks (no literal English copy of any
scripted-reply content surviving in turn_processor.py/turn_logic.py/
distress_gate.py) live in `tests/test_scripted_replies_reachability.py`
instead — that's the acceptance test for the CALL-SITE MIGRATION, not for
this loader.
"""

from __future__ import annotations

import logging

import pytest

from app.config.language_map import all_entries
from app.config.scripted_replies import (
    LANGUAGE_NAME_KEYWORDS_FIELD,
    MODE_KEYWORD_FIELDS,
    SCRIPTED_REPLY_KEYS,
    all_language_keys,
    get_language_choice_keywords,
    get_mode_choice_keywords,
    get_reset_keyword,
    get_scripted_reply,
    language_choice_prompt,
    language_choice_reask,
)

# ---- structural key-parity (AC-1) -----------------------------------------


def test_every_language_map_language_has_a_scripted_replies_block():
    """Fails loudly if a language is added to language_map.yaml and
    forgotten here (AC-1's explicit requirement).
    """
    language_map_keys = set(all_entries().keys())
    scripted_reply_keys = set(all_language_keys())
    assert language_map_keys <= scripted_reply_keys, (
        f"missing scripted_replies.yaml blocks for: {language_map_keys - scripted_reply_keys}"
    )


def test_every_language_block_has_all_10_reply_keys_and_keyword_fields():
    # get_scripted_reply() never raises (it falls back) — read the raw
    # loaded dict via the module's private _load() to distinguish "key
    # present" from "key fell back to English".
    import app.config.scripted_replies as scripted_replies_module

    data = scripted_replies_module._load()
    for language in all_entries():
        block = data[language]
        for key in SCRIPTED_REPLY_KEYS:
            assert key in block, f"{language} is missing scripted_replies key {key!r}"
        for field in MODE_KEYWORD_FIELDS:
            assert block.get(field), f"{language} is missing/empty {field!r}"
        assert block.get(LANGUAGE_NAME_KEYWORDS_FIELD), (
            f"{language} is missing/empty {LANGUAGE_NAME_KEYWORDS_FIELD!r}"
        )


def test_language_choice_block_has_prompt_and_reask():
    assert language_choice_prompt()
    assert language_choice_reask()


# ---- _reset_keyword / reset_confirmation (CR-011/ADR-013 Task 6) ----------


def test_get_reset_keyword_is_testairreset():
    assert get_reset_keyword() == "TESTAIRESET"


def test_reset_confirmation_present_and_language_specific():
    # A per-item check (L-021) that reset_confirmation is genuinely
    # per-language content, not the same English string copy-pasted into
    # every block — mirrors the existing greeting_direct spot-check above.
    assert get_scripted_reply("reset_confirmation", "Hindi") != get_scripted_reply(
        "reset_confirmation", "English"
    )
    assert get_scripted_reply("reset_confirmation", "English")


# ---- get_scripted_reply fallback contract ---------------------------------


def test_get_scripted_reply_returns_the_languages_own_value():
    assert get_scripted_reply("fallback", "Hindi") != get_scripted_reply("fallback", "English")
    assert "नमस्ते" in get_scripted_reply("greeting_direct", "Hindi")


def test_get_scripted_reply_missing_language_falls_back_to_english(caplog):
    with caplog.at_level(logging.WARNING, logger="app.config.scripted_replies"):
        result = get_scripted_reply("fallback", "Klingon")
    assert result == get_scripted_reply("fallback", "English")
    assert any("Klingon" in r.getMessage() for r in caplog.records)


def test_get_scripted_reply_none_language_falls_back_to_english(caplog):
    with caplog.at_level(logging.WARNING, logger="app.config.scripted_replies"):
        result = get_scripted_reply("fallback", None)
    assert result == get_scripted_reply("fallback", "English")


def test_get_scripted_reply_missing_key_within_present_language_falls_back(monkeypatch, caplog):
    # Simulate a language block that's present but (hypothetically) missing
    # one key — mirrors distress_keywords.py's "language present, category
    # missing" tolerance.
    import app.config.scripted_replies as scripted_replies_module

    fake_data = {
        "Hindi": {k: "x" for k in SCRIPTED_REPLY_KEYS if k != "fallback"},
        "English": {k: f"english-{k}" for k in SCRIPTED_REPLY_KEYS},
    }
    monkeypatch.setattr(scripted_replies_module, "_load", lambda: fake_data)

    with caplog.at_level(logging.WARNING, logger="app.config.scripted_replies"):
        result = scripted_replies_module.get_scripted_reply("fallback", "Hindi")
    assert result == "english-fallback"
    assert any("fallback" in r.getMessage() and "Hindi" in r.getMessage() for r in caplog.records)


def test_get_scripted_reply_never_concatenates_two_languages():
    # Deliberate divergence from get_distress_patterns' combined fallback —
    # the result is always exactly ONE language's string, never a
    # concatenation of two.
    result = get_scripted_reply("fallback", "Klingon")
    assert result == get_scripted_reply("fallback", "English")
    assert "\n" not in result


# ---- get_mode_choice_keywords / get_language_choice_keywords -------------


@pytest.mark.parametrize("language", list(all_entries().keys()))
def test_mode_choice_keywords_union_with_english_for_every_language(language):
    keywords = get_mode_choice_keywords(language)
    assert "voice" in keywords
    assert "text" in keywords
    lowered_voice = [k.lower() for k in keywords["voice"]]
    lowered_text = [k.lower() for k in keywords["text"]]
    # English's own universal keywords always work, regardless of the
    # session's resolved language (AC-4).
    assert "voice" in lowered_voice
    assert "text" in lowered_text
    assert "type" in lowered_text


def test_mode_choice_keywords_for_unrecognized_language_is_english_only():
    keywords = get_mode_choice_keywords("Klingon")
    assert keywords == get_mode_choice_keywords("English")


def test_language_choice_keywords_covers_every_language_in_declared_order():
    ordered = get_language_choice_keywords()
    languages_in_order = [language for language, _ in ordered]
    assert languages_in_order == list(all_entries().keys())
    assert languages_in_order[0] == "English"


def test_language_choice_keywords_hindi_matches_native_script_and_romanization():
    ordered = dict(get_language_choice_keywords())
    hindi_keywords = [kw.lower() for kw in ordered["Hindi"]]
    assert "हिंदी" in hindi_keywords or "हिन्दी" in hindi_keywords
    assert "hindi" in hindi_keywords
