"""LLM layer: ModelRouter (OpenRouter-backed) + Langfuse tracing wrapper.

Invariant (L-004): every model call goes through the config-driven
ModelRouter and the Langfuse tracing wrapper; no raw provider calls, no
hardcoded model strings in business logic. Implementation lands in WBS
0.3-0.4, 1.7, 1.10.
"""
