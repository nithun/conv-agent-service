"""LLM layer: ModelRouter (multi-provider, ADR-011) + Langfuse tracing
wrapper.

Invariant (L-004): every model call goes through the config-driven
`ModelRouter` (`router.py`) and the Langfuse tracing wrapper (`tracing.py`);
no raw provider calls, no hardcoded model strings in business logic.
`routes.py` holds the `MODEL_ROUTES` config (ADR-011 shape: provider/model/
selected_by/fallback, defaulting to `"openrouter"`); `providers/` holds one
adapter module per provider behind the shared `generate()` interface
(`providers/base.py`). Ported from tap-buddy's single-Vertex-client
`clients/llm_client.py` onto this multi-provider-capable target per
CR-004/ADR-010 R-1/ADR-011.
"""
