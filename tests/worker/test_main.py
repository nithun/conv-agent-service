"""Standalone worker process entrypoint tests (CR-004 Phase 7 gap-close):
contact-stream discovery finds active streams by key prefix, and
`_build_redis_client` sets `decode_responses=True` (same live-caught
requirement as `app.main`'s).
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.config.settings import Settings
from app.worker.main import _build_redis_client, _build_speech_client, _discover_active_contact_ids


@pytest.mark.asyncio
async def test_discover_active_contact_ids_finds_streams() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await redis.xadd("tap_agent:stream:contact:1", {"a": "1"})
    await redis.xadd("tap_agent:stream:contact:42", {"a": "1"})

    contact_ids = await _discover_active_contact_ids(redis)

    assert set(contact_ids) == {"1", "42"}


@pytest.mark.asyncio
async def test_discover_active_contact_ids_empty_when_no_streams() -> None:
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    contact_ids = await _discover_active_contact_ids(redis)
    assert contact_ids == []


def test_worker_build_redis_client_sets_decode_responses_true() -> None:
    settings = Settings(redis_url="redis://localhost:6379", _env_file=None)
    client = _build_redis_client(settings)
    assert client.connection_pool.connection_kwargs.get("decode_responses") is True


# ---- _build_speech_client: settings-gated construction (BR-004 Task 1) ---
#
# Mirrors tap-buddy's `_build_speech_client(settings)`
# (tap-buddy/src/tap_buddy/main.py:34-39): empty credentials -> None, never
# a bare unguarded SpeechClient() call that would crash worker startup via
# DefaultCredentialsError wherever creds are absent (CI, a fresh dev box).


def test_build_speech_client_returns_none_when_credentials_unset() -> None:
    settings = Settings(google_application_credentials="", _env_file=None)
    assert _build_speech_client(settings) is None


def test_build_speech_client_constructs_when_credentials_configured(monkeypatch) -> None:
    import app.tools.speech_client as speech_client_module

    constructed: list[bool] = []

    class _FakeSpeechClient:
        def __init__(self) -> None:
            constructed.append(True)

    monkeypatch.setattr(speech_client_module, "SpeechClient", _FakeSpeechClient)

    settings = Settings(
        google_application_credentials="/secrets/gcp-sa.json", _env_file=None
    )
    client = _build_speech_client(settings)

    assert client is not None
    assert constructed == [True]


@pytest.mark.asyncio
async def test_run_worker_loop_drains_the_escalation_stream_each_iteration(monkeypatch) -> None:
    """Code-review fix (2026-07-05, Finding 1): the standalone worker loop
    must actually call `drain_escalation_stream_once` — this is what
    performs the real Glific-facing side effects the ingress route now
    only enqueues for. Monkeypatches the module's client constructors (the
    real ones need live Frappe/Glific/OpenRouter credentials, out of scope
    for this unit-level wiring check) and asserts the escalation drain is
    invoked with the shared (non-per-contact) consumer name.
    """
    import app.worker.main as worker_main_module

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(worker_main_module, "_build_redis_client", lambda settings: fake_redis)

    class _NoopLmsClient:
        async def aclose(self) -> None:
            return None

    class _NoopGlificClient:
        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(
        worker_main_module, "TapLmsClient", lambda *a, **kw: _NoopLmsClient()
    )
    monkeypatch.setattr(
        worker_main_module, "GlificClient", lambda *a, **kw: _NoopGlificClient()
    )
    monkeypatch.setattr(
        worker_main_module, "build_default_model_router", lambda *a, **kw: object()
    )

    drain_calls: list[tuple[object, str]] = []

    async def _fake_drain_escalation_stream_once(redis, consumer_name, **kwargs):
        drain_calls.append((redis, consumer_name))
        return None

    monkeypatch.setattr(
        worker_main_module,
        "drain_escalation_stream_once",
        _fake_drain_escalation_stream_once,
    )

    await worker_main_module.run_worker_loop(max_iterations=1)

    assert len(drain_calls) == 1
    assert drain_calls[0][0] is fake_redis
    assert drain_calls[0][1] == worker_main_module.ESCALATION_CONSUMER_NAME


# ---- build_process_turn_fn: turn-flow re-arm reachability (F-019) ---------
#
# L-011/P-005's call-site check, applied to the re-arm: it is not enough
# that `maybe_rearm_turn_flow` exists and passes its own unit tests — the
# worker's actual per-entry turn function must CALL it after processing.
# The CR-004 port shipped setting + client method with no call site and
# every mocked suite stayed green; the gap only surfaced live at the F-019
# cutover smoke (turn 2 fell through to Glific's out-of-office handler).


@pytest.mark.asyncio
async def test_process_turn_fn_rearms_turn_flow_after_processing() -> None:
    from dataclasses import dataclass, field

    from app.ingress.glific_payload import GlificWebhookPayload
    from app.worker.main import build_process_turn_fn
    from app.worker.session_store import RedisSessionStore

    @dataclass
    class RecordingGlificClient:
        sent_texts: list[tuple[str, str]] = field(default_factory=list)
        started_flows: list[tuple[str, str]] = field(default_factory=list)

        async def send_text(self, receiver_id: str, body_text: str) -> dict[str, str]:
            self.sent_texts.append((receiver_id, body_text))
            return {"id": "fake-message-id"}

        async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> dict:
            return {"id": "fake-ticket-id"}

        async def start_contact_flow(self, flow_id: str, contact_id: str) -> bool:
            self.started_flows.append((flow_id, contact_id))
            return True

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    session_store = RedisSessionStore(redis, Settings(_env_file=None))
    glific = RecordingGlificClient()

    process_turn_fn = build_process_turn_fn(
        session_store=session_store,
        lms_client=object(),  # first-message branch never touches the LMS
        model_router=object(),  # ...nor the model router
        glific_client=glific,  # type: ignore[arg-type]
        turn_flow_id="26",
    )

    payload = GlificWebhookPayload(
        contact_id="7", phone="917834811114", message_id="m-rearm-1", text="buddy"
    )
    result = await process_turn_fn(payload)

    assert result.handled is True
    assert len(glific.sent_texts) == 1  # the name-ask greeting went out
    # THE point of this test: the turn function re-armed the relay flow so
    # the contact's next message reaches this service through Glific at all.
    assert glific.started_flows == [("26", "7")]


@pytest.mark.asyncio
async def test_process_turn_fn_skips_rearm_when_flow_id_unset() -> None:
    from dataclasses import dataclass, field

    from app.ingress.glific_payload import GlificWebhookPayload
    from app.worker.main import build_process_turn_fn
    from app.worker.session_store import RedisSessionStore

    @dataclass
    class RecordingGlificClient:
        sent_texts: list[tuple[str, str]] = field(default_factory=list)
        started_flows: list[tuple[str, str]] = field(default_factory=list)

        async def send_text(self, receiver_id: str, body_text: str) -> dict[str, str]:
            self.sent_texts.append((receiver_id, body_text))
            return {"id": "fake-message-id"}

        async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> dict:
            return {"id": "fake-ticket-id"}

        async def start_contact_flow(self, flow_id: str, contact_id: str) -> bool:
            self.started_flows.append((flow_id, contact_id))
            return True

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    session_store = RedisSessionStore(redis, Settings(_env_file=None))
    glific = RecordingGlificClient()

    process_turn_fn = build_process_turn_fn(
        session_store=session_store,
        lms_client=object(),
        model_router=object(),
        glific_client=glific,  # type: ignore[arg-type]
        turn_flow_id="",
    )

    payload = GlificWebhookPayload(
        contact_id="8", phone="917834811114", message_id="m-rearm-2", text="buddy"
    )
    await process_turn_fn(payload)

    assert glific.started_flows == []


@pytest.mark.asyncio
async def test_process_turn_fn_rearms_even_when_process_turn_raises() -> None:
    # Code-review finding 1 (2026-07-16): a failing turn must still re-arm
    # the relay flow (in `finally`) — otherwise a dead-lettered turn strands
    # the contact with no reply AND no armed flow (the F-019 symptom via an
    # error path). The exception must still propagate: the consumer loop's
    # retry/DLQ semantics depend on it.
    from dataclasses import dataclass, field

    import app.worker.main as worker_main_module
    from app.ingress.glific_payload import GlificWebhookPayload
    from app.worker.main import build_process_turn_fn
    from app.worker.session_store import RedisSessionStore

    @dataclass
    class FlowOnlyClient:
        started_flows: list[tuple[str, str]] = field(default_factory=list)

        async def start_contact_flow(self, flow_id: str, contact_id: str) -> bool:
            self.started_flows.append((flow_id, contact_id))
            return True

    async def _boom(*args, **kwargs):
        raise RuntimeError("model backend down")

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    session_store = RedisSessionStore(redis, Settings(_env_file=None))
    glific = FlowOnlyClient()

    process_turn_fn = build_process_turn_fn(
        session_store=session_store,
        lms_client=object(),
        model_router=object(),
        glific_client=glific,  # type: ignore[arg-type]
        turn_flow_id="26",
    )

    # Patch the module-level process_turn the factory closes over.
    import unittest.mock

    with unittest.mock.patch.object(worker_main_module, "process_turn", _boom):
        payload = GlificWebhookPayload(
            contact_id="9", phone="917834811114", message_id="m-rearm-3", text="hi"
        )
        with pytest.raises(RuntimeError, match="model backend down"):
            await process_turn_fn(payload)

    assert glific.started_flows == [("26", "9")]


@pytest.mark.asyncio
async def test_run_worker_loop_builds_turn_fn_with_settings_flow_id(monkeypatch) -> None:
    # Code-review suggestion (2026-07-16): entrypoint-level wiring check —
    # run_worker_loop must construct its per-entry turn function via
    # build_process_turn_fn with settings.glific_turn_flow_id (symmetry
    # with the escalation-drain wiring test above).
    import app.worker.main as worker_main_module

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(worker_main_module, "_build_redis_client", lambda settings: fake_redis)

    class _Noop:
        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(worker_main_module, "TapLmsClient", lambda *a, **kw: _Noop())
    monkeypatch.setattr(worker_main_module, "GlificClient", lambda *a, **kw: _Noop())
    monkeypatch.setattr(worker_main_module, "build_default_model_router", lambda *a, **kw: object())

    flow_id_settings = Settings(_env_file=None, glific_turn_flow_id="26")
    monkeypatch.setattr(worker_main_module, "get_settings", lambda: flow_id_settings)

    captured: dict[str, object] = {}
    real_build = worker_main_module.build_process_turn_fn

    def _recording_build(**kwargs):
        captured.update(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(worker_main_module, "build_process_turn_fn", _recording_build)

    async def _noop_drain(*args, **kwargs):
        return None

    monkeypatch.setattr(worker_main_module, "drain_escalation_stream_once", _noop_drain)

    await worker_main_module.run_worker_loop(max_iterations=1)

    assert captured.get("turn_flow_id") == "26"


@pytest.mark.asyncio
async def test_run_worker_loop_threads_speech_client_and_settings_into_turn_fn(
    monkeypatch,
) -> None:
    # BR-004 Task 2's shared plumbing: run_worker_loop must build the
    # (possibly-None) speech client via _build_speech_client and pass BOTH
    # it and settings through to build_process_turn_fn — otherwise
    # process_turn never receives what Tasks 2-4's call sites need, even
    # once they exist.
    import app.worker.main as worker_main_module

    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(worker_main_module, "_build_redis_client", lambda settings: fake_redis)

    class _Noop:
        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(worker_main_module, "TapLmsClient", lambda *a, **kw: _Noop())
    monkeypatch.setattr(worker_main_module, "GlificClient", lambda *a, **kw: _Noop())
    monkeypatch.setattr(worker_main_module, "build_default_model_router", lambda *a, **kw: object())

    configured_settings = Settings(
        _env_file=None, google_application_credentials="/secrets/gcp-sa.json"
    )
    monkeypatch.setattr(worker_main_module, "get_settings", lambda: configured_settings)

    sentinel_speech_client = object()
    monkeypatch.setattr(
        worker_main_module, "_build_speech_client", lambda settings: sentinel_speech_client
    )

    captured: dict[str, object] = {}
    real_build = worker_main_module.build_process_turn_fn

    def _recording_build(**kwargs):
        captured.update(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(worker_main_module, "build_process_turn_fn", _recording_build)

    async def _noop_drain(*args, **kwargs):
        return None

    monkeypatch.setattr(worker_main_module, "drain_escalation_stream_once", _noop_drain)

    await worker_main_module.run_worker_loop(max_iterations=1)

    assert captured.get("speech_client") is sentinel_speech_client
    assert captured.get("settings") is configured_settings
