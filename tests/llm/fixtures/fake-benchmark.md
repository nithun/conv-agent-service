# Fake benchmark artifact (test fixture only)

Used by `tests/llm/test_router.py` to exercise ADR-011's evidence-gate path
where `selected_by` resolves to a real file — not a real benchmark, not
referenced by any production MODEL_ROUTES entry.
