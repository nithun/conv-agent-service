---
type: "Reference"
title: "Delivery — `app/delivery/`"
openwiki_generated: true
---

# Delivery — `app/delivery/`

The outbound side: `GlificClient` (`app/delivery/glific_client.py:110`), an httpx client
(10s timeout) that talks to Glific's GraphQL API.

## Auth
- `POST /api/v1/session` (`:54`) → `access_token`, sent as a **bare** `authorization: <token>`
  header (no `Bearer`).
- Renew at `/api/v1/session/renew` (`:55`) with a 30s expiry safety margin (`:96`);
  `_graphql` retries once on 401 (`:195`).
- GraphQL endpoint: `POST /api` (`:56`).

## Exact mutations used

| Mutation | Defined | Wrapper |
|---|---|---|
| `createAndSendMessage(input: MessageInput!)` | `:58` | `send_text` (`:208`), `send_audio` (`:237`) |
| `startContactFlow(flowId, contactId)` | `:67` | `start_contact_flow` (`:262`) |
| `createMessageMedia(input: MessageMediaInput!)` | `:76` | `register_media` (`:227`) |
| `createTicket(input: TicketInput!)` | `:85` | `create_ticket` (`:295`) — the human-handoff surface |

> **`sendMessage` does not exist** in the live Glific schema. The client uses
> `createAndSendMessage`, and there is a regression guard so it can never regress
> (`:17`; test `tests/delivery/test_glific_client.py::test_never_calls_send_message_regression_guard`).

## Local-dev host-header override
`host_header_override` (`:117`–`:134`, opt-in via `GLIFIC_HOST_HEADER_OVERRIDE`) supports
local dev only (F-021) — never set in production.

## Where sends come from
`create_ticket` is the single handoff surface used by
[escalation](worker.md#distress-handling-distress_gatepy-escalationpy). Normal replies
are sent by the [worker](worker.md)'s `_deliver_reply`. `startContactFlow` is only ever
called after [core](core.md) has *stashed* a flow decision — `core` itself never calls
Glific.

Related: [external-contracts](external-contracts.md) · [worker](worker.md)
