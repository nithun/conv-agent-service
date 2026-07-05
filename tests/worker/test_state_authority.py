"""AC-8/Invariant 1 (state authority, L-001): no code path in the ported
worker/core logic writes to Redis anything that could be mistaken for
progress/enrollment state — Frappe's `save_submission` remains the ONLY
write of consequence; Redis session state is conversational only.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).parent.parent.parent / "app"


def _redis_write_call_names(path: Path) -> set[str]:
    """Finds every `.hset(`/`.set(`/`.xadd(` style call in a module's AST
    (a coarse but effective proxy for "this module writes to Redis") and
    returns the attribute names used, so we can inspect what's actually
    being written.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"hset", "set", "xadd", "hmset"}:
                names.add(node.func.attr)
    return names


def test_session_store_is_the_only_module_writing_redis_hashes() -> None:
    """`app/worker/session_store.py` is the sole owner of session-state
    Redis writes (hset) — no other module in app/worker or app/core
    duplicates or bypasses it with its own hash write.
    """
    worker_files = list((APP_ROOT / "worker").glob("*.py"))
    core_files = list((APP_ROOT / "core").glob("*.py"))

    violations = []
    for path in worker_files + core_files:
        if path.name == "session_store.py":
            continue
        if "hset" in _redis_write_call_names(path):
            violations.append(str(path))

    assert violations == [], (
        f"unexpected direct Redis hash writes outside session_store.py: {violations}"
    )


def test_save_submission_is_the_only_lms_write_call() -> None:
    """`app/tools/lms_client.py`'s public surface has exactly one write
    method (`save_submission`) — everything else is a read. Mirrors
    tap-buddy's `test_client_has_no_other_methods` intent for this port.
    """
    from app.tools.lms_client import TapLmsClient

    public_methods = {
        name
        for name in dir(TapLmsClient)
        if not name.startswith("_") and callable(getattr(TapLmsClient, name))
    }
    write_shaped_names = {
        m for m in public_methods if m.startswith(("save_", "update_", "create_", "delete_"))
    }
    assert write_shaped_names == {"save_submission"}


def test_session_data_fields_are_conversational_not_enrollment_shaped() -> None:
    """A textual/structural guard: SessionData's field names must not
    include any enrollment/progress-authoritative-sounding field
    (grade progression counters, enrollment status, curriculum position as
    a standalone authoritative value) — those stay in Frappe.
    """
    import dataclasses

    from app.core.state_machine import SessionData

    field_names = {f.name for f in dataclasses.fields(SessionData)}
    forbidden = {"enrollment_status", "grade_progression", "curriculum_position", "program_state"}
    assert not (field_names & forbidden)
