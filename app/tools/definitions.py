"""Tool definitions bound to the 4 whitelisted LMS methods (CR-004 Task 12,
P-001). Ported from `tap-buddy/src/tap_buddy/core/tools.py`.

Fixed tool set — the model never gets free-form lesson-content generation.
Tool schemas are plain dicts (OpenAI/OpenRouter function-declaration shape,
converted to the provider-specific wrapper shape inside
`app/llm/providers/*` — see `openrouter.py::_to_openai_tools`); no
FastAPI/Glific imports.
"""

from __future__ import annotations

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
