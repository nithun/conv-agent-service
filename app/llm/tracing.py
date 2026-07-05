"""Langfuse tracing wrapper (Task 2, ADR-005/L-004/ADR-011).

Every provider adapter's `generate()` call is traced identically — route
name, provider, model, tokens, latency — regardless of which provider
served the call. The wrapper lives at the `ModelRouter` layer
(`app/llm/router.py`), not duplicated per-provider-adapter, per ADR-011's
"multi-provider capable, not multi-blind-spot" framing.

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
    ) -> None:
        self._on_end(  # type: ignore[operator]
            self,
            latency_s=latency_s,
            error=error,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
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
    ) -> None:
        usage_details = None
        if input_tokens is not None or output_tokens is not None:
            usage_details = {"input": input_tokens or 0, "output": output_tokens or 0}
        try:
            generation.end(  # type: ignore[attr-defined]
                usage_details=usage_details,
                metadata={"latency_s": latency_s, "error": error},
                level="ERROR" if error else "DEFAULT",
            )
        except Exception:
            logger.exception("Langfuse trace finalization failed")
