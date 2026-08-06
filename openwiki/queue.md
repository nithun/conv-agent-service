---
title: Queue (Redis Streams)
okf_version: "0.1"
kind: module
updated: 2026-08-07
source: app/queue/
---

# Queue — `app/queue/`

Redis Streams decouple the fast-ack [ingress](ingress.md) from the [worker](worker.md).

## Keys & naming (`app/queue/settings.py`)

| Purpose | Key pattern | Note |
|---|---|---|
| Per-contact turn stream | `tap_agent:stream:contact:<id>` (`:21`) | **one stream per contact** → structural FIFO per student |
| Message dedupe | `tap_agent:dedupe:message:<id>` (`:22`) | `SET NX EX`, 24h TTL (`:13`) |
| Consumer group | `tap_agent_workers` (`:23`) | |
| Dead-letter | `tap_agent:stream:dead_letter` (`consumer.py:28`) | after `MAX_DELIVERY_ATTEMPTS = 3` (`:29`) |
| Escalation stream | `tap_agent:stream:escalation` (`escalation_queue.py:26`) | **single shared** stream, group `tap_agent_escalation_workers` (`:27`) |
| Outbound send-guard | `tap_agent:sent:<contact>:<entry>` (`idempotency.py:26`) | 24h TTL |

The one-stream-per-contact design is what guarantees a single student's turns are
processed in order.

## Enqueue side (`enqueue.py`)
- `is_duplicate_message` (`:35`) — `SET NX EX`.
- `enqueue_turn` (`:45`) — `XADD` to the contact stream.

## Consume side (`consumer.py`)
- `ensure_consumer_group` (`:32`) — `XGROUP CREATE … MKSTREAM`.
- `read_next_entry` (`:47`) — `XREADGROUP >`.
- `ack_entry` (`:76`), `dead_letter` (`:80`).

## Escalation stream (`escalation_queue.py`)
A **separate, shared** (not per-contact) stream so distress hits are drained
independently of normal turns: `enqueue_escalation` (`:38`),
`read_next_escalation_entry` (`:63`), `ack_escalation_entry` (`:90`).

## Idempotency (`idempotency.py`)
- `is_reply_already_sent` (`:48`) — read-only check **before** processing.
- `mark_reply_sent_if_not_already` (`:33`) — marked **only after** a successful send.

Together these prevent a retry from double-sending a reply. See
[design-principles](design-principles.md#7-idempotency-everywhere-a-retry-can-double-fire).

Next: [worker](worker.md) — what drains these streams.
