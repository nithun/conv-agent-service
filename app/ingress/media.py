"""Inbound voice-note download + ASR trigger, outbound TTS + `/media/{token}`
route (CR-004 Task 4). Ported from `tap-buddy/src/tap_buddy/adapters/media.py`.

Adapter layer — may import FastAPI; `app/core/` never imports this.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.config.language_map import LocaleEntry, resolve_locale
from app.config.settings import Settings, get_settings
from app.core.state_machine import SessionData
from app.tools.speech_client import AsrResult, TtsResult

router = APIRouter()

_MEDIA_TOKEN_SAFE_CHARS = set("abcdef0123456789")


class _SpeechClient(Protocol):
    """Structural type for the ASR/TTS client this module needs —
    satisfied by `app.tools.speech_client.SpeechClient` or any test double
    without either module importing the other.
    """

    def transcribe(self, audio_bytes: bytes, locale: LocaleEntry) -> AsrResult: ...
    def synthesize(self, text: str, locale: LocaleEntry) -> TtsResult: ...


async def download_media(url: str, *, timeout: float = 15.0) -> bytes:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


@dataclass(frozen=True)
class InboundTranscription:
    """Raw ASR outcome for one inbound voice note.

    `transcript` is the RAW best-effort text — it may be NON-EMPTY even when
    `low_confidence` is True. This is deliberate (live-caught 2026-07-17,
    BR-004 Task 10): a distress utterance transcribed at 0.49 confidence
    must still reach the worker-side distress gate — safety checks run on
    best-effort text; only CONVERSATION logic gates on `low_confidence`.
    Callers must never feed a low-confidence transcript into the state
    machine/LLM path.
    """

    transcript: str
    low_confidence: bool


async def transcribe_inbound_media(
    media_url: str, session: SessionData | None, speech_client: _SpeechClient
) -> InboundTranscription:
    """Downloads a voice-note media URL and runs it through the ASR
    wrapper, using the session's resolved language. Returns the raw
    transcript + confidence verdict — see `InboundTranscription` for the
    safety-first contract (the transcript is preserved even when
    low-confidence, for the distress gate; it previously returned "" and
    the discarded text was live-caught hiding a spoken distress cry from
    the gate).
    """
    audio_bytes = await download_media(media_url)
    tap_language = session.identity.tap_language if session else None
    locale = resolve_locale(tap_language)
    result = speech_client.transcribe(audio_bytes, locale)
    return InboundTranscription(
        transcript=result.transcript.strip(), low_confidence=result.low_confidence
    )


def _media_token() -> str:
    return uuid.uuid4().hex


def _media_path(media_dir: str, token: str) -> Path:
    # token is always a fresh uuid4 hex from _media_token — never
    # user-supplied — but validate defensively against path traversal
    # anyway before touching the filesystem.
    if not token or not set(token) <= _MEDIA_TOKEN_SAFE_CHARS:
        raise ValueError("invalid media token")
    return Path(media_dir) / f"{token}.mp3"


async def synthesize_and_store(
    text: str, session: SessionData, speech_client: _SpeechClient, settings: Settings
) -> str:
    """Runs `text` through the TTS wrapper, persists the audio file under
    MEDIA_DIR, and returns the publicly reachable /media/{token} URL built
    from PUBLIC_BASE_URL.
    """
    if not settings.public_base_url:
        # Fail loudly, not with a silently-relative (unreachable) media URL —
        # this string is what Glific/WhatsApp will literally fetch. No baked
        # default exists (360°-r2 diff-review High finding: the old ":8000"
        # default pointed at tap-buddy's port after the 8001 move).
        raise ValueError(
            "PUBLIC_BASE_URL is not set — cannot build a reachable /media/ URL. "
            "Set it in .env (local dev: http://localhost:8001)."
        )

    locale = resolve_locale(session.identity.tap_language)
    tts_result = speech_client.synthesize(text, locale)

    token = _media_token()
    path = _media_path(settings.media_dir, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(tts_result.audio_bytes)

    return f"{settings.public_base_url.rstrip('/')}/media/{token}"


def cleanup_expired_media(media_dir: str, ttl_seconds: int, *, now: float | None = None) -> int:
    """Simple TTL/cleanup pass: deletes any .mp3 under media_dir older than
    ttl_seconds. Ported as-is from tap-buddy — acceptable for this port's
    trial scale; not wired to a scheduler by default.
    """
    now = now if now is not None else time.time()
    deleted = 0
    media_path = Path(media_dir)
    if not media_path.exists():
        return 0
    for f in media_path.glob("*.mp3"):
        if now - f.stat().st_mtime > ttl_seconds:
            f.unlink(missing_ok=True)
            deleted += 1
    return deleted


@router.get("/media/{token}")
async def get_media(token: str) -> Response:
    settings = get_settings()
    try:
        path = _media_path(settings.media_dir, token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid media token") from exc

    if not path.is_file():
        raise HTTPException(status_code=404, detail="media not found")

    # Defense-in-depth: confirm the resolved path is still inside media_dir
    # (guards path traversal / directory listing even if the token charset
    # check were ever bypassed).
    media_root = Path(settings.media_dir).resolve()
    if media_root not in path.resolve().parents and path.resolve() != media_root:
        raise HTTPException(status_code=400, detail="invalid media path")

    return Response(content=path.read_bytes(), media_type="audio/mpeg")
