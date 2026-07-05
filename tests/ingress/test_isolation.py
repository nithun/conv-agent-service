"""Structural isolation check (code-review fix, 2026-07-05, Finding 1 —
High-priority companion): `app/ingress/` must never call a `GlificClient`
method directly — every Glific-facing side effect (send_text, create_ticket,
send_audio, etc.) must go through the queue (enqueue onto a Redis Stream),
never be awaited inline on the fast-ack path. This is the exact regression
class the L-002 fix (distress-escalation path calling `distress_handler`
inline, which reached `GlificClient.send_text`/`create_ticket`) belongs to —
this test exists so a future call site can't silently reintroduce it.

Walks the AST of every `app/ingress/*.py` module's calls, mirroring
tests/core/test_isolation.py and tests/llm/test_isolation.py's AST-walk
pattern applied to a different invariant.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).parent.parent.parent / "app"
INGRESS_ROOT = APP_ROOT / "ingress"

# GlificClient's actual Glific-facing methods (app/delivery/glific_client.py)
# — an attribute-call with one of these names, anywhere in app/ingress/, is
# the regression class this test guards against.
FORBIDDEN_GLIFIC_CLIENT_METHOD_NAMES = {
    "send_text",
    "send_audio",
    "create_ticket",
    "register_media",
    "start_contact_flow",
}

# GlificClient itself must never be imported/constructed in app/ingress/ —
# the only legitimate way ingress touches Glific-adjacent state is via the
# queue (Redis enqueue), never a direct client call.
FORBIDDEN_IMPORT_MODULE = "app.delivery.glific_client"


def _ingress_modules() -> list[Path]:
    return [p for p in INGRESS_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_ingress_modules_exist() -> None:
    modules = _ingress_modules()
    assert modules, f"expected at least one module under {INGRESS_ROOT}"


def test_ingress_never_imports_glific_client_module() -> None:
    violations = []
    for path in _ingress_modules():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == FORBIDDEN_IMPORT_MODULE:
                violations.append(str(path))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == FORBIDDEN_IMPORT_MODULE:
                        violations.append(str(path))
    assert violations == [], (
        f"app/ingress/ must never import {FORBIDDEN_IMPORT_MODULE} directly — "
        f"Glific-facing side effects belong in the worker, reached via the queue "
        f"(L-002): {violations}"
    )


def test_ingress_never_calls_a_glific_client_method_directly() -> None:
    violations: list[tuple[str, str]] = []
    for path in _ingress_modules():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_GLIFIC_CLIENT_METHOD_NAMES:
                    violations.append((str(path), node.func.attr))
    assert violations == [], (
        f"app/ingress/ must never call a GlificClient method inline — these are real "
        f"network calls (GlificClient defaults to a 10s httpx timeout) that can blow "
        f"Glific's 4s webhook budget (L-002). Route through the queue instead "
        f"(app/queue/escalation_queue.py's enqueue pattern): {violations}"
    )


def test_scan_is_not_vacuous_glific_client_really_defines_these_methods() -> None:
    """Sanity check the forbidden-name list is real, not a typo'd list that
    would make the scan above vacuously pass.
    """
    glific_client_path = APP_ROOT / "delivery" / "glific_client.py"
    tree = ast.parse(glific_client_path.read_text(), filename=str(glific_client_path))
    defined_method_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }
    found = FORBIDDEN_GLIFIC_CLIENT_METHOD_NAMES & defined_method_names
    assert found, (
        "expected at least one FORBIDDEN_GLIFIC_CLIENT_METHOD_NAMES entry to match a "
        "real GlificClient method — the scan's forbidden-name list may be stale"
    )
