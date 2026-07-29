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


def test_lms_client_write_methods_are_the_known_whitelisted_set() -> None:
    """`app/tools/lms_client.py`'s public surface has exactly THREE write
    methods: `save_submission` (state authority's sole progress write,
    unchanged), and CR-011/ADR-013's two newly-whitelisted test-flow writes
    `register_test_student`/`reset_test_student` (2026-07-27, F-071 reply —
    existing, live-verified Frappe endpoints registered in AD-002's
    VERIFIED external-contracts rows, never an agent-invented state, L-001).

    A naming-heuristic prefix check alone (this test's pre-CR-011 shape)
    would silently MISS both new methods — neither starts with
    save_/update_/create_/delete_ — so this test enumerates the known write
    set explicitly instead: a REAL new write method (anything outside this
    set) fails this assertion loudly rather than sneaking past a stale
    heuristic. Mirrors tap-buddy's `test_client_has_no_other_methods` intent
    for this port.
    """
    from app.tools.lms_client import TapLmsClient

    public_methods = {
        name
        for name in dir(TapLmsClient)
        if not name.startswith("_") and callable(getattr(TapLmsClient, name))
    }
    read_only_methods = {
        "get_student_by_phone_and_name",
        "get_next_content",
        "get_quiz_question_details",
        # Not a pure read (creates/resumes a quiz attempt server-side, F-028)
        # but not a progress WRITE this test is guarding either — unchanged
        # classification from its own docstring.
        "start_quiz",
        # CR-013/ADR-014 Ask A (2026-07-29): a genuine pure read, scoped
        # server-side to test batch BT00000025 — never a progress write.
        "get_test_student_by_phone",
        "aclose",
    }
    write_methods = public_methods - read_only_methods
    assert write_methods == {"save_submission", "register_test_student", "reset_test_student"}


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
