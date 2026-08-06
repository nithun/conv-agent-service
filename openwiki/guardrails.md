---
type: "Reference"
title: "Guardrails — `app/guardrails/`"
openwiki_generated: true
---

# Guardrails — `app/guardrails/`

Post-output validation of the model's reply, wrapping the pure reply-shape checks from
[core](core.md) (`guardrails_checks.py`) as Guardrails-AI `Validator` classes.

## Validators (`validators.py`)
| Validator | File | Checks |
|---|---|---|
| `ReplyLength` | `:42` | reply within length budget |
| `SingleQuestion` | `:61` | at most one question per turn |
| `NoAnswerLeakage` | `:72` | no quiz answer leaked (no-op today; see below) |
| `NoFabricatedContent` | `:88` | no fabricated content ids |

`run_all_validators` (`:106`) is the **live production call site**, invoked from
`run_turn` (`app/core/turn_logic.py:589`) with one corrective regeneration if a
validator fails.

## Two layers, two locations
- **Pre-LLM keyword detectors** (`detect_distress`, `detect_doubt_signal`,
  `detect_off_turn_signal`) live in [core](core.md) — they route the turn *before* the
  model runs.
- **Post-output validators** (this module) shape the model's *output* before delivery.

## Note on answer-leakage
`NoAnswerLeakage` is a no-op at the validator layer today because answer leakage is
prevented **upstream, structurally**: the LMS client scrubs `correct_option*` keys and
the quiz result types carry no answer field (`app/tools/lms_client.py:196`, see
[tools](tools.md)). Defense-in-depth, enforced where the data enters — not left to a
prompt instruction (GL-005).

Related: [core](core.md) · [tools](tools.md) · [design-principles](design-principles.md)
