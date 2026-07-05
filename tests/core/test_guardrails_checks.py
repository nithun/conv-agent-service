"""Guardrail check acceptance tests: each validator gets a violating +
passing case; distress detector covers each category (abuse, self-harm,
fear, sickness, safety) with English + Hindi + the other 8 supported-
language positive/negative examples.

Re-targeted from tap-buddy/tests/test_guardrails.py against
app.core.guardrails_checks (CR-004 Task 11). BR-002's all-10-language
distress coverage ports forward unmodified (Invariant 4) — this file
re-asserts it against the new module, it doesn't re-derive it.

AC-5/F-011: `check_no_answer_leakage` no longer accepts a `correct_option`
parameter — this port removes the dead param + its overstated docstring
claim rather than wiring the now-confirmed-live value (Nithun's decision,
2026-07-05, decisions.jsonl).
"""

from __future__ import annotations

from app.core.guardrails_checks import (
    ValidationFailure,
    check_length,
    check_no_answer_leakage,
    check_no_fabricated_content,
    check_single_question,
    detect_distress,
    detect_doubt_signal,
    detect_off_turn_signal,
    run_guardrails,
)

# ---- length -------------------------------------------------------------


def test_length_violating_too_many_sentences():
    reply = "This is one. This is two. This is three. This is four."
    assert check_length(reply) == ValidationFailure.TOO_LONG


def test_length_passing_within_three_sentences():
    reply = "This is one. This is two. This is three?"
    assert check_length(reply) is None


# ---- single question ------------------------------------------------------


def test_single_question_violating_two_questions():
    reply = "How are you? Are you ready?"
    assert check_single_question(reply) == ValidationFailure.MULTIPLE_QUESTIONS


def test_single_question_passing_one_question():
    reply = "How are you feeling today?"
    assert check_single_question(reply) is None


def test_single_question_passing_zero_questions_allowed():
    assert check_single_question("Great job today!") is None


# ---- answer leakage (F-011: no correct_option param anymore) --------------


def test_answer_leakage_check_takes_only_reply_no_correct_option_param():
    """AC-5/F-011: the parameter and its overstated docstring claim are both
    gone — asserted by signature inspection, not by a real value firing it.
    """
    import inspect

    sig = inspect.signature(check_no_answer_leakage)
    assert list(sig.parameters) == ["reply"], (
        "check_no_answer_leakage must accept only `reply` — F-011's dead "
        "`correct_option` parameter must not be re-introduced"
    )


def test_check_no_answer_leakage_has_no_correct_option_param():
    """Duplicate-named test matching CR-004's test plan literal name, so the
    acceptance-criteria mapping is unambiguous for the 360 review.
    """
    import inspect

    sig = inspect.signature(check_no_answer_leakage)
    assert "correct_option" not in sig.parameters
    docstring = check_no_answer_leakage.__doc__ or ""
    assert "belt-and-suspenders" not in docstring.lower()


def test_answer_leakage_check_never_fails_today_type_level_guard_only():
    """Honest current behavior: with no correct_option to check against,
    this validator always passes — the type-level guard
    (QuizQuestionDetails carrying no correct_option field) is the sole,
    honestly-documented protection, not this check.
    """
    assert check_no_answer_leakage("The answer is 3/4, well done!") is None
    assert check_no_answer_leakage("Try finding a common denominator first.") is None


# ---- fabricated content ------------------------------------------------


def test_fabricated_content_violating():
    reply = "You already completed [[bonus_video]], great job!"
    assert (
        check_no_fabricated_content(reply, allowed_content_fields={"video"})
        == ValidationFailure.FABRICATED_CONTENT
    )


def test_fabricated_content_passing_field_in_scope():
    reply = "Let's start [[video]] now."
    assert check_no_fabricated_content(reply, allowed_content_fields={"video"}) is None


def test_fabricated_content_passing_no_markers():
    reply = "Let's start today's video now."
    assert check_no_fabricated_content(reply, allowed_content_fields=set()) is None


# ---- run_guardrails aggregate ---------------------------------------------


def test_run_guardrails_ok_on_clean_reply():
    result = run_guardrails("How are you feeling today?")
    assert result.ok is True
    assert result.failures == []


def test_run_guardrails_blocks_violating_reply():
    reply = "One. Two. Three. Four?"
    result = run_guardrails(reply)
    assert result.ok is False
    assert ValidationFailure.TOO_LONG in result.failures


# ---- distress detector: English -------------------------------------------


def test_distress_english_abuse_positive():
    assert detect_distress("my dad keeps hitting me", "English") is True


def test_distress_english_abuse_negative():
    assert detect_distress("I hit the wrong button", "English") is False


def test_distress_english_self_harm_positive():
    assert detect_distress("I want to kill myself", "English") is True


def test_distress_english_self_harm_negative():
    assert detect_distress("this quiz is killing me lol", "English") is False


def test_distress_english_fear_positive():
    assert detect_distress("I am so scared right now", "English") is True


def test_distress_english_fear_negative():
    assert detect_distress("that was a scary movie", "English") is False


def test_distress_english_sickness_positive():
    assert detect_distress("I feel very ill and can't breathe", "English") is True


def test_distress_english_sickness_negative():
    assert detect_distress("I feel a bit tired today", "English") is False


def test_distress_english_safety_positive():
    assert detect_distress("I don't feel safe at home right now", "English") is True


def test_distress_english_safety_negative():
    assert detect_distress("my network is bad today", "English") is False


# ---- distress detector: Hindi (proves not English-only) -------------------


def test_distress_hindi_abuse_positive():
    assert detect_distress("mujhe maar rahe hain", "Hindi") is True


def test_distress_hindi_abuse_negative():
    assert detect_distress("aaj mausam accha hai", "Hindi") is False


def test_distress_hindi_self_harm_positive():
    assert detect_distress("main marna chahta hoon", "Hindi") is True


def test_distress_hindi_self_harm_negative():
    assert detect_distress("mujhe homework karna hai", "Hindi") is False


def test_distress_hindi_fear_positive():
    assert detect_distress("mujhe dar lag raha hai", "Hindi") is True


def test_distress_hindi_fear_negative():
    assert detect_distress("yeh video accha hai", "Hindi") is False


def test_distress_hindi_sickness_positive():
    assert detect_distress("main bahut bimar hoon", "Hindi") is True


def test_distress_hindi_sickness_negative():
    assert detect_distress("main theek hoon", "Hindi") is False


def test_distress_hindi_safety_positive():
    assert detect_distress("main surakshit nahi hoon", "Hindi") is True


def test_distress_hindi_safety_negative():
    assert detect_distress("chalo lesson shuru karte hain", "Hindi") is False


def test_distress_unrecognized_language_falls_back_to_english_patterns():
    assert detect_distress("I want to kill myself", "Klingon") is True


def test_distress_unrecognized_language_also_falls_back_to_hindi_patterns():
    assert detect_distress("mujhe maar rahe hain", "Klingon") is True


def test_distress_unrecognized_language_logs_a_warning(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="app.config.distress_keywords")
    detect_distress("hello there", "Klingon")
    assert any("no dedicated pattern list" in record.getMessage() for record in caplog.records), (
        "expected a warning when falling back for an unmapped language"
    )


# ---- distress detector: remaining fully-supported languages ---------------


def test_distress_bengali_abuse_positive():
    assert detect_distress("amake mar dhe", "Bengali") is True


def test_distress_bengali_self_harm_positive():
    assert detect_distress("more jete chai", "Bengali") is True


def test_distress_bengali_negative():
    assert detect_distress("aj abohaowa bhalo", "Bengali") is False


def test_distress_tamil_fear_positive():
    assert detect_distress("bayama irukku", "Tamil") is True


def test_distress_tamil_negative():
    assert detect_distress("indha padam nalla irukku", "Tamil") is False


def test_distress_telugu_sickness_positive():
    assert detect_distress("chala anaarogyanga undi", "Telugu") is True


def test_distress_telugu_negative():
    assert detect_distress("ee video baagundi", "Telugu") is False


def test_distress_marathi_safety_positive():
    assert detect_distress("mala surakshit nahi vatat", "Marathi") is True


def test_distress_marathi_negative():
    assert detect_distress("aaj shala changla zala", "Marathi") is False


def test_distress_gujarati_fear_positive():
    assert detect_distress("mane dar lage chhe", "Gujarati") is True


def test_distress_gujarati_negative():
    assert detect_distress("aaje saras divas chhe", "Gujarati") is False


def test_distress_kannada_abuse_positive():
    assert detect_distress("nannannu hodedaru nirantara", "Kannada") is True


def test_distress_kannada_negative():
    assert detect_distress("ee pathya chennagide", "Kannada") is False


def test_distress_malayalam_self_harm_positive():
    assert detect_distress("marikkanam ennu thonnunnu", "Malayalam") is True


def test_distress_malayalam_negative():
    assert detect_distress("ee classundu nannayirunnu", "Malayalam") is False


def test_distress_urdu_sickness_positive():
    assert detect_distress("mujhe bahut bimar mehsoos ho raha hai", "Urdu") is True


def test_distress_urdu_negative():
    assert detect_distress("aaj mausam acha hai", "Urdu") is False


def test_distress_punjabi_uses_fallback_and_still_catches_hindi_signal():
    assert detect_distress("mujhe maar rahe hain", "Punjabi") is True


def test_every_fully_supported_language_has_dedicated_distress_coverage():
    """Regression guard: every language_map.yaml entry marked
    supported: true (STT+TTS) must have its own distress_keywords.yaml
    list — carries BR-002's fix forward unmodified.
    """
    from app.config.distress_keywords import supported_languages
    from app.config.language_map import all_entries

    fully_supported = {name for name, entry in all_entries().items() if entry.supported}
    covered = set(supported_languages())
    missing = fully_supported - covered
    assert missing == set(), (
        f"languages marked fully-supported but missing distress coverage: {missing}"
    )


# ---- doubt/confusion signal detector ---------------------------------------


def test_doubt_signal_positive_i_dont_understand():
    assert detect_doubt_signal("I don't understand this at all") is True


def test_doubt_signal_positive_confused():
    assert detect_doubt_signal("I'm so confused right now") is True


def test_doubt_signal_positive_explain_again():
    assert detect_doubt_signal("can you explain again please") is True


def test_doubt_signal_positive_tolerates_interposed_words():
    assert detect_doubt_signal("I still really don't understand") is True


def test_doubt_signal_negative_ordinary_reply():
    assert detect_doubt_signal("ok got it, thanks") is False


def test_doubt_signal_negative_unrelated_question():
    assert detect_doubt_signal("what happens next?") is False


def test_doubt_signal_empty_text_is_false():
    assert detect_doubt_signal("") is False


# ---- off-turn / unrelated-input signal detector ----------------------------


def test_off_turn_signal_positive_network_complaint():
    assert detect_off_turn_signal("sorry my network is bad") is True


def test_off_turn_signal_positive_who_is_this():
    assert detect_off_turn_signal("who is this?") is True


def test_off_turn_signal_positive_are_you_a_bot():
    assert detect_off_turn_signal("wait, are you a bot?") is True


def test_off_turn_signal_negative_ordinary_lesson_reply():
    assert detect_off_turn_signal("I finished the video") is False


def test_off_turn_signal_negative_doubt_signal_is_not_off_turn():
    assert detect_off_turn_signal("I don't understand this part") is False


def test_off_turn_signal_empty_text_is_false():
    assert detect_off_turn_signal("") is False
