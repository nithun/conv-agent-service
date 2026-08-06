# OpenWiki scope — tap-agent-service

User-authored scope + priorities for the OpenWiki documentation agent.
(Pre-seeded; keep this file when you run `openwiki --init`.)

## What to document

Document the conversational-agent pipeline **top-down**, following the
request lifecycle through `app/`:

    ingress (POST /webhook/glific)
      → queue (Redis Streams)
      → worker
      → llm (providers, routing)
      → guardrails (distress / safety gates)
      → delivery (Glific replies)
      → tools (TAP LMS / Frappe calls)

## Priorities

1. The **fast-ack webhook contract** at ingress (ack first, work async).
2. The **state-authority invariant** — Frappe/TAP LMS is the source of truth;
   the service holds session/scratch state only.
3. The **guardrail / distress gates** and where they run relative to delivery.
4. Each **external contract**: Glific mutations (`createAndSendMessage`,
   `startContactFlow`, …) and the Frappe endpoints the tools call.

Cross-link to `docs/INTEGRATION.md` and `docs/GLIFIC-FLOW-INTEGRATION.md`
rather than restating them.

## Do NOT document

- Deployment secrets, tokens, or the VM's hostnames (see `.openwikiignore`).
- Runtime data under `data/`.
