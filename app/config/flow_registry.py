"""Loader for `flow_registry.yaml` — the config-driven catalog of Glific
flows the conv-AI agent may agent-initiate via the `start_glific_flow` tool
(CR-014 / ADR-015). Mirrors `app/config/language_map.py`'s
`yaml.safe_load`/dataclass shape.

**Keying decision (CR-014 Proposed behavior point 2): entries are keyed by
stable NAME, never the mutable integer `flow_id`.** The LLM's tool argument
and every `when_to_use` cross-reference use `flow_name`; `flow_id` is the
CURRENT resolved value, refreshed by whoever maintains the file whenever the
flow is re-imported/republished (same manual-refresh discipline
`GLIFIC_TURN_FLOW_ID` already requires — L-013 class).

**BC-3 (ADR-015, structural no-loopback guard) — the load-bearing safety
check in this module.** `load_flow_registry` REJECTS — a boot-time
`RuntimeError`, this project's fail-closed discipline, never a silent skip
— any entry whose `flow_id` equals the turn-relay flow id
(`Settings.glific_turn_flow_id`). Registering the conv-AI relay flow itself
as an "orchestrated flow" would create an undefined recursion/ownership
case: this service could receive its own orchestrated flow's webhook call
while still inside the turn that started it (CR-014 "Registry admission
constraint"). This check runs independently of
`Settings.flow_orchestrator_enabled` — a broken registry must fail loudly
at boot even while the capability itself is flag-disabled, so a bad file
never lands undetected.

Empty/absent registry file resolves to an EMPTY `FlowRegistry` (graceful,
not an error) — CR-014 AC-1's "no registry -> tool never offered" structural
kill switch; `FlowRegistry.__bool__`/`.names()` both read empty in that
case, so `_tools_for_state` (`app/core/turn_logic.py`) omits the tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = Path(__file__).parent / "flow_registry.yaml"


@dataclass(frozen=True)
class FlowRegistryEntry:
    name: str
    flow_id: str
    when_to_use: str


@dataclass(frozen=True)
class FlowRegistry:
    """An immutable, boot-validated catalog keyed by stable flow NAME."""

    entries: dict[str, FlowRegistryEntry]

    def names(self) -> list[str]:
        return list(self.entries.keys())

    def resolve(self, name: str) -> FlowRegistryEntry | None:
        return self.entries.get(name)

    def __bool__(self) -> bool:
        return bool(self.entries)


def _parse_entries(raw: dict[str, Any]) -> dict[str, FlowRegistryEntry]:
    entries: dict[str, FlowRegistryEntry] = {}
    for name, body in (raw or {}).items():
        entries[name] = FlowRegistryEntry(
            name=name,
            flow_id=str(body["flow_id"]),
            when_to_use=str(body.get("when_to_use") or "").strip(),
        )
    return entries


def load_flow_registry(*, path: Path | None = None, turn_flow_id: str = "") -> FlowRegistry:
    """Loads and validates the flow registry.

    - Missing file -> empty `FlowRegistry` (graceful; AC-1's structural
      disable — the tool is then never offered).
    - Present file with an entry whose `flow_id == turn_flow_id` -> raises
      `RuntimeError` at load time (BC-3/AC-6 — a boot-time failure, not a
      silent skip). Only checked when `turn_flow_id` is truthy — an unset
      turn-relay id has nothing to collide with.
    - `path` overrides the default `app/config/flow_registry.yaml` location
      (tests only).
    """
    config_path = path or _CONFIG_PATH
    if not config_path.is_file():
        return FlowRegistry(entries={})

    with open(config_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    entries = _parse_entries(raw)

    if turn_flow_id:
        colliding = sorted(
            entry.name for entry in entries.values() if entry.flow_id == turn_flow_id
        )
        if colliding:
            raise RuntimeError(
                f"flow_registry.yaml entries {colliding!r} use flow_id={turn_flow_id!r}, "
                "which is Settings.glific_turn_flow_id (the conv-AI turn-relay flow "
                "itself) -- registering the relay flow as an agent-orchestrated flow "
                "would create an undefined recursion/ownership case (ADR-015 BC-3). "
                "Fix flow_registry.yaml before booting."
            )

    return FlowRegistry(entries=entries)
