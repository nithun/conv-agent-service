"""Glific GraphQL client: auth + createAndSendMessage delivery + media send
+ flow re-arm + Ticketing (CR-004 Task 13/14). Ported from
`tap-buddy/src/tap_buddy/adapters/glific_client.py`.

Live-verified against the local dev Glific instance (tap-buddy build,
2026-07-03/04 — see AD-002's External contracts table and
docs/glific-dev-endpoint.md). Re-verification against THIS stack (arq
worker process vs. tap-buddy's single FastAPI process) is Phase 7's job
(L-009) — not assumed carried over just because the mutation names are
unchanged.

- Auth: POST {base}/api/v1/session {"user": {"phone", "password"}} ->
  data.access_token (+ renewal_token, token_expiry_time). Token passed as a
  bare `authorization: <token>` header (NO "Bearer " prefix).
- Text/media send: mutation `createAndSendMessage(input: MessageInput!)`.
  `sendMessage` DOES NOT EXIST in the live schema (F-003) — regression
  guarded by tests/delivery/test_glific_client.py::test_never_calls_send_message_regression_guard.
- Media registration (for outbound voice notes): mutation
  `createMessageMedia(input: MessageMediaInput!)`.
- Flow re-arm: mutation `startContactFlow(flowId, contactId)`.
- Ticketing: mutation `createTicket(input: TicketInput!)` (CR-002/L-007
  shape) — the ONLY escalation/handoff surface; `adapters/review.py`
  (tap-buddy's ad-hoc HTML dashboard) does NOT port (ADR-010 Invariant 5,
  Nithun's decision 2026-07-05).

### Local-dev-only Docker `Host`-header workaround (F-021)

The local dev Glific instance's `SubdomainPlug` hardcodes a hostname
allowlist (`localhost`/`127.0.0.1`) that omits `host.docker.internal` — it
rejects (403) any request whose `Host` header isn't in that allowlist,
regardless of source IP or credentials (root-caused by the sibling
`glific-platform` workspace, `BR-001-docker-host-403-subdomain-plug.md`;
this workspace's own diagnosis trail is `docs/open-followups.md` F-021 in
the parent workspace). This client never hardcodes a workaround for that —
`host_header_override` (wired from the optional `GLIFIC_HOST_HEADER_OVERRIDE`
env var, empty/unset by default, see `app/config/settings.py`) lets the
TCP connection still go to wherever `base_url`/`GLIFIC_API_URL` resolves
(`host.docker.internal:4000` in local dev) while overriding only the
outbound `Host` header httpx would otherwise derive from that URL, so the
request lands inside the allowlist. This is opt-in via env, exactly like
`GLIFIC_API_URL` itself — it must never default to a value and must never
silently activate in staging/prod, where the real fix belongs on Glific's
side (BR-001), not as a permanent client-side workaround here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

SESSION_AUTH_PATH = "/api/v1/session"
SESSION_RENEW_PATH = "/api/v1/session/renew"
GRAPHQL_PATH = "/api"

CREATE_AND_SEND_MESSAGE_MUTATION = """
mutation createAndSendMessage($input: MessageInput!) {
  createAndSendMessage(input: $input) {
    message { id body type }
    errors { key message }
  }
}
"""

START_CONTACT_FLOW_MUTATION = """
mutation startContactFlow($flowId: ID!, $contactId: ID!) {
  startContactFlow(flowId: $flowId, contactId: $contactId) {
    success
    errors { key message }
  }
}
"""

CREATE_MESSAGE_MEDIA_MUTATION = """
mutation createMessageMedia($input: MessageMediaInput!) {
  createMessageMedia(input: $input) {
    messageMedia { id url }
    errors { key message }
  }
}
"""

CREATE_TICKET_MUTATION = """
mutation createTicket($input: TicketInput!) {
  createTicket(input: $input) {
    ticket { id topic body }
    errors { key message }
  }
}
"""

# Token is considered stale this many seconds before its reported expiry,
# to avoid racing a call against the exact expiry instant.
TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS = 30


class GlificApiError(Exception):
    pass


@dataclass
class _TokenState:
    access_token: str
    renewal_token: str
    expires_at: float  # epoch seconds


class GlificClient:
    def __init__(
        self,
        base_url: str,
        phone: str,
        password: str,
        timeout: float = 10.0,
        host_header_override: str | None = None,
    ):
        """`host_header_override`: local-dev-only Docker workaround (F-021,
        see module docstring). When set (non-empty), every request this
        client makes sends that value as the `Host` header instead of the
        one httpx would derive from `base_url` — the TCP connection still
        goes to `base_url`'s real address. Left `None`/empty, the client's
        behavior is unchanged (no header override sent, current staging/prod
        behavior). Callers must only pass this from an explicit, opt-in env
        var (`GLIFIC_HOST_HEADER_OVERRIDE`) — never a hardcoded default.
        """
        self.base_url = base_url.rstrip("/")
        self.phone = phone
        self.password = password
        self.host_header_override = host_header_override or None
        default_headers = (
            {"Host": self.host_header_override} if self.host_header_override else None
        )
        self._client = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout, headers=default_headers
        )
        self._token: _TokenState | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- auth -----------------------------------------------------------

    async def _login(self) -> _TokenState:
        resp = await self._client.post(
            SESSION_AUTH_PATH, json={"user": {"phone": self.phone, "password": self.password}}
        )
        if resp.status_code >= 400:
            raise GlificApiError(f"Glific auth failed: {resp.status_code} {resp.text[:200]}")
        body = resp.json()
        data = body.get("data", {})
        expiry_time = data.get("token_expiry_time")
        expires_at = _parse_expiry(expiry_time)
        return _TokenState(
            access_token=data["access_token"],
            renewal_token=data.get("renewal_token", ""),
            expires_at=expires_at,
        )

    async def _renew(self, token: _TokenState) -> _TokenState:
        resp = await self._client.post(
            SESSION_RENEW_PATH, headers={"authorization": token.access_token}
        )
        if resp.status_code >= 400:
            return await self._login()
        body = resp.json()
        data = body.get("data", {})
        expiry_time = data.get("token_expiry_time")
        return _TokenState(
            access_token=data["access_token"],
            renewal_token=data.get("renewal_token", token.renewal_token),
            expires_at=_parse_expiry(expiry_time),
        )

    async def _get_valid_token(self) -> str:
        now = time.time()
        if self._token is None:
            self._token = await self._login()
        elif now >= (self._token.expires_at - TOKEN_EXPIRY_SAFETY_MARGIN_SECONDS):
            self._token = await self._renew(self._token)
        return self._token.access_token

    # ---- GraphQL ----------------------------------------------------------

    async def _graphql(
        self, query: str, variables: dict[str, Any], *, _retried: bool = False
    ) -> dict[str, Any]:
        token = await self._get_valid_token()
        resp = await self._client.post(
            GRAPHQL_PATH,
            json={"query": query, "variables": variables},
            headers={"authorization": token},
        )
        if resp.status_code == 401 and not _retried:
            self._token = await self._login()
            return await self._graphql(query, variables, _retried=True)
        if resp.status_code >= 400:
            raise GlificApiError(f"Glific GraphQL error {resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        if body.get("errors"):
            raise GlificApiError(f"Glific GraphQL errors: {body['errors']}")
        data: dict[str, Any] = body["data"]
        return data

    # ---- delivery ---------------------------------------------------------

    async def send_text(
        self, receiver_id: str, body_text: str, sender_id: str | None = None
    ) -> dict[str, Any]:
        variables = {
            "input": {
                "body": body_text,
                "type": "TEXT",
                "receiverId": receiver_id,
                "flow": "OUTBOUND",
                **({"senderId": sender_id} if sender_id else {}),
            }
        }
        data = await self._graphql(CREATE_AND_SEND_MESSAGE_MUTATION, variables)
        result = data["createAndSendMessage"]
        if result.get("errors"):
            raise GlificApiError(f"createAndSendMessage errors: {result['errors']}")
        message: dict[str, Any] = result["message"]
        return message

    async def register_media(self, url: str, caption: str | None = None) -> str:
        """Registers a media URL via createMessageMedia, returns the media id."""
        variables = {"input": {"url": url, "sourceUrl": url, "caption": caption or ""}}
        data = await self._graphql(CREATE_MESSAGE_MEDIA_MUTATION, variables)
        result = data["createMessageMedia"]
        if result.get("errors"):
            raise GlificApiError(f"createMessageMedia errors: {result['errors']}")
        media_id: str = result["messageMedia"]["id"]
        return media_id

    async def send_audio(
        self,
        receiver_id: str,
        media_url: str,
        caption: str | None = None,
        sender_id: str | None = None,
    ) -> dict[str, Any]:
        media_id = await self.register_media(media_url, caption=caption)
        variables = {
            "input": {
                "body": caption or "",
                "type": "AUDIO",
                "mediaId": media_id,
                "receiverId": receiver_id,
                "flow": "OUTBOUND",
                **({"senderId": sender_id} if sender_id else {}),
            }
        }
        data = await self._graphql(CREATE_AND_SEND_MESSAGE_MUTATION, variables)
        result = data["createAndSendMessage"]
        if result.get("errors"):
            raise GlificApiError(f"createAndSendMessage (audio) errors: {result['errors']}")
        message: dict[str, Any] = result["message"]
        return message

    async def start_contact_flow(self, flow_id: str, contact_id: str) -> bool:
        """Starts a flow for a contact. Used to re-arm the turn-relay flow
        after each reply: Glific's webhook Oban jobs are unique on
        (context_id, url, action_id) for 60s, so a wait->webhook loop inside
        ONE flow context silently drops every repeat call (live-verified,
        tap-buddy 2026-07-04). A fresh flow run per turn gives a fresh
        context_id, sidestepping the uniqueness window.
        """
        variables = {"flowId": flow_id, "contactId": contact_id}
        data = await self._graphql(START_CONTACT_FLOW_MUTATION, variables)
        result = data["startContactFlow"]
        if result.get("errors"):
            raise GlificApiError(f"startContactFlow errors: {result['errors']}")
        return bool(result.get("success"))

    # ---- Ticketing (CR-002/L-007 escalation surface, Task 13) -------------
    #
    # `createTicket(input: TicketInput!)` and its `TicketInput`/`TicketResult`
    # shapes below are schema-introspection-VERIFIED against the local dev
    # Glific instance (docs/glific-dev-endpoint.md) this session:
    # `TicketInput` fields are {body, contactId, remarks, status, topic,
    # userId}; `TicketResult` is {ticket: Ticket, errors: [InputError]};
    # `Ticket` includes {id, topic, body, contact, status, remarks,
    # insertedAt, updatedAt, messageNumber, user}. ADR-008/CR-002 document
    # the MECHANISM as `startContactFlow` triggering a per-type escalation
    # flow that itself raises the ticket — this client method is the
    # direct-call alternative for a case where the worker opens the ticket
    # itself rather than relying on a flow's internal ticket-creation step;
    # both are valid given the schema supports a direct `createTicket` call.
    # An actual live createTicket call (not just introspection) is deferred
    # to Phase 7's live-integration pass (test-runner, Task 16) — writing a
    # real Ticket during this build session was correctly out of scope for
    # a schema-verification step.
    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> dict[str, Any]:
        """Opens a Glific Ticket — the ONLY escalation/handoff surface this
        service uses (ADR-010 Invariant 5). Replaces tap-buddy's ad-hoc
        `adapters/review.py` dashboard entirely; no read-only ops-view
        equivalent is built.
        """
        variables = {
            "input": {
                "contactId": contact_id,
                "topic": topic,
                "body": body_text,
            }
        }
        data = await self._graphql(CREATE_TICKET_MUTATION, variables)
        result = data["createTicket"]
        if result.get("errors"):
            raise GlificApiError(f"createTicket errors: {result['errors']}")
        ticket: dict[str, Any] = result["ticket"]
        return ticket


def _parse_expiry(expiry_time: str | None) -> float:
    """Glific returns token_expiry_time as an ISO8601 string. Falls back to
    "now + 20 minutes" if the field is missing/unparseable.
    """
    if not expiry_time:
        return time.time() + 20 * 60
    try:
        import datetime

        dt = datetime.datetime.fromisoformat(expiry_time.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return time.time() + 20 * 60
