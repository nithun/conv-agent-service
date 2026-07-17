"""CR-007 AC-2/AC-3: static reachability / no-hardcoded-English-copy check.

Mirrors P-005's AST/grep-fallback reachability idiom
(tests/core/test_isolation.py, tests/worker/test_turn_processor.py's BR-004
call-graph check). Cross-cutting across app/worker and app/core, so it
lives at the top level rather than under tests/config (the loader itself)
or tests/worker (one call site among several) — it's the acceptance test
for the CALL-SITE MIGRATION (turn_processor.py/turn_logic.py/
distress_gate.py), not for scripted_replies.py/.yaml themselves.
"""

from __future__ import annotations

from pathlib import Path

from app.config.scripted_replies import SCRIPTED_REPLY_KEYS, get_scripted_reply

APP_ROOT = Path(__file__).parent.parent / "app"

_SCANNED_MODULES = [
    APP_ROOT / "worker" / "turn_processor.py",
    APP_ROOT / "core" / "turn_logic.py",
    APP_ROOT / "worker" / "distress_gate.py",
]

# The four pre-identity-resolution strings (CR-007 Scope of change) that
# MUST remain literal Python strings in turn_processor.py — the check below
# must not flag these as a "missing scripted_replies migration".
_PRE_IDENTITY_LITERALS = [
    "Hi! Before we start, what's your name?",
    "Which one is you?",
    "Can you check the spelling?",
    "Sorry, I didn't recognize that name — can you try again?",
]


def test_no_english_scripted_reply_content_survives_as_a_literal_outside_config():
    """AC-2: for each of the 10 language-eligible keys, the ENGLISH value in
    scripted_replies.yaml must not appear verbatim as a literal string in
    turn_processor.py/turn_logic.py/distress_gate.py — it must be reached
    only via get_scripted_reply().
    """
    english_values = [get_scripted_reply(key, "English") for key in SCRIPTED_REPLY_KEYS]
    for path in _SCANNED_MODULES:
        source = path.read_text(encoding="utf-8")
        for value in english_values:
            assert value not in source, (
                f"{path.name} still contains a literal copy of scripted reply "
                f"content ({value[:40]!r}...) — must be sourced via get_scripted_reply()"
            )


def test_get_scripted_reply_is_actually_called_in_every_scanned_module():
    for path in _SCANNED_MODULES:
        source = path.read_text(encoding="utf-8")
        assert "get_scripted_reply(" in source, f"{path.name} never calls get_scripted_reply()"


def test_pre_identity_literals_remain_and_are_not_false_flagged():
    """AC-3: the four pre-identity strings are correctly EXCLUDED from the
    "must be scripted" set — this test proves they're still literal Python
    strings (not migrated), which the AC-2 check above does not (and must
    not) flag, since none of them match a SCRIPTED_REPLY_KEYS English value.
    """
    turn_processor_source = (APP_ROOT / "worker" / "turn_processor.py").read_text(
        encoding="utf-8"
    )
    for literal in _PRE_IDENTITY_LITERALS:
        assert literal in turn_processor_source, (
            f"pre-identity literal {literal!r} unexpectedly missing from turn_processor.py"
        )
    english_values = {get_scripted_reply(key, "English") for key in SCRIPTED_REPLY_KEYS}
    assert not (set(_PRE_IDENTITY_LITERALS) & english_values), (
        "a pre-identity literal collides with a scripted-reply English value — "
        "the AC-2 check above would false-positive on it"
    )
