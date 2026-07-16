# Integrating a messaging platform with tap-agent-service

**Audience:** anyone wiring a chat/messaging platform (WhatsApp BSP,
SwiftChat, a web chat, an IVR gateway, …) to this conversational-agent
service. Glific is the first and reference integration — every contract
below is stated generically first, with the Glific specifics as the worked
example.

**Honesty note (read this first):** the service's *architecture* is
channel-agnostic — the state machine, LLM layer, guardrails, and queue know
nothing about Glific — but today's *implementation* has exactly one channel
integration built: Glific. There is no runtime adapter registry; plugging in
a second platform means implementing the three seams in
[§6](#6-what-integrating-a-new-platform-actually-takes) (a small, bounded
amount of new code), not flipping a config value. This document specifies
the contract that code must satisfy.

---

## 1. The shape of every integration

```
                    INBOUND (platform → service)
Student ──▶ Platform ──HTTP POST /webhook/<platform>──▶ ingress (fast-ack)
                                                          │  parse → dedupe →
                                                          │  distress gate →
                                                          ▼  enqueue
                                                    Redis Stream (per contact)
                                                          │
                                                          ▼
                                                       worker
                                              state machine → LLM → guardrails
                                                          │
                    OUTBOUND (service → platform)         ▼
Student ◀── Platform ◀──platform API calls────── delivery client
                         send text / send voice note /
                         open human-handoff ticket /
                         (re-arm relay, if the platform needs it)
```

An integration therefore has two halves and an optional third:

| Half | Direction | What the platform does | What the service does |
|---|---|---|---|
| **Webhook ingress** | platform → service | POSTs every user message as JSON | fast-acks (<200ms), dedupes, enqueues |
| **Delivery client** | service → platform | exposes an API to send messages / open tickets | calls it from the worker, never from ingress |
| **Media fetch** (voice mode only) | platform → service | GETs synthesized voice notes | serves them at `GET /media/{token}` |

## 2. Inbound webhook contract

### 2.1 Endpoint

One route per platform, named for it: the Glific integration is
`POST /webhook/glific` (`app/ingress/glific_webhook.py`). A new platform
gets its own route (`POST /webhook/<platform>`) with its own thin payload
normalizer — see §6.

### 2.2 Request

`Content-Type: application/json`. The normalized message the service needs
from any platform's payload:

| Field | Required | Meaning |
|---|---|---|
| `contact_id` | yes | Platform-stable conversation/contact key. Partitions the queue (strict per-contact ordering) and keys the Redis session. |
| `phone` | yes | The student's phone number — the LMS identity lookup key (`get_student_by_phone_and_name`). Platforms without phone-based identity would need an identity-mapping decision first (see §7). |
| `message_id` | no | Platform message id, used for dedupe. **Generated (uuid4) when absent** — dedupe then only protects against true HTTP/BSP retries that resend an identical explicit id. |
| `text` | one of text/media | The user's message text. |
| `media_url` | one of text/media | Fetchable URL of an inbound voice note. Its presence switches the turn to the audio path (ASR → text → same pipeline). |
| `contact_name` | no | Display name; cosmetic. |

Glific's parser (`app/ingress/glific_payload.py`) additionally tolerates
Glific-specific quirks — nested `contact`/`message` key aliases, literal
unresolved `@`-expressions treated as absent, simulator-phone `_N` suffix
stripping. A new platform's normalizer owns its own dialect the same way.

### 2.3 Response — the fast-ack contract

The handler does **no LLM, LMS, or platform network calls inline** (L-002).
It parses, does one Redis dedupe round-trip, one deterministic distress
keyword check, one enqueue, and returns. Budget: **<200ms**; Glific's flow
webhook allows 4s, so there is deliberate headroom for any platform with a
webhook timeout ≥ ~1s.

| HTTP | Body | Meaning |
|---|---|---|
| 200 | `{"status": "ok"}` | Accepted and enqueued. **This is an ACK of receipt, not of processing** — the reply arrives later via the delivery client. |
| 200 | `{"status": "ok", "duplicate": true}` | Same `message_id` seen within 24h — acked, **not** re-enqueued. Platforms that retry deliveries can retry safely. |
| 200 | `{"status": "ok", "escalated": true}` | The deterministic distress gate tripped; the message bypasses the normal state machine and the worker performs the human-handoff path instead. |
| 400 | `{"detail": ...}` | Invalid JSON or missing required fields. Malformed input is never a 5xx. |

There are **no response-body fields the platform must interpolate** — a
platform only needs "HTTP < 400 = accepted". (Glific's flows route on the
webhook result *category* only.)

### 2.4 Semantics the platform can rely on

- **Idempotency:** dedupe on `message_id` via Redis `SETNX`, 24h TTL.
- **Ordering:** messages for one `contact_id` are processed strictly in
  order (per-contact Redis Stream + consumer group), even across worker
  instances.
- **Retry/DLQ:** a turn that fails processing is retried up to
  **3 attempts** with backoff, then lands on the dead-letter stream
  (`tap_agent:stream:dead_letter`) — never silently dropped, never blocks
  other contacts.
- **Failure isolation:** a Redis outage on the escalation-enqueue path logs
  loudly but still acks — the platform's webhook call never hangs on the
  service's internals.

### 2.5 The turn-relay requirement (the one that bit us)

The platform must deliver **every subsequent user message** in the
conversation to the webhook — not just the first one.

- Platforms with a persistent "route all messages from this conversation to
  this URL" subscription satisfy this for free. Nothing extra to build.
- **Glific does not work that way.** A flow's `call_webhook` fires once per
  flow run, and a wait→webhook loop inside one flow context is silently
  deduped by Glific's Oban job uniqueness (60s on context/url/action). So
  the Glific integration uses a **relay flow** ("TAP Buddy Turn"): it parks
  at a wait node, the student's next message resumes it, its webhook node
  POSTs to us, and the flow ends. After **every** reply, the worker re-arms
  it via `startContactFlow` (`GLIFIC_TURN_FLOW_ID`), except after terminal
  states (`close`/`escalated` — the keyword flow is the way back in). The
  re-arm runs in a `finally`, so even a failed turn never strands the
  contact with no armed relay.
- A new platform integration must state which of these two models it uses.
  If it needs re-arm-like behavior, implement `start_contact_flow` on its
  delivery client and reuse `maybe_rearm_turn_flow`
  (`app/worker/turn_processor.py`); if not, leave the relay flow id unset —
  the re-arm is a no-op when the id is empty.

## 3. Outbound delivery contract (the client a platform must provide)

The worker talks to the platform through a delivery client. There is no
formal ABC today — the contract is structural (see the `_GlificSender` /
`_FlowStarter` protocols in `app/worker/turn_processor.py`); a new
platform's client must implement the same call signatures:

| Method | Required | Used for |
|---|---|---|
| `send_text(receiver_id, body_text)` | yes | Every normal turn reply. |
| `register_media(url, caption) -> media_id` + `send_audio(receiver_id, media_url)` | voice mode only | Sending synthesized voice notes. The URL handed over is `PUBLIC_BASE_URL` + `/media/{token}` — the platform fetches it (§4). |
| `create_ticket(contact_id, topic, body_text)` | yes | The human-handoff surface (§5). Whatever "a staffed queue" means on that platform. |
| `start_contact_flow(flow_id, contact_id)` | only if the platform needs turn re-arm (§2.5) | Re-arming the relay flow. |
| `aclose()` | yes | Lifecycle. |

Behavioral requirements:

- **Raise on failure** (don't swallow): the worker's retry/DLQ machinery
  keys off exceptions. Glific's client raises `GlificApiError` on any
  GraphQL-level `errors`.
- **Own your auth internally** (token acquisition/renewal inside the
  client). Glific's does phone+password → `/api/v1/session` → token renewal.
- **Never log message bodies, prompts, or credentials** (L-007). Langfuse
  is the sanctioned capture surface.

The Glific implementation (`app/delivery/glific_client.py`) maps these onto
GraphQL: `createAndSendMessage`, `createMessageMedia`, `startContactFlow`,
`createTicket` — the **live-verified** mutation names (the architecture
docs' `sendMessage`/`startFlow` shorthand does not exist in the schema;
F-003).

## 4. Media serving (voice replies)

For voice-mode turns the worker synthesizes TTS audio, stores it under
`MEDIA_DIR`, and exposes it at `GET /media/{token}` (unguessable token,
`MEDIA_TTL_SECONDS` = 24h cleanup). `PUBLIC_BASE_URL` must be set to a URL
**the platform can fetch** — e.g. the Tailscale funnel URL in local dev,
the service's public URL in staging/prod. The platform downloads the file
when the delivery client hands it the URL (Glific: `createMessageMedia`
then send).

## 5. Escalation / human handoff

Two triggers, one surface:

1. **Ingress distress gate** — deterministic keyword check (never an LLM
   judgment) on every inbound text *before* any state handling. A hit
   enqueues onto a dedicated escalation stream; the worker sends a scripted
   comfort reply and opens a ticket.
2. **In-conversation escalations** — unresolved-doubt cap and ASR-failure
   cap inside the state machine.

Every escalation calls `create_ticket` with topic one of: `"Distress
escalation"`, `"Unresolved doubt escalation"`, `"ASR failure escalation"`.
**Integration requirement:** the platform (or the org's ops setup on it)
must provide a monitored destination for these — in Glific, the Ticketing
feature with a staffed queue. An integration without a real staffed
handoff surface must not face children (CR-002 / Invariant 5).

## 6. What integrating a new platform actually takes

The three seams, in dependency order (all bounded, all unit-testable
against fakes — no live platform needed until the last step):

1. **Payload normalizer + ingress route** (`app/ingress/`): a
   `parse_<platform>_webhook_payload(raw) -> GlificWebhookPayload`-shaped
   normalizer producing the §2.2 fields, plus a `POST /webhook/<platform>`
   route reusing the existing dedupe/distress/enqueue pipeline (the body of
   `glific_webhook.py` is ~40 lines and platform-neutral except the parser
   call — lift it).
2. **Delivery client** (`app/delivery/`): implement §3 against the
   platform's API.
3. **Wiring** (`app/worker/main.py` + `app/config/settings.py`): construct
   the new client in `run_worker_loop` / `build_process_turn_fn`, add the
   platform's env vars (URL + credentials; no baked defaults — fail loudly).

Renaming note: `GlificWebhookPayload` is, in content, the channel-neutral
normalized message (nothing in it is Glific-specific); a second integration
would rename it `InboundMessage` (mechanical, high-touch — grep all call
sites, one commit, per GL-004) rather than duplicate it.

**Process requirement (parent workspace):** a new platform integration is a
PRD/CR + architecture design (AD-NNN) + architecture-reviewer verdict in the
parent planning workspace *before* the code — and per L-011/P-005, its task
list must carry a call-site reachability acceptance criterion (a live
message from the real platform reaching the worker), not just mocked-green
units. The re-arm gap this doc's §2.5 describes shipped precisely because
the mocked suite couldn't see a missing call site.

## 7. Known constraints / open edges

- **No webhook auth today.** `POST /webhook/<platform>` accepts
  unauthenticated calls (deliberate tap-buddy parity, live-verified at the
  F-019 cutover). Hardening (shared-secret header or IP allowlist) is a
  future CR on this repo — any platform needing an auth header at cutover
  time must trigger that CR first.
- **Identity is phone-based.** The LMS lookup needs `phone` (+ the child's
  name on turn 1). A platform without phone identity needs an
  identity-mapping decision (architecture-reviewer question) before
  integration.
- **Text and voice only.** Inbound images are not routed anywhere today
  (PRD-001 Phase 1c is unbuilt); `media_url` is treated as audio.
- **One org/tenant assumption locally.** Glific's `organization_id` is
  accepted but not used for multi-tenant partitioning in this service yet.

## 8. Environment variables (integration-relevant)

| Var | Half | Notes |
|---|---|---|
| `GLIFIC_API_URL` | delivery | Platform API base. No baked default; local dev uses `http://host.docker.internal:4000` in `.env` only. |
| `GLIFIC_PHONE` / `GLIFIC_PASSWORD` | delivery | Platform API credentials (never committed; `.env` only). |
| `GLIFIC_TURN_FLOW_ID` | delivery | Relay-flow re-arm id (§2.5). Empty = re-arm disabled. Local dev Glific: `26` ("TAP Buddy Turn"). |
| `GLIFIC_HOST_HEADER_OVERRIDE` | delivery | macOS-Docker-only workaround for Glific's hostname allowlist (F-021). Unset everywhere else. |
| `PUBLIC_BASE_URL` | media | The URL the *platform* uses to fetch `GET /media/{token}` (§4). |
| `MEDIA_DIR` / `MEDIA_TTL_SECONDS` | media | Voice-note storage + cleanup. |
| `REDIS_URL` | ingress+worker | Queue/session store. Client must be built with `decode_responses=True` (F-022). |
| `SESSION_TTL_SECONDS` | worker | Conversation session expiry (default 30 min). |

A new platform integration adds its own `<PLATFORM>_*` equivalents of the
first three rows.

## 9. Worked example: the Glific integration, end to end

1. Student sends the keyword (`buddy`) → Glific starts the **"TAP Buddy"**
   flow → its `call_webhook` node POSTs to `POST /webhook/glific`.
2. Service fast-acks, worker processes turn 1 (asks the child's name),
   replies via `createAndSendMessage`, then re-arms **"TAP Buddy Turn"**
   via `startContactFlow` (flow id from `GLIFIC_TURN_FLOW_ID`).
3. Every next student message resumes the parked relay flow → its webhook
   node POSTs to us → reply → re-arm again — until the session reaches
   `close` or `escalated` (no re-arm; the keyword starts a fresh session).
4. Distress at any point → scripted comfort reply + `createTicket` into
   Glific Ticketing's staffed queue.
5. Ops facts: Glific's flow-webhook budget is 4s (we ack in <200ms); its
   webhook Oban jobs retry `max_attempts: 2` on non-2xx; the webhook URL
   lives in the flow's *published* revision JSON — repoint = edit + publish
   (cutover runbook: parent workspace `docs/open-followups.md` F-019).

Verified live 2026-07-16 (F-019 cutover): 47/47 webhook calls 200, full
6-scenario conversation pack PASS, escalation tickets opened — this
document describes the integration as it demonstrably runs, not as
designed on paper.
