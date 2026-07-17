"""Redis session store (CR-004 Task 10, R-2): the storage-shape successor
to tap-buddy's SQLite `core/store.py::SessionStore`.

Session hash key: `tap_agent:session:{glific_contact_id}` (AC-7 — keyed by
Glific's own contact id, never re-derived; Glific remains sole authority
for contact/conversation identity, R-7). Fields: `state`, `identity`
(student_id, child_name, grade, tap_language, candidates — JSON-encoded),
`content_reference` (JSON), `mode`, `turn_count`, `reexplanation_count`,
`asr_failure_count`, `language_choice_attempts` (CR-007), `readiness_response`,
`escalation_reason`, `created_at`, `updated_at`.

TTL policy (AD-002 Interfaces & schemas / CR-004 Open Question 2's
settled answer): `SESSION_TTL_SECONDS` (30 min inactivity, matching
tap-buddy's `state_machine.SESSION_TTL_SECONDS`) is applied as a Redis key
TTL, refreshed on every `save_session` call — this reproduces tap-buddy's
`SessionData.is_expired()`/`touch()` semantics (an idle session "expires"
i.e. a fresh session is created on the contact's next message) using
Redis's native eviction instead of an app-level expiry check on load.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import asdict
from typing import Any, cast

from redis.asyncio import Redis

from app.config.settings import Settings
from app.core.state_machine import (
    EscalationReason,
    SessionData,
    SessionState,
    StudentIdentity,
)

_SESSION_KEY_PREFIX = "tap_agent:session:"


def session_key(glific_contact_id: str) -> str:
    return f"{_SESSION_KEY_PREFIX}{glific_contact_id}"


def _serialize_session(session: SessionData) -> dict[str, str]:
    """Flattens SessionData into a Redis-hash-safe (string -> string) dict.
    Nested structures (identity, content_reference, resolved_locale) are
    JSON-encoded; None becomes an empty string sentinel decoded back to
    None on load.
    """
    return {
        "glific_contact_id": session.glific_contact_id,
        "phone": session.phone,
        "state": session.state.value,
        "identity": json.dumps(asdict(session.identity)),
        "content_reference": json.dumps(session.content_reference)
        if session.content_reference is not None
        else "",
        "mode": session.mode or "",
        "turn_count": str(session.turn_count),
        "reexplanation_count": str(session.reexplanation_count),
        "asr_failure_count": str(session.asr_failure_count),
        "language_choice_attempts": str(session.language_choice_attempts),
        "readiness_response": session.readiness_response or "",
        "resolved_locale": json.dumps(session.resolved_locale)
        if session.resolved_locale is not None
        else "",
        "escalation_reason": session.escalation_reason.value if session.escalation_reason else "",
        "created_at": str(session.created_at),
        "updated_at": str(session.updated_at),
    }


def _deserialize_session(fields: dict[str, str]) -> SessionData:
    identity_raw = json.loads(fields["identity"])
    identity = StudentIdentity(**identity_raw)
    content_reference: dict[str, Any] | None = (
        json.loads(fields["content_reference"]) if fields.get("content_reference") else None
    )
    resolved_locale: dict[str, Any] | None = (
        json.loads(fields["resolved_locale"]) if fields.get("resolved_locale") else None
    )
    escalation_reason = (
        EscalationReason(fields["escalation_reason"]) if fields.get("escalation_reason") else None
    )
    return SessionData(
        glific_contact_id=fields["glific_contact_id"],
        phone=fields["phone"],
        state=SessionState(fields["state"]),
        identity=identity,
        content_reference=content_reference,
        mode=fields.get("mode") or None,
        turn_count=int(fields["turn_count"]),
        reexplanation_count=int(fields["reexplanation_count"]),
        asr_failure_count=int(fields["asr_failure_count"]),
        # CR-007: tolerate a pre-existing Redis hash serialized before this
        # field existed — `.get()` with a "0" default, same discipline as
        # every other scalar int field here (no migration needed).
        language_choice_attempts=int(fields.get("language_choice_attempts") or "0"),
        readiness_response=fields.get("readiness_response") or None,
        resolved_locale=resolved_locale,
        escalation_reason=escalation_reason,
        created_at=float(fields["created_at"]),
        updated_at=float(fields["updated_at"]),
    )


class RedisSessionStore:
    """Redis-backed replacement for tap-buddy's SQLite `SessionStore`
    (session half only — dedupe lives in `app/queue/enqueue.py`,
    escalations are Glific Tickets now, not a bespoke table, Task 13).
    """

    def __init__(self, redis: Redis, settings: Settings):
        self._redis = redis
        self._ttl_seconds = settings.session_ttl_seconds

    async def save_session(self, session: SessionData) -> None:
        payload = _serialize_session(session)
        key = session_key(session.glific_contact_id)
        # redis-py's stub types hset as `Union[Awaitable[int], int]` (shared
        # across the sync and async client bases) — on the async client
        # it's always awaitable at runtime; the cast documents that rather
        # than scattering a per-call type: ignore.
        await cast(Awaitable[int], self._redis.hset(key, mapping=payload))
        # Refresh TTL on every save — mirrors tap-buddy's touch()-on-write
        # semantics: an active session's expiry keeps sliding forward.
        await cast(Awaitable[int], self._redis.expire(key, self._ttl_seconds))

    async def load_session(self, glific_contact_id: str) -> SessionData | None:
        key = session_key(glific_contact_id)
        fields = await cast(Awaitable[dict[str, str]], self._redis.hgetall(key))
        if not fields:
            return None
        return _deserialize_session(fields)

    async def delete_session(self, glific_contact_id: str) -> None:
        await self._redis.delete(session_key(glific_contact_id))
