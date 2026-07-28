"""Langfuse tracing wrapper (Task 2, ADR-005/L-004/ADR-011).

Every provider adapter's `generate()` call is traced identically — route
name, provider, model, tokens, latency — regardless of which provider
served the call. The wrapper lives at the `ModelRouter` layer
(`app/llm/router.py`), not duplicated per-provider-adapter, per ADR-011's
"multi-provider capable, not multi-blind-spot" framing.

F-079 (architecture-reviewer ratification of CR-012): the trace's `model`
field starts as the STATIC `RouteConfig.model` label (all that's known
before the call), but is corrected at `end()` time to the actually-served
model when a provider adapter detects a mismatch (`ModelResponse.
served_model` — see `app/llm/providers/openrouter.py`, populated when the
OpenAI-compatible endpoint is actually the LiteLLM gateway serving the
Vertex-hosted model). Without this, every `tap_buddy_turn` call through
that gateway would be misattributed in the Langfuse cost/model dashboard
as whatever un-aliased label the route config happens to use (this repo's
static route labels predate this CR and were never renamed — see
`app/llm/routes.py`'s naming note).

`Tracer` is a small protocol so `ModelRouter` can be tested with a
`RecordingTracer` test double (AC-1b) without needing a real Langfuse
project; `NullTracer` is the safe do-nothing default for construction paths
that don't care about tracing (e.g. unit tests of the router itself);
`LangfuseTracer` is the real production implementation.

Never logs PII, prompts, or raw response payloads to application logs
(L-007) — Langfuse itself is the sanctioned prompt/response capture
surface; this module logs only route/provider/model/timing metadata.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger("app.llm.tracing")


class TraceSpan(Protocol):
    def end(
        self,
        *,
        latency_s: float,
        error: bool = False,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        served_model: str | None = None,
        served_cost_usd: float | None = None,
    ) -> None: ...


class Tracer(Protocol):
    def start_trace(self, *, route_name: str, provider: str, model: str) -> TraceSpan: ...


@dataclass
class _RecordedSpan:
    """A closed-over span implementation shared by every concrete Tracer —
    each Tracer's `start_trace` returns one of these, configured with a
    callback that does the tracer-specific finalization (Langfuse flush, an
    in-memory record, or nothing).
    """

    route_name: str
    provider: str
    model: str
    _on_end: object  # set by the owning Tracer; callable, kept loosely typed to avoid a cycle

    def end(
        self,
        *,
        latency_s: float,
        error: bool = False,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        served_model: str | None = None,
        served_cost_usd: float | None = None,
    ) -> None:
        self._on_end(  # type: ignore[operator]
            self,
            latency_s=latency_s,
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            served_model=served_model,
            served_cost_usd=served_cost_usd,
        )


class NullTracer:
    """Does nothing — the safe default for contexts that don't need
    tracing evidence (e.g. constructing a `ModelRouter` for a unit test that
    isn't asserting anything about tracing).
    """

    def start_trace(self, *, route_name: str, provider: str, model: str) -> _RecordedSpan:
        return _RecordedSpan(
            route_name=route_name, provider=provider, model=model, _on_end=self._noop
        )

    @staticmethod
    def _noop(span: _RecordedSpan, **kwargs: object) -> None:
        return None


@dataclass
class RecordedCall:
    route_name: str
    provider: str
    model: str
    latency_s: float
    error: bool
    input_tokens: int | None
    output_tokens: int | None
    # F-079: the model that ACTUALLY served the call, when a provider
    # detected it differs from `model` (the static RouteConfig label) —
    # None when no such detection happened (e.g. real OpenRouter).
    served_model: str | None = None
    served_cost_usd: float | None = None


@dataclass
class RecordingTracer:
    """Test double: records every completed call in-memory so a test can
    assert a trace was recorded through the shared interface (AC-1b),
    without needing a real Langfuse project.
    """

    calls: list[RecordedCall] = field(default_factory=list)

    def start_trace(self, *, route_name: str, provider: str, model: str) -> _RecordedSpan:
        return _RecordedSpan(
            route_name=route_name, provider=provider, model=model, _on_end=self._record
        )

    def _record(
        self,
        span: _RecordedSpan,
        *,
        latency_s: float,
        error: bool = False,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        served_model: str | None = None,
        served_cost_usd: float | None = None,
    ) -> None:
        self.calls.append(
            RecordedCall(
                route_name=span.route_name,
                provider=span.provider,
                model=span.model,
                latency_s=latency_s,
                error=error,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                served_model=served_model,
                served_cost_usd=served_cost_usd,
            )
        )


class LangfuseTracer:
    """Real production tracer — wraps the `langfuse` SDK (v2.x, per
    `pyproject.toml`'s pin). One generation span per `ModelRouter.generate()`
    call, tagged with route name and provider so per-route and per-provider
    views are both derivable from the same trace store (ADR-011's lock-in
    mitigation #2).

    Uses the v2 SDK's `Langfuse.generation(...)` /
    `StatefulGenerationClient.end(...)` shape (NOT the v3
    `start_generation`/`.update()` shape — confirmed against the installed
    package, since the vendor's own example payloads are not a substitute
    for checking the pinned version's real signature, per this workspace's
    "verify premises" discipline).
    """

    def __init__(self) -> None:
        from langfuse import Langfuse

        self._client = Langfuse()

    def flush(self) -> None:
        """Forces the batched SDK client to send any pending events now,
        rather than waiting for its background flush interval or process
        exit (the SDK registers an `atexit` flush, which a long-lived
        `worker`/`app` process never hits until shutdown). Used by the
        CR-012 AC-4 live smoke test (`tests/llm/test_gemini_live_smoke.py`)
        to observe its own trace via the Langfuse API without a raced sleep.
        """
        self._client.flush()

    def start_trace(self, *, route_name: str, provider: str, model: str) -> _RecordedSpan:
        generation = self._client.generation(
            name=route_name,
            model=model,
            metadata={"route": route_name, "provider": provider},
        )
        return _RecordedSpan(
            route_name=route_name,
            provider=provider,
            model=model,
            _on_end=lambda _span, **kwargs: self._end(generation, **kwargs),
        )

    @staticmethod
    def _end(
        generation: object,
        *,
        latency_s: float,
        error: bool = False,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        served_model: str | None = None,
        served_cost_usd: float | None = None,
    ) -> None:
        # v2-server field is `usage` — NOT `usage_details`, which the
        # installed v2.60 SDK *accepts* (its signature has both) but which
        # only a v3 SERVER ingests; a v2 server silently drops it, leaving
        # tokens 0/0 in the dashboard. Caught live 2026-07-08 against the
        # self-hosted v2.95 server (F-020 Langfuse-half verification): a
        # probe with input_tokens=7/output_tokens=13 arrived with usage
        # zeroed. The SDK signature check the docstring describes was
        # necessary but not sufficient — the SERVER's accepted shape is the
        # real contract (L-009).
        usage = None
        if input_tokens is not None or output_tokens is not None:
            usage = {
                "input": input_tokens or 0,
                "output": output_tokens or 0,
                "unit": "TOKENS",
            }
        metadata: dict[str, object] = {"latency_s": latency_s, "error": error}
        # F-079 (architecture-reviewer ratification of CR-012): the
        # generation was created (start_trace, above) with `model=` set to
        # the STATIC RouteConfig label (e.g. "anthropic/claude-sonnet-5")
        # because that's all that's known before the call. When the
        # provider detected the REAL served model differs (LiteLLM->Vertex
        # aliasing), overriding `model=` here on end() corrects the trace
        # actually stored/displayed — `StatefulGenerationClient.end` accepts
        # `model` for exactly this "update at close" case. Without this,
        # Gemini calls are misattributed as Claude in the cost/model
        # dashboard.
        end_kwargs: dict[str, object] = {}
        if served_model:
            end_kwargs["model"] = served_model
            metadata["served_model"] = served_model
        if served_cost_usd is not None:
            metadata["served_cost_usd"] = served_cost_usd
        try:
            generation.end(  # type: ignore[attr-defined]
                usage=usage,
                metadata=metadata,
                level="ERROR" if error else "DEFAULT",
                **end_kwargs,
            )
        except Exception:
            logger.exception("Langfuse trace finalization failed")
