---
title: Tools (TAP LMS / Frappe client + schemas)
okf_version: "0.1"
kind: module
updated: 2026-08-07
source: app/tools/
---

# Tools — `app/tools/`

The integration with the system of record (TAP LMS / Frappe), the LLM tool schemas,
and the speech client.

## TAP LMS client (`lms_client.py`)
`TapLmsClient` (`:229`) exposes **exactly 8 whitelisted methods** — do not add a 9th
without a change request. Each calls `POST /api/method/<method>` with the `api_key` in
the body **and** an `Authorization: token <frappe_token>` header (`:253`). Dotted-path
constants at `:73`–`:84`.

| Method | File | R/W | Notes |
|---|---|---|---|
| `get_student_by_phone_and_name` | `:276` | read | |
| `get_next_content` | `:304` | read | |
| `get_quiz_question_details` | `:320` | read | no `correct_option` on the returned type |
| `start_quiz` | `:342` | **write** | creates/resumes an attempt |
| `save_submission` | `:382` | **write** | |
| `register_test_student` | `:400` | **write** | test provisioning |
| `reset_test_student` | `:446` | **write** | test provisioning |
| `get_test_student_by_phone` | `:474` | read | server-scoped to test batch `BT00000025` |

**Answer-leak scrub:** `_scrub_answer_leak` drops `correct_option*` keys (`:196`); the
`QuizQuestionDetails` / `QuizStartResult` types carry no `correct_option` field (a
type-level guard, not just runtime). Errors surface as `LmsApiError` (`:87`).

The exact Frappe endpoints and their dotted paths are consolidated in
[external-contracts](external-contracts.md).

## Tool schemas (`definitions.py`)
The tool definitions handed to the model:
`GET_NEXT_CONTENT_TOOL` (`:16`), `GET_QUIZ_QUESTION_DETAILS_TOOL` (`:26`),
`SAVE_SUBMISSION_TOOL` (`:42`), and `start_glific_flow_tool(registry_names)` (`:62`) —
the last is **Glific-facing, not Frappe**, and builds its enum fresh from the
[flow registry](configuration.md).

## Speech client (`speech_client.py`)
`SpeechClient` (`:36`) — Google STT/TTS with `ASR_CONFIDENCE_THRESHOLD = 0.5` (`:17`).
Lazy imports keep the module importable even without the GCP speech libs installed (so
the worker degrades to text-only rather than failing to boot).

Related: [core](core.md) · [delivery](delivery.md) · [external-contracts](external-contracts.md)
