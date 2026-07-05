"""Provider adapters (ADR-011): one module per LLM provider, each
implementing the shared interface in `base.py` — `generate(route, messages,
tools) -> ModelResponse`. This is the ONLY place a provider SDK/client is
constructed directly; enforced by `tests/llm/test_isolation.py`'s AST scan.
"""
