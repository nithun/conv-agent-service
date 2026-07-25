"""CR-010 task 8: `.env.example` documents `GLIFIC_CONTACT_ALLOWLIST`
covering (a) the double-gated local-dev simulator-pool fallback, (b) the
non-local boot-failure behavior, (c) contact_id-preferred / exact-phone-
match-no-normalization, and (d) the F-057 test-number-is-entry-#1 note —
and commits no literal contact id/phone value.
"""

from __future__ import annotations

from pathlib import Path

ENV_EXAMPLE_PATH = Path(__file__).parent.parent.parent / ".env.example"


def _read_env_example() -> str:
    return ENV_EXAMPLE_PATH.read_text()


def _contact_allowlist_section(content: str) -> str:
    """Slices the file between the section's own header comment and the
    next section header — avoids a fixed-character-offset window (fragile
    against wording edits) while still scoping assertions to THIS section
    rather than the whole file.
    """
    start = content.index("--- Contact allowlist")
    end = content.index("--- Self-hosted LiteLLM", start)
    return content[start:end]


def test_env_example_declares_glific_contact_allowlist_var() -> None:
    content = _read_env_example()
    assert "GLIFIC_CONTACT_ALLOWLIST=" in content


def test_env_example_glific_contact_allowlist_var_line_is_empty_no_committed_value() -> None:
    """Structural check (CR-010 task 8 AC): the actual variable-assignment
    LINE carries no literal contact id/phone digit string — comments above
    it may use illustrative example digits to explain the format, but the
    committed value itself must be empty, exactly like every other
    credential/config field in this file.
    """
    content = _read_env_example()
    lines = [line for line in content.splitlines() if line.startswith("GLIFIC_CONTACT_ALLOWLIST=")]
    assert len(lines) == 1, "expected exactly one GLIFIC_CONTACT_ALLOWLIST= assignment line"
    assert lines[0] == "GLIFIC_CONTACT_ALLOWLIST="


def test_env_example_covers_the_double_gated_local_fallback() -> None:
    content = _read_env_example()
    assert "ENVIRONMENT=local" in content
    assert "GLIFIC_API_URL" in content
    assert "localhost/127.0.0.1/host.docker.internal" in content or (
        "localhost" in content and "127.0.0.1" in content and "host.docker.internal" in content
    )


def test_env_example_covers_non_local_boot_failure_behavior() -> None:
    section = _contact_allowlist_section(_read_env_example())
    assert "REFUSE TO BOOT" in section or "refuses" in section.lower() or "boot" in section.lower()


def test_env_example_covers_contact_id_preferred_and_no_phone_normalization() -> None:
    section = _contact_allowlist_section(_read_env_example())
    assert "PREFERRED" in section
    assert "no further" in section.lower() or "no normalization" in section.lower()


def test_env_example_covers_f057_test_number_as_entry_one() -> None:
    section = _contact_allowlist_section(_read_env_example())
    assert "F-057" in section
    assert "entry #1" in section or "entry number 1" in section.lower()
