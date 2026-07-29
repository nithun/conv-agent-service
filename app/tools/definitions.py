"""Tool definitions bound to the 4 whitelisted LMS methods (CR-004 Task 12,
P-001), plus the CR-014/ADR-015 `start_glific_flow` tool (Glific-, not
Frappe-facing — see that function's own docstring).

Fixed tool set — the model never gets free-form lesson-content generation.
Tool schemas are plain dicts (OpenAI/OpenRouter function-declaration shape,
converted to the provider-specific wrapper shape inside
`app/llm/providers/*` — see `openrouter.py::_to_openai_tools`); no
FastAPI/Glific imports.
"""

from __future__ import annotations

from typing import Any

GET_NEXT_CONTENT_TOOL = {
    "name": "get_next_content",
    "description": "Fetch the student's next assigned lesson/video/quiz content from the LMS.",
    "parameters": {
        "type": "object",
        "properties": {"student_id": {"type": "string"}},
        "required": ["student_id"],
    },
}

GET_QUIZ_QUESTION_DETAILS_TOOL = {
    "name": "get_quiz_question_details",
    "description": (
        "Fetch hint details for a quiz question (never returns the correct answer). "
        "Only use during doubt-clearing when a quiz question is in scope."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question_id": {"type": "string"},
            "language": {"type": "string"},
        },
        "required": ["question_id", "language"],
    },
}

SAVE_SUBMISSION_TOOL = {
    "name": "save_submission",
    "description": (
        "Record a completion/submission signal for the student's current content. "
        "Only call this once the child has indicated they finished the activity."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "student_id": {"type": "string"},
            "content_id": {"type": "string"},
            "submission": {"type": "string"},
        },
        "required": ["student_id", "content_id", "submission"],
    },
}

ALL_TOOLS = [GET_NEXT_CONTENT_TOOL, GET_QUIZ_QUESTION_DETAILS_TOOL, SAVE_SUBMISSION_TOOL]


def start_glific_flow_tool(registry_names: list[str]) -> dict[str, Any]:
    """CR-014/ADR-015: a 4th tool, Glific- not Frappe-facing, offered only
    when both `Settings.flow_orchestrator_enabled` is True AND the flow
    registry resolves to >=1 entry (see `app/core/turn_logic.py::_tools_for_state`).

    Built FRESH per call from the loaded registry (never a hardcoded list)
    so the `enum` always reflects the current catalog. The `enum` is
    defense-in-depth only — providers vary in how strictly they enforce a
    tool-schema enum, so the real gate is the server-side registry lookup in
    `turn_logic.py::_execute_tool_call` (a `flow_name` miss is refused,
    never forwarded to Glific — L-014's identifier-provenance discipline
    extended to a new identifier type).
    """
    return {
        "name": "start_glific_flow",
        "description": (
            "Start a registered Glific flow for this contact when their request "
            "matches one of the flows below better than a conversational answer. "
            "Only names from the enum are valid -- never invent a flow name."
        ),
        "parameters": {
            "type": "object",
            "properties": {"flow_name": {"type": "string", "enum": registry_names}},
            "required": ["flow_name"],
        },
    }
