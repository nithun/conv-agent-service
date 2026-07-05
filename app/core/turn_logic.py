"""Turn orchestration (CR-004 Task 7): read state -> call LLM with tools ->
candidate reply -> guardrail pass -> on distress, escalate instead of
delivering -> on completion signal, call save_submission -> update state.

Ported from `tap-buddy/src/tap_buddy/core/turn_logic.py`. Every call site
that assumed tap-buddy's single Vertex client now resolves a `MODEL_ROUTES`
key via `ModelRouter` (Task 1) — no call site constructs a provider client
directly (R-1, AC-1). No FastAPI/Redis/Glific imports (isolation, Task 19
discipline) — the `ModelRouter` and `lms_client`-like object are passed in
by the caller (the worker, Task 7/8).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.config.language_map import resolve_locale
from app.core.guardrails_checks import (
    detect_distress,
    detect_doubt_signal,
    detect_off_turn_signal,
    run_guardrails,
)
from app.core.prompts import build_system_prompt
from app.core.state_machine import (
    EscalationReason,
    SessionData,
    SessionState,
    can_transition,
    escalate,
    record_asr_failure,
    record_reexplanation,
    transition,
)
from app.llm.types import ModelResponse, ToolCall
from app.tools.definitions import (
    GET_NEXT_CONTENT_TOOL,
    GET_QUIZ_QUESTION_DETAILS_TOOL,
    SAVE_SUBMISSION_TOOL,
)
from app.tools.lms_client import LmsApiError

logger = logging.getLogger("app.core.turn_logic")

FALLBACK_REPLY = "Sorry, I'm having a little trouble right now. Let's try again in a moment."
DISTRESS_CLOSE_REPLY = (
    "Thank you for telling me. I'm going to get a grown-up to help you with this right now. "
    "You did great today."
)
CLARIFICATION_REPLY = "I didn't catch that clearly. Can you say it once again?"
ASR_FALLBACK_REPLY = "Let's switch to typing for now — you can type your reply instead."
UNRESOLVED_DOUBT_ESCALATION_REPLY = (
    "It looks like this one's tricky today. I'm going to get a grown-up to help explain it "
    "properly. You did great trying!"
)

# The MODEL_ROUTES key for this main turn loop — resolved via ModelRouter,
# never a hardcoded model string (L-004/AC-1). See app/llm/routes.py.
TAP_BUDDY_TURN_ROUTE = "tap_buddy_turn"


class ModelRouterLike(Protocol):
    """Structural type for the ModelRouter this module needs — avoids a
    hard import-time dependency loop and keeps this module trivially
    testable with a fake router.
    """

    async def generate(
        self,
        route_name: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse: ...


@dataclass
class TurnOutcome:
    reply_text: str
    session: SessionData
    escalated: bool = False
    tool_calls_made: list[str] | None = None
    advanced_to: SessionState | None = None


def _tools_for_state(state: SessionState) -> list[dict[str, Any]]:
    """Fixed tool set bound to state — the model may only call
    get_next_content at content_intro, get_quiz_question_details only
    during doubt_clearing, save_submission only when a completion signal
    is plausible (activity_in_progress/redirect).
    """
    if state == SessionState.CONTENT_INTRO:
        return [GET_NEXT_CONTENT_TOOL]
    if state == SessionState.DOUBT_CLEARING:
        return [GET_QUIZ_QUESTION_DETAILS_TOOL]
    if state in (SessionState.ACTIVITY_IN_PROGRESS, SessionState.REDIRECT):
        return [SAVE_SUBMISSION_TOOL]
    return []


def _progress_state_text(session: SessionData) -> str:
    return session.state.value


def _today_content_text(session: SessionData) -> str:
    if session.content_reference:
        ref = session.content_reference
        # Live LMS puts the human-readable name in content_id and leaves
        # title empty — prefer whichever is populated.
        label = ref.get("title") or ref.get("content_id") or "today's activity"
        ctype = ref.get("content_type")
        return f"{label} ({ctype})" if ctype else str(label)
    return "not yet fetched"


# Deterministic state-advance policy, applied after a successful
# (non-escalated, non-fallback) turn — driven by which tool actually ran,
# never by trusting the LLM's own sense of progress.
_ADVANCE_ON_TOOL: dict[str, SessionState] = {
    "get_next_content": SessionState.ACTIVITY_IN_PROGRESS,
    "save_submission": SessionState.CLOSE,
}


def _maybe_advance_state(session: SessionData, tool_names: list[str]) -> SessionState | None:
    for tool_name in tool_names:
        target = _ADVANCE_ON_TOOL.get(tool_name)
        if target is not None and can_transition(session.state, target):
            transition(session, target)
            return target
    return None


async def _execute_tool_call(
    call: ToolCall, session: SessionData, lms_client: Any
) -> tuple[str, dict[str, Any] | None]:
    """Executes exactly one of the 3 bound LMS tools. Returns (name, result_summary).
    `result_summary` is stashed back on session state where relevant (e.g.
    content_reference) so the reply reflects the tool's REAL return value
    (L-005).
    """
    if call.name == "get_next_content":
        content = await lms_client.get_next_content(call.arguments.get("student_id", ""))
        if content is None:
            return call.name, None
        summary = {
            "content_id": content.content_id,
            "title": content.title,
            "content_type": content.content_type,
        }
        session.content_reference = summary
        return call.name, summary

    if call.name == "get_quiz_question_details":
        details = await lms_client.get_quiz_question_details(
            call.arguments.get("question_id", ""), call.arguments.get("language", "English")
        )
        if details is None:
            return call.name, None
        # Deliberately never include a "correct_option" key — the client
        # type has none (F-011/F-012).
        return call.name, {"hint": details.hint, "question_text": details.question_text}

    if call.name == "save_submission":
        result = await lms_client.save_submission(
            call.arguments.get("student_id", ""),
            call.arguments.get("content_id", ""),
            call.arguments.get("submission", ""),
        )
        return call.name, {"success": result.success, "status": result.status}

    raise ValueError(f"Unbound tool call: {call.name}")


def _maybe_enter_or_advance_doubt_clearing(
    session: SessionData, user_input: str
) -> EscalationReason | None:
    """Deterministic doubt-clearing transition, applied before any LLM
    call — driven by a keyword/intent check (`detect_doubt_signal`), never
    by trusting the LLM's own sense of whether the child understood.
    """
    has_doubt_signal = detect_doubt_signal(user_input)

    if session.state == SessionState.ACTIVITY_IN_PROGRESS and has_doubt_signal:
        if can_transition(session.state, SessionState.DOUBT_CLEARING):
            transition(session, SessionState.DOUBT_CLEARING)
        return None

    if session.state == SessionState.DOUBT_CLEARING:
        if has_doubt_signal:
            return record_reexplanation(session)
        if can_transition(session.state, SessionState.ACTIVITY_IN_PROGRESS):
            transition(session, SessionState.ACTIVITY_IN_PROGRESS)
        return None

    return None


async def run_turn(
    *,
    session: SessionData,
    user_input: str,
    model_router: ModelRouterLike,
    lms_client: Any,
    is_off_turn: bool = False,
) -> TurnOutcome:
    """Orchestrates exactly one conversational turn. Caller (the worker) is
    responsible for: resolving user_input from text or ASR, and delivering
    reply_text via the Glific client.
    """
    locale = resolve_locale(session.identity.tap_language)

    # Pre-LLM distress check — deterministic, runs before any model call.
    # Highest-priority short-circuit: takes precedence over off-turn/doubt.
    # BACKSTOP (BR-003): the ingress-layer top-level distress gate
    # (`app/ingress/glific_webhook.py`) is the PRIMARY check now — it runs
    # before identity_confirmation, greeting, and mode_choice, all of which
    # return before ever reaching this function. This check remains as
    # defense-in-depth for any turn that does reach run_turn
    # (activity_in_progress, doubt_clearing, etc.).
    if user_input and detect_distress(user_input, session.identity.tap_language or "English"):
        escalate(session, EscalationReason.DISTRESS)
        return TurnOutcome(reply_text=DISTRESS_CLOSE_REPLY, session=session, escalated=True)

    # Pre-LLM off-turn check — deterministic keyword match, never
    # prompt-only. Short-circuits to the scripted acknowledge-then-redirect
    # reply; no LLM call, no state advance.
    if is_off_turn or (user_input and detect_off_turn_signal(user_input)):
        return TurnOutcome(reply_text=handle_off_turn(session), session=session)

    # Pre-LLM doubt-clearing transition — deterministic, applied before the
    # LLM call so the cap/escalation logic is reachable from the live
    # pipeline, not just unit-tested in isolation.
    doubt_escalation = _maybe_enter_or_advance_doubt_clearing(session, user_input)
    if doubt_escalation is not None:
        return TurnOutcome(
            reply_text=UNRESOLVED_DOUBT_ESCALATION_REPLY, session=session, escalated=True
        )

    tool_names: list[str] = []
    tool_summaries: list[str] = []

    # Deterministic content prefetch: entering content_intro MUST fetch
    # today's content — leaving it to the LLM's tool whim produced generic
    # no-content replies live in tap-buddy's build. Same stash shape and
    # advance semantics as the get_next_content tool path.
    if session.state == SessionState.CONTENT_INTRO and not session.content_reference:
        try:
            content = await lms_client.get_next_content(session.identity.student_id)
        except LmsApiError as exc:
            logger.warning("content prefetch failed: %s", exc)
            return TurnOutcome(reply_text=FALLBACK_REPLY, session=session)
        if content is not None:
            summary = {
                "content_id": content.content_id,
                "title": content.title,
                "content_type": content.content_type,
            }
            session.content_reference = summary
            tool_names.append("get_next_content")
            tool_summaries.append(f"get_next_content -> {summary}")

    system_prompt = build_system_prompt(
        child_name=session.identity.child_name or "there",
        grade=session.identity.grade or "unknown",
        language=locale.tap_language,
        today_content=_today_content_text(session),
        progress_state=_progress_state_text(session),
    )

    conversation: list[dict[str, str]] = [{"role": "user", "content": user_input}]
    tools = _tools_for_state(session.state)

    # ModelRouter's shared generate() interface takes only (route_name,
    # messages, tools) — no separate system-prompt argument (ADR-011) — so
    # the system prompt is prepended as the first message, uniformly for
    # every provider adapter.
    messages_with_system = [{"role": "system", "content": system_prompt}, *conversation]
    result = await model_router.generate(TAP_BUDDY_TURN_ROUTE, messages_with_system, tools)
    for call in result.tool_calls:
        try:
            name, tool_summary = await _execute_tool_call(call, session, lms_client)
            tool_names.append(name)
            tool_summaries.append(f"{name} -> {tool_summary}")
        except LmsApiError as exc:
            # LMS failure mid-turn: degrade to the fallback reply rather than
            # crash the turn or invent LMS data.
            logger.warning("LMS tool call %s failed: %s", call.name, exc)
            return TurnOutcome(
                reply_text=FALLBACK_REPLY, session=session, tool_calls_made=tool_names
            )

    reply_text = result.text.strip()
    if tool_names and not reply_text:
        # The model frequently returns ONLY the function call with empty
        # text (observed live in tap-buddy's build). Run one follow-up
        # round — tool results in, no tools bound (prevents loops) — so
        # the reply reflects the REAL tool output instead of falling back.
        followup = messages_with_system + [
            {
                "role": "user",
                "content": (
                    "[system note] Tool results: "
                    + "; ".join(tool_summaries)
                    + " — now give the child your reply for this turn, "
                    "following all the rules. Do not mention tools."
                ),
            }
        ]
        followup_result = await model_router.generate(TAP_BUDDY_TURN_ROUTE, followup, [])
        reply_text = followup_result.text.strip()

    if not reply_text:
        logger.warning("model returned empty reply with no usable tool round; falling back")
    reply_text = reply_text or FALLBACK_REPLY

    allowed_fields = set()
    if session.content_reference:
        allowed_fields = {str(v) for v in session.content_reference.values()}

    guardrail_result = run_guardrails(reply_text, allowed_content_fields=allowed_fields)
    if not guardrail_result.ok:
        # One corrective regeneration ("regenerate once"): tell the model
        # which rule broke, no tools bound. If the rewrite still violates,
        # fall back — never ship a violating reply.
        failures = ", ".join(f.value for f in guardrail_result.failures)
        logger.warning("guardrails rejected reply (%s); regenerating once", failures)
        rewrite = await model_router.generate(
            TAP_BUDDY_TURN_ROUTE,
            messages_with_system
            + [
                {
                    "role": "user",
                    "content": (
                        f"[system note] Your draft reply broke these rules: {failures}. "
                        f"Draft: {reply_text!r}. Rewrite it following ALL rules — "
                        "1-3 short sentences, at most one question."
                    ),
                }
            ],
            [],
        )
        reply_text = rewrite.text.strip() or FALLBACK_REPLY
        recheck = run_guardrails(reply_text, allowed_content_fields=allowed_fields)
        if not recheck.ok:
            logger.warning(
                "guardrails rejected rewrite (%s); falling back",
                ", ".join(f.value for f in recheck.failures),
            )
            reply_text = FALLBACK_REPLY

    advanced_to = _maybe_advance_state(session, tool_names)

    return TurnOutcome(
        reply_text=reply_text,
        session=session,
        tool_calls_made=tool_names,
        advanced_to=advanced_to,
    )


def advance_state(session: SessionData, target: SessionState) -> SessionData:
    """Thin pass-through to state_machine.transition, kept here so callers
    orchestrating a turn only need to import turn_logic, not state_machine
    directly, for the common case.
    """
    return transition(session, target)


def handle_off_turn(session: SessionData) -> str:
    """Off-turn/unrelated input: acknowledge briefly, redirect, never
    repeat the prior instruction verbatim. Does not advance turn-relevant
    state fields (caller should not call advance_state for an off-turn reply).
    """
    return (
        "I understand — let's come back to that in a moment. "
        "Shall we continue with today's activity?"
    )


def handle_low_confidence_asr(session: SessionData) -> tuple[str, bool]:
    """Low-confidence ASR triggers a clarification turn. Returns
    (reply_text, should_fallback_to_text) — should_fallback_to_text is True
    once record_asr_failure's cap (2 consecutive) is reached.
    """
    should_fallback = record_asr_failure(session)
    if should_fallback:
        return ASR_FALLBACK_REPLY, True
    return CLARIFICATION_REPLY, False


def handle_doubt_reexplanation(session: SessionData) -> EscalationReason | None:
    """Wraps state_machine.record_reexplanation for turn-logic callers."""
    return record_reexplanation(session)
