---
type: "Reference"
title: "Core — `app/core/`"
openwiki_generated: true
---

# Core — `app/core/`

Channel-agnostic conversation logic. **No FastAPI / Redis / Glific imports** — this is
the portable brain, unit-testable in isolation.

## State machine (`state_machine.py`)
- `SessionState` enum (`:35`) — 10 states including `LANGUAGE_CHOICE`.
- `EscalationReason` (`:53`); allowed-transitions table `_ALLOWED_TRANSITIONS` (`:66`).
- `SessionData` dataclass (`:111`); `transition` (`:189`), `escalate` (`:204`).
- Caps: `MAX_REEXPLANATIONS = 2`, `MAX_ASR_FAILURES = 2`, `MAX_LANGUAGE_CHOICE_ATTEMPTS = 1`.
- **Never writes LMS state** — conversational state only
  ([state authority](design-principles.md#2-state-authority--frappe--tap-lms)).

## The turn (`turn_logic.py`)
`run_turn` (`:337`) is the heart of a turn:
1. **Pre-LLM deterministic short-circuits** — distress (`:366`), off-turn (`:374`),
   doubt (`:380`).
2. Content prefetch (`:399`) / `start_quiz` (`:434`).
3. Build system prompt → `model_router.generate(TAP_BUDDY_TURN_ROUTE, …)` (`:477`,
   see [llm](llm.md)).
4. Execute tool calls (`_execute_tool_call:211`, see [tools](tools.md)).
5. **Guardrail pass** `run_all_validators` (`:589`) with one corrective regen
   (see [guardrails](guardrails.md)).
6. Deterministic state advance keyed on a **successful** tool result
   (`_maybe_advance_state:160`, `_tool_call_succeeded:174`).

Identifier authority: `student_id` / `content_id` come from the **session**, never the
model (`_session_authoritative_id:190`). `start_glific_flow` only **stashes a decision**
— it makes no Glific call here (`:288`–`:308`), keeping `core` free of I/O.

## Identity (`identity.py`)
`resolve_identity_by_name` (`:46`), `resolve_returning_identity_by_phone` (`:100`,
CR-013), `confirm_identity` (`:155`), `_identity_from_candidate` (`:177`, stores the raw
`tap_language` so a language-choice step can trigger).

## Detectors (`guardrails_checks.py`)
Pure functions, config-driven: `detect_distress` (`:156`, per-language patterns),
`detect_doubt_signal` (`:192`), `detect_off_turn_signal` (`:235`). Also reply-shape
checks `check_length`, `check_single_question`, `check_no_answer_leakage` (no-op today),
`check_no_fabricated_content`. The keyword detectors live here (pre-LLM routing); their
Guardrails-AI wrappers live in [guardrails](guardrails.md) (post-output validation).

## Prompts (`prompts.py`)
`build_system_prompt` (`:38`) over a verbatim template (`:10`).

Related: [worker](worker.md) · [llm](llm.md) · [guardrails](guardrails.md) · [tools](tools.md)
