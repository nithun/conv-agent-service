"""CR-014/ADR-015: the config-driven flow registry loader.

Mirrors `app/config/language_map.py`'s loader-test convention. Covers
AC-1 (empty/missing file resolves to a structurally-empty registry) and
AC-6/BC-3 (boot-time rejection of a registry entry whose `flow_id` collides
with the turn-relay flow id).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config.flow_registry import FlowRegistry, load_flow_registry


def _write_registry(tmp_path: Path, contents: dict) -> Path:
    path = tmp_path / "flow_registry.yaml"
    path.write_text(yaml.safe_dump(contents), encoding="utf-8")
    return path


def test_loads_a_valid_registry_keyed_by_name(tmp_path: Path) -> None:
    contents = {
        "TAP Buddy": {"flow_id": "25", "when_to_use": "Voice-first handoff."},
        "Registration Workflow": {"flow_id": "6", "when_to_use": "Not a recognized student."},
    }
    path = _write_registry(tmp_path, contents)

    registry = load_flow_registry(path=path)

    assert isinstance(registry, FlowRegistry)
    assert set(registry.names()) == {"TAP Buddy", "Registration Workflow"}
    entry = registry.resolve("TAP Buddy")
    assert entry is not None
    assert entry.flow_id == "25"
    assert entry.when_to_use == "Voice-first handoff."


def test_missing_file_resolves_to_empty_registry_not_an_error(tmp_path: Path) -> None:
    missing_path = tmp_path / "does-not-exist.yaml"

    registry = load_flow_registry(path=missing_path)

    assert registry.names() == []
    assert bool(registry) is False
    assert registry.resolve("anything") is None


def test_empty_file_resolves_to_empty_registry(tmp_path: Path) -> None:
    path = tmp_path / "flow_registry.yaml"
    path.write_text("", encoding="utf-8")

    registry = load_flow_registry(path=path)

    assert registry.names() == []
    assert bool(registry) is False


def test_resolve_returns_none_for_an_unregistered_name(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, {"TAP Buddy": {"flow_id": "25", "when_to_use": "x"}})

    registry = load_flow_registry(path=path)

    assert registry.resolve("Not A Real Flow") is None


# ---- BC-3/AC-6: structural no-loopback boot-time rejection -----------------


def test_rejects_at_load_time_when_an_entry_collides_with_the_turn_relay_flow_id(
    tmp_path: Path,
) -> None:
    """The registry loader must FAIL AT BOOT (RuntimeError), not silently
    skip, an entry whose flow_id equals the turn-relay flow id — starting
    the conv-AI relay flow itself as an "orchestrated flow" would be a
    self-loop (ADR-015 BC-3).
    """
    path = _write_registry(
        tmp_path,
        {
            "TAP Buddy": {"flow_id": "25", "when_to_use": "x"},
            "Accidental Relay Loop": {"flow_id": "80", "when_to_use": "oops"},
        },
    )

    with pytest.raises(RuntimeError, match="Accidental Relay Loop"):
        load_flow_registry(path=path, turn_flow_id="80")


def test_no_collision_check_when_turn_flow_id_is_unset(tmp_path: Path) -> None:
    """An unset/empty turn_flow_id has nothing to collide with — must not
    raise (mirrors GLIFIC_TURN_FLOW_ID's own empty-by-default local dev
    state, where this check is a structural no-op).
    """
    path = _write_registry(tmp_path, {"TAP Buddy": {"flow_id": "25", "when_to_use": "x"}})

    registry = load_flow_registry(path=path, turn_flow_id="")

    assert registry.names() == ["TAP Buddy"]


def test_no_collision_when_ids_simply_differ(tmp_path: Path) -> None:
    path = _write_registry(tmp_path, {"TAP Buddy": {"flow_id": "25", "when_to_use": "x"}})

    registry = load_flow_registry(path=path, turn_flow_id="80")

    assert registry.names() == ["TAP Buddy"]


def test_the_real_shipped_flow_registry_yaml_loads_and_passes_bc3() -> None:
    """The actual `app/config/flow_registry.yaml` shipped is registry v1
    (AD-006 §2/§7, 2026-07-30): exactly one entry, TAP Feedback (flow 95,
    BC-1-verified). The 2026-07-29 placeholders (TAP Buddy 25, Registration
    Workflow 6) were removed per AD-006 — their reappearance here would be
    a regression against that decision, not an addition.

    The pin was 94 until 2026-08-04; glific-platform's post-flow-distress
    fix re-imported the flow and the numeric id moved 94 -> 95 (uuid anchor
    02cbcbec-… re-resolved against live glific_dev; old 94 no longer exists).
    This assertion is the staleness tripwire — when it fails after a
    re-import, re-resolve from the uuid, don't just bump the number.
    """
    registry = load_flow_registry(turn_flow_id="80")

    assert set(registry.names()) == {"TAP Feedback"}
    entry = registry.resolve("TAP Feedback")
    assert entry is not None and entry.flow_id == "95"
    for e in registry.entries.values():
        assert e.when_to_use  # AC-8: real authored text, never blank
