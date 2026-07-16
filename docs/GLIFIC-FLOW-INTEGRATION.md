# Building Glific flows against tap-agent-service

**Audience:** a Glific flow author — human or flow-creation agent — building
flows that integrate with tap-agent-service (the conversational-AI service).
This doc is self-contained: everything you need to author, publish, and
verify the flows lives here. The service-side view (queue, worker, delivery
client, env vars) is `docs/INTEGRATION.md`.

The sample flows in §4 are the **real, live-verified definitions** exported
from the running integration (2026-07-16, post-F-019 cutover: 47/47 webhook
calls 200, full conversation packs passing) — not sketches.

---

## 1. The service API, from a flow's point of view

One endpoint matters to a flow:

```
POST http://<service-host>:8001/webhook/glific
Content-Type: application/json
```

(Local dev: `http://localhost:8001/webhook/glific`. Staging/prod: the
deployed base URL — never hardcode an environment's host into a reusable
flow doc; the URL is per-environment flow configuration.)

### Webhook node body template

```json
{
 "contact_id": "@contact.id",
 "phone": "@contact.phone",
 "contact_name": "@contact.name",
 "text": "@results.<your_wait_result_name>"
}
```

Field notes:

| Field | Template value | Why |
|---|---|---|
| `contact_id` | `@contact.id` | Required. Keys the service's session + ordering. |
| `phone` | `@contact.phone` | Required. The service's LMS identity lookup key. |
| `contact_name` | `@contact.name` | Optional, cosmetic. |
| `text` | `@results.<wait_result_name>` — or a **sentinel** like `"::flow_start::"` on an entry flow that has no wait before the webhook | The user's message. `@input.text` does **not** exist in Glific webhook bodies (live-verified) — text only reaches a webhook via a preceding wait-router's result. |
| `media_url` | `@results.<wait_result_name>.attachment`-style expressions are **not currently forwarded** by Glific's flow webhook (known platform gap, tracked as F-013) | Voice notes through a flow don't reach the service yet; also see BR-004 on the service side. |

Unresolved flow variables render **literally** (`"@results.foo"` arrives as
that string). The service treats any leftover `@…` string as absent — so a
misconfigured body fails soft, but your flow silently loses the user's text.
Always double-check the `result_name` spelling matches your wait node.

### Response contract

The service **fast-acks** (<200ms; Glific's webhook budget is 4s — wide
headroom). The ack means "received and queued", **not** "replied":

- The AI's actual reply arrives **asynchronously** — the service sends it
  itself via `createAndSendMessage`. It is **not** in the webhook response
  body, so there is nothing to interpolate into a `send_msg` node. **Do not
  put a send_msg node after the webhook expecting the AI reply.**
- The only thing your flow should route on is the webhook **category**:
  `Success` (HTTP < 400) / `Failure`. Route `Failure` to a fallback (e.g. a
  canned "we'll get back to you" message or a human-handoff flow) so an
  outage degrades gracefully.
- Idempotent: Glific's Oban retries (max 2) of the same delivery are safe —
  the service dedupes.

Possible 200 bodies (informational; flows normally ignore them):
`{"status":"ok"}`, `{"status":"ok","duplicate":true}`,
`{"status":"ok","escalated":true}` (the service's distress gate tripped —
the service handles the comfort reply + staff ticket itself).

## 2. Integration scenarios (the catalog)

| # | Scenario | Who owns the conversation | Status today |
|---|---|---|---|
| S1 | **Full conversation handoff** — entry flow + turn-relay flow pair; the AI owns every turn until close/escalation | tap-agent-service | **LIVE** (the §4 samples) |
| S2 | **Agent-embedded micro-flows** — the AI conversation triggers small, terminal Glific flows for structured moments | tap-agent-service, delegating | Primitives live (`startContactFlow`); per-flow design needed |
| S3 | **AI-node-in-a-flow** — a Glific-owned flow calls the service for one smart step, then continues | Glific flow | Async-reply shape works today; a synchronous "answer in `@results`" variant needs a service CR |
| S4 | **Scheduled / broadcast nudges** — a Glific trigger or campaign starts a flow per contact whose webhook asks the AI for a personalized message | Glific (initiation) | Needs service-side nudge route (parent WBS 1.17) |
| S5 | **Hybrid hand-back** — S1 conversation that, on close, starts a wrap-up flow (rating, next-steps menu) | Handoff both ways | Primitives live; wrap-up flow design needed |

### S1 — Full conversation handoff (the pattern to copy)

Two flows, split because of a hard Glific constraint (§5.1):

- **Entry flow** ("TAP Buddy" pattern): keyword-triggered. One
  `call_webhook` node sending the sentinel text `"::flow_start::"`. No wait,
  no send_msg — the AI's greeting comes from the service. Flow ends.
- **Turn-relay flow** ("TAP Buddy Turn" pattern): `wait_for_response`
  (result_name e.g. `child_reply`) → `call_webhook` (text =
  `@results.child_reply`) → flow ends. The **service re-arms this flow
  after every reply** (`startContactFlow`), so each turn is a fresh flow
  run. The service skips the re-arm after terminal states (`close` /
  `escalated`) — the keyword is the way back in.

**Flow-author handoff requirement:** after publishing the relay flow,
report its **numeric flow id** to the service operator — it must be set as
`GLIFIC_TURN_FLOW_ID` in the service's environment or every conversation
dies after turn 1. (Get it from the flow editor URL or
`SELECT id FROM flows WHERE name = '<relay flow name>'`.)

### S2 — Agent-embedded micro-flows ("small, small flows")

The inverse composition: the AI owns the conversation but delegates
structured moments to purpose-built Glific flows, triggered by the service
via `startContactFlow`. Good fits:

- **Escalation/handoff flows** — a distress or unresolved-doubt hit starts
  an `SP_Distress_Handoff`-style flow (scripted comfort message, staff
  group notify, ticket metadata) instead of, or alongside, the service's
  direct `createTicket`.
- **Structured collectors** — interactive quick-reply/list menus (mode
  choice, quiz options, consent), which WhatsApp renders as buttons — a
  better UX than free text for closed questions.
- **Compliance/utility flows** — opt-out, language change, re-registration.

**Design rules for an embeddable micro-flow** (all consequences of the
live-verified kill semantics, §5.2):

1. **Short and terminal**: do its one job in 1–3 nodes and END. No loops,
   no long waits (set a short `expire_after_minutes`).
2. **Hand control back explicitly**: its LAST wait's result should flow
   into a `call_webhook` back to the service (body `text` =
   `@results.<its_result>`, plus a sentinel prefix if the service needs to
   know which micro-flow answered, e.g. `"::mode_choice:: @results.choice"`)
   — OR, if it collects nothing, end plainly and let the service's next
   re-arm resume the relay loop.
3. **Never assume the relay survives it**: starting a micro-flow HARD-KILLS
   whatever flow context was active (including a parked relay). The service
   must re-arm the relay after the micro-flow completes its webhook
   round-trip.
4. Service-side counterpart: each micro-flow's numeric id becomes a config
   value (like `GLIFIC_TURN_FLOW_ID`); the service calls
   `start_contact_flow(<id>, contact_id)` at the right conversation moment.
   New micro-flow hooks in the service are governed changes (CR in the
   parent workspace).

### S3 — AI-node-in-a-flow

A Glific-owned flow (registration, survey, campaign) wants one smart step —
classify a free-text answer, generate one personalized line, grade a
response. Today's shape: `wait_for_response` → `call_webhook` to the
service → route on Success/Failure → **the service sends the AI output as
its own message** → flow continues with its next structured step.

Limitation to design around: the flow **cannot read the AI's text** from
`@results.<name>.<key>` today, because the service acks before the AI runs.
If a flow needs the answer inside flow variables (e.g. to branch on an AI
classification), that requires a synchronous endpoint variant on the
service (`POST /webhook/glific/sync` returning the result in the response
body within the 4s budget) — a small, well-defined CR; request it via the
parent workspace before authoring such a flow.

### S4 — Scheduled / broadcast nudges

Glific triggers (time-based) or campaigns start a flow for a cohort; the
flow's webhook asks the service to compose a personalized nudge
(non-submitter reminder in the student's language). Constraint: outside the
24h customer-service window (§5.3) WhatsApp only allows **HSM template**
messages — the AI can pick/fill a template's variables but cannot free-form.
Service-side nudge route is parent-workspace WBS 1.17 (not yet built);
author these flows only after that lands.

### S5 — Hybrid hand-back

An S1 conversation that ends with structure: when the session closes, the
service (instead of just not re-arming) starts a wrap-up flow — rating
buttons, "come back tomorrow" schedule, sibling handoff. Same rules as S2
micro-flows; the hook point (on-close flow id) is one more config value +
one `start_contact_flow` call at the close transition.

## 3. Authoring + publishing checklist

1. Build the flow(s) in the Glific flow editor (or POST the definition to
   `/flow-editor/revisions` and publish via the `publishFlow` GraphQL
   mutation — both live-verified; publish handles cache invalidation).
2. Webhook node: URL per environment; method POST; headers
   `Content-Type: application/json` (+ `Accept`); body per §1; give the
   node an explicit `result_name`.
3. Route on `@results.<result_name>.category` — `Success` / `Failure`
   (has_only_text case). Wire `Failure` somewhere deliberate.
4. Entry flow: attach the keyword trigger. Relay flow: **no keyword** —
   only the service starts it.
5. Publish. Remember: only the **published** revision serves traffic;
   a stale draft republished later silently reverts your URL/body.
6. Report to the service operator: relay flow's numeric id
   (`GLIFIC_TURN_FLOW_ID`), plus any micro-flow ids.
7. Verify end-to-end: send the keyword from a test contact, confirm
   (a) `webhook_logs.status_code < 400` for your flow's node,
   (b) the AI reply arrives, (c) a SECOND message also gets an AI reply —
   (c) is the relay re-arm working; if turn 2 falls to the org's
   out-of-office autoreply, the relay id isn't configured on the service.

## 4. Sample flows (real, live-verified definitions)

These are the actual published definitions running the live integration
(exported 2026-07-16; URL shown is the local-dev value). Import them as-is
via `/flow-editor/revisions` + publish, or rebuild node-by-node.

### 4.1 Entry flow — "TAP Buddy" (keyword-triggered)

Structure: one node — `call_webhook` (text = sentinel `::flow_start::`) +
Success/Failure router. The AI sends the greeting; the flow just ends.

```json
{
  "name": "TAP Buddy",
  "type": "messaging",
  "uuid": "4bc8d0fc-6cc6-4f3f-b3e0-c451f7c9574f",
  "spec_version": "13.1.0",
  "language": "base",
  "expire_after_minutes": 1440,
  "vars": [],
  "localization": {},
  "nodes": [
    {
      "uuid": "1fcffac8-a68f-42f5-bd3e-fc34403533a6",
      "actions": [
        {
          "type": "call_webhook",
          "uuid": "fe770be7-dc0a-4901-9a0e-270a31fe8a02",
          "method": "POST",
          "url": "http://localhost:8001/webhook/glific",
          "headers": {
            "Accept": "application/json",
            "Content-Type": "application/json"
          },
          "body": "{\n \"contact_id\": \"@contact.id\",\n \"phone\": \"@contact.phone\",\n \"contact_name\": \"@contact.name\",\n \"text\": \"::flow_start::\"\n}",
          "result_name": "tapbuddy_start"
        }
      ],
      "router": {
        "type": "switch",
        "operand": "@results.tapbuddy_start.category",
        "cases": [
          {
            "uuid": "17abba39-d853-45b7-b12a-a82fca2f482b",
            "type": "has_only_text",
            "arguments": ["Success"],
            "category_uuid": "77a2141e-728b-4530-ac82-e90abd52de5c"
          }
        ],
        "categories": [
          {
            "uuid": "77a2141e-728b-4530-ac82-e90abd52de5c",
            "name": "Success",
            "exit_uuid": "cd4750d9-42a2-48f5-aa4a-18ecd37c9d86"
          },
          {
            "uuid": "950cd0c6-9660-49b0-8d9d-0c7014b1c9e0",
            "name": "Failure",
            "exit_uuid": "c27b6dd9-9352-43e6-bfeb-d446df62cb4c"
          }
        ],
        "default_category_uuid": "950cd0c6-9660-49b0-8d9d-0c7014b1c9e0"
      },
      "exits": [
        { "uuid": "cd4750d9-42a2-48f5-aa4a-18ecd37c9d86", "destination_uuid": null },
        { "uuid": "c27b6dd9-9352-43e6-bfeb-d446df62cb4c", "destination_uuid": null }
      ]
    }
  ],
  "_ui": {
    "nodes": {
      "1fcffac8-a68f-42f5-bd3e-fc34403533a6": {
        "type": "execute_actions",
        "position": { "top": 80, "left": 120 }
      }
    }
  }
}
```

Plus, outside the definition: a **keyword trigger** (e.g. `buddy`) attached
to this flow in Glific's trigger settings.

### 4.2 Turn-relay flow — "TAP Buddy Turn" (service-started, no keyword)

Structure: `wait_for_response` (result_name `child_reply`) →
`call_webhook` (text = `@results.child_reply`) → end. The service re-arms
this flow after every reply.

```json
{
  "name": "TAP Buddy Turn",
  "type": "messaging",
  "uuid": "6064007c-44bf-4b56-8ecf-98ae2d008d52",
  "spec_version": "13.1.0",
  "language": "base",
  "expire_after_minutes": 1440,
  "vars": [],
  "localization": {},
  "nodes": [
    {
      "uuid": "410074e0-f19c-4a1d-b891-00b1d7da7bef",
      "actions": [],
      "router": {
        "type": "switch",
        "wait": { "type": "msg" },
        "operand": "@input.text",
        "result_name": "child_reply",
        "cases": [],
        "categories": [
          {
            "uuid": "120444af-123f-4e0e-be26-b853f5e84e74",
            "name": "All Responses",
            "exit_uuid": "80359d34-6205-4b01-935a-d32065f71efc"
          }
        ],
        "default_category_uuid": "120444af-123f-4e0e-be26-b853f5e84e74"
      },
      "exits": [
        {
          "uuid": "80359d34-6205-4b01-935a-d32065f71efc",
          "destination_uuid": "3dc668bc-a0ba-44ae-99d6-788f56b5a4b1"
        }
      ]
    },
    {
      "uuid": "3dc668bc-a0ba-44ae-99d6-788f56b5a4b1",
      "actions": [
        {
          "type": "call_webhook",
          "uuid": "89676165-1dd3-4cf6-94be-104445508509",
          "method": "POST",
          "url": "http://localhost:8001/webhook/glific",
          "headers": {
            "Accept": "application/json",
            "Content-Type": "application/json"
          },
          "body": "{\n \"contact_id\": \"@contact.id\",\n \"phone\": \"@contact.phone\",\n \"contact_name\": \"@contact.name\",\n \"text\": \"@results.child_reply\"\n}",
          "result_name": "tapbuddy_turn"
        }
      ],
      "router": {
        "type": "switch",
        "operand": "@results.tapbuddy_turn.category",
        "cases": [
          {
            "uuid": "46f9838f-2ff7-4e8c-b624-42c5826fe44a",
            "type": "has_only_text",
            "arguments": ["Success"],
            "category_uuid": "830fcafe-5389-4535-a9b9-a239f8844e63"
          }
        ],
        "categories": [
          {
            "uuid": "830fcafe-5389-4535-a9b9-a239f8844e63",
            "name": "Success",
            "exit_uuid": "43a2e51c-c4c1-41c2-8f63-bcbafca2ac5f"
          },
          {
            "uuid": "0cf141ba-0726-4309-bd22-b3b59411dbe1",
            "name": "Failure",
            "exit_uuid": "7ae57e4c-b8be-41db-9330-f047d804c958"
          }
        ],
        "default_category_uuid": "0cf141ba-0726-4309-bd22-b3b59411dbe1"
      },
      "exits": [
        { "uuid": "43a2e51c-c4c1-41c2-8f63-bcbafca2ac5f", "destination_uuid": null },
        { "uuid": "7ae57e4c-b8be-41db-9330-f047d804c958", "destination_uuid": null }
      ]
    }
  ],
  "_ui": {
    "nodes": {
      "410074e0-f19c-4a1d-b891-00b1d7da7bef": {
        "type": "wait_for_response",
        "position": { "top": 80, "left": 120 }
      },
      "3dc668bc-a0ba-44ae-99d6-788f56b5a4b1": {
        "type": "execute_actions",
        "position": { "top": 320, "left": 120 }
      }
    }
  }
}
```

The full conversation loop these two produce:

```
keyword "buddy" → Entry flow → webhook(::flow_start::) → AI greeting
                                          service re-arms Turn flow ↴
child reply → Turn flow wait → webhook(@results.child_reply) → AI reply
                                          service re-arms Turn flow ↴
child reply → …                (loop until session close / escalation:
                                no re-arm; keyword starts fresh)
```

## 5. Pitfalls (each one live-verified, not theoretical)

1. **The 60s Oban uniqueness rule — why two flows exist at all.** Glific
   dedupes webhook jobs on (flow-context, url, action) for 60s. A single
   flow looping wait → webhook **silently drops** every repeat call. Never
   design a multi-turn AI loop inside one flow context; use the
   entry + service-re-armed-relay pair.
2. **Keyword interrupts HARD-KILL the active flow.** If the user sends
   another flow's keyword mid-conversation, Glific kills the current
   context immediately ("Last Active flow is killed as new flow has
   started") — it does not queue or resume. Design micro-flows and help
   keywords knowing they destroy a parked relay; the service's re-arm is
   what restores the loop.
3. **The 24h customer-service window.** `createAndSendMessage` (the
   service's replies) fails outside WhatsApp's 24h window with "Sorry! 24
   hrs window closed". Session messages only flow while the user is
   recently active; anything proactive/scheduled needs HSM templates (S4).
4. **Draft-revision revert trap.** Only published revisions serve traffic,
   but an operator republishing a stale draft silently reverts your webhook
   URL/body. After any republish, re-verify with the §3.7 smoke.
5. **Media doesn't traverse flow webhooks today.** Don't design voice/image
   scenarios through `call_webhook` yet (platform gap F-013 + service gap
   BR-004). Text only.
6. **The AI reply is not in `@results`.** Repeated because it's the most
   common authoring mistake: the webhook response is an ack; the AI's
   message arrives separately from the service. Flows route on category
   only (until an S3 synchronous variant exists).
7. **`expire_after_minutes`**: the relay flow's wait parks for at most this
   long (samples use 1440 = 24h, matching the WhatsApp window). A child
   replying after expiry gets the org default handler — the keyword
   restarts the conversation.
