"""Structural isolation check — core state machine, guardrails, identity,
and prompts modules must import zero FastAPI/Redis/Glific-client/ingress
symbols. Walks the AST of each module's imports rather than trusting
convention. Mirrors tap-buddy's tests/test_isolation.py pattern (CR-004
Task 7/9 acceptance discipline).
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).parent.parent.parent / "app"
CORE_ROOT = APP_ROOT / "core"

CORE_MODULES = [
    CORE_ROOT / "state_machine.py",
    CORE_ROOT / "guardrails_checks.py",
    CORE_ROOT / "identity.py",
    CORE_ROOT / "prompts.py",
]

FORBIDDEN_MODULE_PREFIXES = (
    "fastapi",
    "redis",
    "arq",
    "app.ingress",
    "app.delivery",
    "app.worker",
)


def _imported_module_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_core_modules_exist():
    for path in CORE_MODULES:
        assert path.is_file(), f"expected core module at {path}"


def test_core_modules_have_no_forbidden_imports():
    violations = []
    for path in CORE_MODULES:
        imported = _imported_module_names(path)
        for name in imported:
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in FORBIDDEN_MODULE_PREFIXES
            ):
                violations.append((path.name, name))
    assert violations == [], f"core modules leak channel-specific imports: {violations}"
