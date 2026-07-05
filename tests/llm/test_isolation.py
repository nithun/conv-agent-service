"""AC-1(c): static-scan confirming zero inline model-string literals and
zero ad-hoc provider-client construction anywhere outside
`app/llm/providers/`. Mirrors tap-buddy's `tests/test_isolation.py` AST-walk
pattern, applied to a different invariant (L-004's hardcoded-model-string
ban / ADR-011's evidence gate scan) rather than tap-buddy's channel
isolation.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).parent.parent.parent / "app"
PROVIDERS_DIR = APP_ROOT / "llm" / "providers"

# Known provider-SDK client constructor calls — attribute or name access
# whose presence outside app/llm/providers/ indicates an ad-hoc provider
# client built directly in business logic (ADR-011's structural mitigation
# #1: lock-in confined to the SDK layer, never the calling-code layer).
FORBIDDEN_CONSTRUCTOR_NAMES = {
    "AsyncOpenAI",
    "OpenAI",
    "genai",  # google-genai (Vertex) client namespace
}

# Model-identifier literal substrings that must only ever appear inside
# app/llm/routes.py (the MODEL_ROUTES config) or app/llm/providers/ (default
# base URLs, provider-name strings). A business-logic module referencing one
# of these directly as a literal is a hardcoded-model-string violation
# (L-004), the exact defect class ADR-011's AC-1 static scan exists to
# catch.
FORBIDDEN_MODEL_STRING_SUBSTRINGS = (
    "claude-sonnet",
    "claude-haiku",
    "claude-opus",
    "gemini-",
    "gpt-4",
    "gpt-3.5",
)

ALLOWED_MODEL_STRING_FILES = {APP_ROOT / "llm" / "routes.py"}


def _iter_py_files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*.py") if "__pycache__" not in p.parts]


def _business_logic_files() -> list[Path]:
    """Every app/*.py file except app/llm/providers/ itself (which is
    exactly where provider-SDK construction and model-string literals are
    allowed to live) and app/llm/routes.py (the MODEL_ROUTES config file,
    the one sanctioned home for model-identifier literals).
    """
    files = []
    for path in _iter_py_files(APP_ROOT):
        if PROVIDERS_DIR in path.parents or path.parent == PROVIDERS_DIR:
            continue
        files.append(path)
    return files


def test_no_ad_hoc_provider_client_construction_outside_providers_dir() -> None:
    violations: list[tuple[str, str]] = []
    for path in _business_logic_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name in FORBIDDEN_CONSTRUCTOR_NAMES:
                    violations.append((str(path), name))
    assert violations == [], f"ad-hoc provider client construction found: {violations}"


def test_no_hardcoded_model_strings_outside_routes_config() -> None:
    violations: list[tuple[str, str]] = []
    for path in _business_logic_files():
        if path in ALLOWED_MODEL_STRING_FILES:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                lowered = node.value.lower()
                for needle in FORBIDDEN_MODEL_STRING_SUBSTRINGS:
                    if needle in lowered:
                        violations.append((str(path), node.value))
    assert violations == [], f"hardcoded model-string literals found: {violations}"


def test_providers_dir_is_the_only_place_provider_clients_are_constructed() -> None:
    """Sanity check the scan itself isn't vacuous — providers/openrouter.py
    SHOULD construct AsyncOpenAI, confirming the forbidden-name list is
    real and the exemption path (PROVIDERS_DIR) is exercised.
    """
    openrouter_path = PROVIDERS_DIR / "openrouter.py"
    assert openrouter_path.is_file()
    tree = ast.parse(openrouter_path.read_text(), filename=str(openrouter_path))
    found = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AsyncOpenAI"
        for node in ast.walk(tree)
    )
    assert found, "expected OpenRouterProvider to construct AsyncOpenAI directly"
