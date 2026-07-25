"""Media route/adapter tests (CR-004 Task 4): inbound media download -> ASR
-> transcript, outbound TTS -> /media route, path-traversal guard.

Retargeted from tap-buddy/tests/test_media.py against app.ingress.media —
uses httpx.ASGITransport/AsyncClient per this repo's async-stack test
convention (no TestClient/requests-style sync client).
"""

from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from fastapi import FastAPI

from app.config.settings import Settings
from app.core.state_machine import StudentIdentity, new_session
from app.ingress.contact_allowlist import verify_contact_allowlisted
from app.ingress.media import (
    cleanup_expired_media,
    download_media,
    synthesize_and_store,
    transcribe_inbound_media,
)
from app.ingress.media import router as media_router
from app.ingress.webhook_auth import verify_glific_webhook_signature
from app.tools.speech_client import AsrResult, TtsResult


def test_media_route_has_no_webhook_signature_dependency() -> None:
    """AC-8 (CR-009): /media/{token} stays public/BSP-fetchable per CR-007's
    live-verified contract — the BSP fetching audio anonymously is not a
    party that would ever carry a Glific-org signing secret. Negative-claim
    reachability check (P-005 idiom).
    """
    route = next(r for r in media_router.routes if r.path == "/media/{token}")  # type: ignore[attr-defined]
    dependency_callables = [dep.call for dep in route.dependant.dependencies]  # type: ignore[attr-defined]

    assert verify_glific_webhook_signature not in dependency_callables


def test_media_route_has_no_contact_allowlist_dependency() -> None:
    """AC-8 (CR-010): /media/{token} stays public/BSP-fetchable, explicitly
    untouched by this CR (Scope of change — the BSP fetching audio is not a
    Glific-authenticated party and carries no contact identity to check).
    """
    route = next(r for r in media_router.routes if r.path == "/media/{token}")  # type: ignore[attr-defined]
    dependency_callables = [dep.call for dep in route.dependant.dependencies]  # type: ignore[attr-defined]

    assert verify_contact_allowlisted not in dependency_callables


@pytest.mark.asyncio
@respx.mock
async def test_download_media() -> None:
    respx.get("http://glific.test/media/note.ogg").mock(
        return_value=httpx.Response(200, content=b"fake-ogg-bytes")
    )
    audio = await download_media("http://glific.test/media/note.ogg")
    assert audio == b"fake-ogg-bytes"


@pytest.mark.asyncio
@respx.mock
async def test_transcribe_inbound_media_reaches_turn_entrypoint_identically_to_text() -> None:
    respx.get("http://glific.test/media/note.ogg").mock(
        return_value=httpx.Response(200, content=b"fake-ogg-bytes")
    )
    session = new_session("contact-1", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(tap_language="English")

    fake_speech_client = MagicMock()
    fake_speech_client.transcribe.return_value = AsrResult(transcript="I'm okay", confidence=0.9)

    result = await transcribe_inbound_media(
        "http://glific.test/media/note.ogg", session, fake_speech_client
    )
    # A confident transcript flows into the turn path as plain text.
    assert result.transcript == "I'm okay"
    assert result.low_confidence is False


@pytest.mark.asyncio
@respx.mock
async def test_transcribe_low_confidence_preserves_raw_transcript() -> None:
    # Safety-first contract (live-caught 2026-07-17, BR-004 Task 10): the
    # RAW transcript is preserved even when low-confidence, so the worker's
    # distress gate can inspect it; only conversation logic gates on the
    # low_confidence flag.
    respx.get("http://glific.test/media/note.ogg").mock(
        return_value=httpx.Response(200, content=b"mumble-bytes")
    )
    session = new_session("contact-2", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(tap_language="English")

    fake_speech_client = MagicMock()
    fake_speech_client.transcribe.return_value = AsrResult(
        transcript="someone is hitting me", confidence=0.49
    )

    result = await transcribe_inbound_media(
        "http://glific.test/media/note.ogg", session, fake_speech_client
    )
    assert result.low_confidence is True
    assert result.transcript == "someone is hitting me"  # raw text preserved


@pytest.mark.asyncio
async def test_synthesize_and_store_produces_media_url(tmp_path) -> None:
    settings = Settings(
        media_dir=str(tmp_path), public_base_url="http://localhost:8000", _env_file=None
    )
    session = new_session("contact-3", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(tap_language="English")

    fake_speech_client = MagicMock()
    fake_speech_client.synthesize.return_value = TtsResult(
        audio_bytes=b"fake-mp3", content_type="audio/mpeg"
    )

    url = await synthesize_and_store("Great job!", session, fake_speech_client, settings)
    assert url.startswith("http://localhost:8000/media/")
    # CR-007 GAP 3: synthesize_and_store now emits a .mp3-suffixed URL.
    assert url.endswith(".mp3")


def test_cleanup_expired_media_deletes_old_files(tmp_path) -> None:
    old_file = tmp_path / "old.mp3"
    old_file.write_bytes(b"old")
    new_file = tmp_path / "new.mp3"
    new_file.write_bytes(b"new")

    now = time.time()
    old_time = now - 3600
    os.utime(old_file, (old_time, old_time))

    deleted = cleanup_expired_media(str(tmp_path), ttl_seconds=1800, now=now)
    assert deleted == 1
    assert not old_file.exists()
    assert new_file.exists()


def _app_with_media_dir(tmp_path, monkeypatch) -> FastAPI:
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")

    app = FastAPI()
    app.include_router(media_router)
    return app


@pytest.mark.asyncio
async def test_media_route_returns_audio_bytes_with_content_type(tmp_path, monkeypatch) -> None:
    app = _app_with_media_dir(tmp_path, monkeypatch)
    (tmp_path / "abcdef0123456789abcdef0123456789.mp3").write_bytes(b"fake-audio-bytes")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/media/abcdef0123456789abcdef0123456789")

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.content == b"fake-audio-bytes"


@pytest.mark.asyncio
async def test_media_route_rejects_path_traversal(tmp_path, monkeypatch) -> None:
    app = _app_with_media_dir(tmp_path, monkeypatch)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/media/..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code in (400, 404)

        resp2 = await client.get("/media/not-a-hex-token")
        assert resp2.status_code == 400


@pytest.mark.asyncio
async def test_media_route_missing_file_returns_404(tmp_path, monkeypatch) -> None:
    app = _app_with_media_dir(tmp_path, monkeypatch)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/media/abcdef0123456789abcdef0123456789")

    assert resp.status_code == 404


# ---- CR-007 GAP 3: .mp3-suffix route parity --------------------------------


@pytest.mark.asyncio
async def test_media_route_mp3_suffix_serves_identical_bytes_to_unsuffixed(
    tmp_path, monkeypatch
) -> None:
    app = _app_with_media_dir(tmp_path, monkeypatch)
    (tmp_path / "abcdef0123456789abcdef0123456789.mp3").write_bytes(b"fake-audio-bytes")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unsuffixed = await client.get("/media/abcdef0123456789abcdef0123456789")
        suffixed = await client.get("/media/abcdef0123456789abcdef0123456789.mp3")

    assert unsuffixed.status_code == suffixed.status_code == 200
    assert unsuffixed.content == suffixed.content == b"fake-audio-bytes"
    assert unsuffixed.headers["content-type"] == suffixed.headers["content-type"] == "audio/mpeg"


@pytest.mark.asyncio
async def test_media_route_mp3_suffix_missing_file_returns_404(tmp_path, monkeypatch) -> None:
    app = _app_with_media_dir(tmp_path, monkeypatch)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/media/abcdef0123456789abcdef0123456789.mp3")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_media_route_mp3_suffix_rejects_path_traversal_identically(
    tmp_path, monkeypatch
) -> None:
    app = _app_with_media_dir(tmp_path, monkeypatch)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/media/not-a-hex-token.mp3")
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_synthesize_and_store_url_is_fetchable_via_get_media(tmp_path) -> None:
    """End-to-end proof (AC-8): the exact URL synthesize_and_store returns
    resolves through get_media to the same bytes it wrote.
    """
    settings = Settings(
        media_dir=str(tmp_path), public_base_url="http://localhost:8000", _env_file=None
    )
    session = new_session("contact-9", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(tap_language="English")

    fake_speech_client = MagicMock()
    fake_speech_client.synthesize.return_value = TtsResult(
        audio_bytes=b"fake-mp3-bytes", content_type="audio/mpeg"
    )

    url = await synthesize_and_store("Great job!", session, fake_speech_client, settings)
    token_with_suffix = url.rsplit("/media/", 1)[1]
    assert token_with_suffix.endswith(".mp3")

    app = FastAPI()
    app.include_router(media_router)
    import app.ingress.media as media_module

    original_get_settings = media_module.get_settings
    media_module.get_settings = lambda: settings  # type: ignore[assignment]
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/media/{token_with_suffix}")
    finally:
        media_module.get_settings = original_get_settings  # type: ignore[assignment]

    assert resp.status_code == 200
    assert resp.content == b"fake-mp3-bytes"


@pytest.mark.asyncio
async def test_synthesize_and_store_fails_loudly_when_public_base_url_unset(tmp_path) -> None:
    """360°-r2 diff-review High finding 2: public_base_url has no baked
    default anymore (the old ':8000' default silently pointed media URLs at
    tap-buddy's port after the 8001 move). Unset must raise, not emit a
    relative/unreachable URL for Glific to fetch.
    """
    settings = Settings(media_dir=str(tmp_path), public_base_url="", _env_file=None)
    session = new_session("contact-4", "917834811114", needs_identity_confirmation=False)
    session.identity = StudentIdentity(tap_language="English")

    fake_speech_client = MagicMock()
    fake_speech_client.synthesize.return_value = TtsResult(
        audio_bytes=b"fake-mp3", content_type="audio/mpeg"
    )

    with pytest.raises(ValueError, match="PUBLIC_BASE_URL"):
        await synthesize_and_store("hello", session, fake_speech_client, settings)
    # And the guard fires BEFORE any TTS work is done.
    fake_speech_client.synthesize.assert_not_called()
