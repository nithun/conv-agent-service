---
title: External contracts (Glific + Frappe)
okf_version: "0.1"
kind: reference
updated: 2026-08-07
---

# External contracts

Consolidated view of everything the service exchanges with the outside world. Deeper
prose lives in [../docs/INTEGRATION.md](../docs/INTEGRATION.md) and
[../docs/GLIFIC-FLOW-INTEGRATION.md](../docs/GLIFIC-FLOW-INTEGRATION.md).

## Inbound — Glific → service
- **Route:** `POST /webhook/glific`, HMAC-SHA256 signed
  (`X-Glific-Signature: t=…,v1=…` over `"<timestamp>.<raw_body>"`).
  See [ingress](ingress.md).
- **Payload:** parsed by `GlificWebhookPayload` — tolerant of `@`-expression-absent
  fields, simulator `_N` phone suffixes, and missing `message_id`.
- **Media:** inbound voice notes are transcribed in the worker; outbound TTS is served
  from `GET /media/{token}` (requires `PUBLIC_BASE_URL`).

## Outbound — service → Glific (GraphQL)
See [delivery](delivery.md) for wrappers.

| Mutation | Use |
|---|---|
| `createAndSendMessage` | text + audio replies |
| `createMessageMedia` | register a media object before sending audio |
| `startContactFlow` | hand the contact into a named Glific flow |
| `createTicket` | human handoff (the only escalation surface) |

`sendMessage` is **not** a real mutation and is regression-guarded against.

## Outbound — service → TAP LMS / Frappe (REST)
`POST /api/method/<method>`, auth = `api_key` in body **and**
`Authorization: token <frappe_token>` header. Exactly 8 whitelisted methods (see
[tools](tools.md)):
`get_student_by_phone_and_name`, `get_next_content`, `get_quiz_question_details`,
`start_quiz`, `save_submission`, `register_test_student`, `reset_test_student`,
`get_test_student_by_phone`. Writes are limited to quiz/submission and test-student
provisioning — **Frappe is the source of truth**
([state authority](design-principles.md#2-state-authority--frappe--tap-lms)).

Answer keys never cross the boundary into a prompt: `correct_option*` is scrubbed and
absent from the returned types.

## LLM egress
Via the self-hosted **LiteLLM gateway → Vertex AI `gemini-2.5-flash`** (project
`tap-buddy-mvp`); every call traced in **self-hosted Langfuse**. The `OPENROUTER_*`
names are legacy — see the [llm caveat](llm.md#-naming-caveat-read-this-first).

## Redis
Streams (per-contact + escalation), dedupe/idempotency keys, session hashes, and the
worker heartbeat. Keys enumerated in [queue](queue.md) and [worker](worker.md).

Related: [ingress](ingress.md) · [delivery](delivery.md) · [tools](tools.md)
