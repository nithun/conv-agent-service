"""CR-012 AC-1 regression guard (code-review finding
`missing_acceptance_criteria_test`, 4th recurrence of the L-021 pattern —
`.claude/memory/code-review-findings.jsonl`, review
`2026-07-28-tap-agent-service-cr-012`).

CR-012 promoted the self-hosted LiteLLM gateway from a documented
"LOCAL DEV ONLY" / "stopgap" substitute to the real, documented production
LLM transport for `tap_buddy_turn` (`docs/architecture.md` Sec5 2026-07-28
entry in the parent workspace). AC-1 requires that no surviving doc or
code comment frames it the old way. Before this test, AC-1's state was
verified only by an ad-hoc grep + commit-message prose (commit `3423410`)
-- nothing committed would catch a silent regression (e.g. someone
reverting a comment, or copy-pasting the old framing into a new file).

This test mechanically scans the same files CR-012 touched for the two
defect phrases ("LOCAL DEV ONLY" / "stopgap", case- and
spacing/hyphenation-insensitive) used anywhere near a LiteLLM mention, and
fails unless each occurrence sits within a negation/historical context
(e.g. "not a ... stopgap", "no longer means dev-only", "removes every
remaining stopgap/dev-only label"). It is intentionally scoped to
occurrences near "litellm" -- `app/config/settings.py` legitimately uses
"local-dev-only" to describe the UNRELATED `GLIFIC_HOST_HEADER_OVERRIDE`
Docker workaround (F-021), which is genuinely local-dev-only and has
nothing to do with the LiteLLM transport-status claim this AC governs; a
naive whole-file grep would misfire on that line.

The negation-cue list and the 400-char LiteLLM-proximity window were
tuned against this repo's ACTUAL current text (read via `git show` across
the CR-012 history, both the original 2026-07-08 "LOCAL DEV ONLY"/F-020
defect wording and the corrected 2026-07-28 wording) so this test passes
now and is verified (via the hand construction of this test) to fail
against the original pre-fix framing -- see this commit's message for the
literal defect strings it was checked against.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.parent

# The exact set of files CR-012 identified as needing the wording sweep
# (CR-012 frontmatter `affects:` + Proposed behavior #1).
TARGET_FILES: tuple[str, ...] = (
    "docker-compose.yml",
    "litellm.config.yaml",
    "app/llm/routes.py",
    "app/llm/router.py",
    "app/config/settings.py",
    "CLAUDE.md",
)

# The defect phrasing this CR fixed: "LOCAL DEV ONLY" (any case, with
# spaces or hyphens between the words) and bare "stopgap".
_BAD_PATTERN = re.compile(r"local[\s\-]*dev[\s\-]*only|stopgap", re.IGNORECASE)

# Only occurrences near an actual LiteLLM mention are in AC-1's scope --
# this file also legitimately says "local-dev-only" about an unrelated
# feature (GLIFIC_HOST_HEADER_OVERRIDE, F-021).
_LITELLM_PATTERN = re.compile(r"litellm", re.IGNORECASE)
_LITELLM_PROXIMITY_CHARS = 400

# Cue substrings (lowercased) that, if present in the text immediately
# preceding a match, mark it as a NEGATED or HISTORICAL reference rather
# than a live "this is dev-only / a stopgap" claim. Read off this repo's
# actual current wording (docker-compose.yml, litellm.config.yaml,
# CLAUDE.md, app/config/settings.py) plus the generic forms a reviewer
# would reach for.
_NEGATION_CUES: tuple[str, ...] = (
    "not a",
    'not"',
    "not the",
    "n't",
    "no longer",
    "never",
    "remov",  # "removes every remaining stopgap/dev-only label"
    "falsified",  # "that framing was already falsified by AD-004"
    "supersed",
)
_NEGATION_WINDOW_CHARS = 160


def _iter_unqualified_matches(text: str) -> list[str]:
    """Returns a human-readable context snippet for every match of the
    defect phrasing that is (a) near a LiteLLM mention and (b) not
    preceded by a negation/historical cue within `_NEGATION_WINDOW_CHARS`.
    Empty list means the file is clean.
    """
    normalized = re.sub(r"\s+", " ", text)
    violations: list[str] = []
    for match in _BAD_PATTERN.finditer(normalized):
        start, end = match.start(), match.end()
        proximity_text = normalized[
            max(0, start - _LITELLM_PROXIMITY_CHARS) : end + _LITELLM_PROXIMITY_CHARS
        ]
        if not _LITELLM_PATTERN.search(proximity_text):
            continue  # not about LiteLLM at all -- out of AC-1's scope
        window_text = normalized[max(0, start - _NEGATION_WINDOW_CHARS) : start].lower()
        if any(cue in window_text for cue in _NEGATION_CUES):
            continue  # negated/historical -- fine
        context = normalized[max(0, start - 60) : end + 40]
        violations.append(f"{match.group()!r} :: ...{context}...")
    return violations


def test_target_files_exist() -> None:
    """Sanity guard: if CR-012's affected-file list drifts (a file gets
    renamed/removed), this test should fail loudly rather than silently
    scanning zero files and reporting a false-clean AC-1.
    """
    missing = [f for f in TARGET_FILES if not (REPO_ROOT / f).exists()]
    assert not missing, f"CR-012 target files missing from repo: {missing}"


def test_no_unqualified_local_dev_only_or_stopgap_framing_of_litellm() -> None:
    """CR-012 AC-1: no doc or code comment frames the LiteLLM gateway as
    "LOCAL DEV ONLY" or a "stopgap" while it is the documented real
    production transport. Fails with the offending file + snippet if a
    live (non-negated, non-historical) occurrence is found near a
    "litellm" mention in any of CR-012's target files.
    """
    all_violations: list[str] = []
    for rel_path in TARGET_FILES:
        path = REPO_ROOT / rel_path
        text = path.read_text(encoding="utf-8")
        for violation in _iter_unqualified_matches(text):
            all_violations.append(f"{rel_path}: {violation}")

    assert not all_violations, (
        "Found stale 'LOCAL DEV ONLY' / 'stopgap' framing of the LiteLLM "
        "gateway (CR-012 AC-1 regression -- it is now the documented real "
        "production transport, docs/architecture.md Sec5 2026-07-28):\n"
        + "\n".join(all_violations)
    )


def test_detector_actually_flags_the_original_pre_cr012_wording() -> None:
    """Guards against this test silently becoming a no-op (e.g. an overly
    narrow pattern or an overly generous negation-cue list that would
    never fail). Runs the same scanner against the literal pre-CR-012
    defect text this repo actually had (commit `89b42c3`, corrected by
    `1f6c34e` and then `3423410`) and asserts it IS flagged.
    """
    original_docker_compose_comment = (
        "  # Self-hosted LiteLLM proxy (open source) - LOCAL DEV ONLY (F-020). An\n"
        "  # OpenAI-compatible gateway fronting Vertex AI with the operator's\n"
        "  # existing GCP service account, so a REAL model call can run + be traced\n"
        "  # without an OpenRouter account."
    )
    original_litellm_config_comment = (
        "# LiteLLM proxy config - LOCAL DEV ONLY (compose service `litellm`).\n"
        "#\n"
        "# Purpose (F-020): give the default OpenAI-compatible provider adapter a\n"
        "# self-hosted, open-source gateway to call."
    )
    hypothetical_stopgap_regression = (
        "# The litellm gateway is a stopgap until a native adapter ships."
    )

    assert _iter_unqualified_matches(original_docker_compose_comment), (
        "scanner failed to flag the real pre-CR-012 docker-compose.yml "
        "'LOCAL DEV ONLY' wording -- detector is not a real regression guard"
    )
    assert _iter_unqualified_matches(original_litellm_config_comment), (
        "scanner failed to flag the real pre-CR-012 litellm.config.yaml "
        "'LOCAL DEV ONLY' wording -- detector is not a real regression guard"
    )
    assert _iter_unqualified_matches(hypothetical_stopgap_regression), (
        "scanner failed to flag an unqualified 'stopgap' claim near a "
        "litellm mention -- detector is not a real regression guard"
    )


def test_unrelated_local_dev_only_mentions_are_not_flagged() -> None:
    """`app/config/settings.py` legitimately describes
    `GLIFIC_HOST_HEADER_OVERRIDE` as "local-dev-only" (F-021) -- that
    workaround genuinely IS local-dev only and is unrelated to the
    LiteLLM transport-status claim CR-012 fixes. Confirms the LiteLLM-
    proximity scoping keeps this test from misfiring on it.
    """
    unrelated_text = (
        "# GLIFIC_HOST_HEADER_OVERRIDE: local-dev-only Docker workaround "
        "(F-021, macOS host.docker.internal quirk)."
    )
    assert not _iter_unqualified_matches(unrelated_text), (
        "scanner incorrectly flagged a 'local-dev-only' mention with no "
        "nearby LiteLLM reference -- proximity scoping regressed"
    )
