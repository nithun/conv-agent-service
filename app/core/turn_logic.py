"""Turn orchestration (CR-004 Task 7): read state -> call LLM with tools ->
candidate reply -> guardrail pass -> on distress, escalate instead of
delivering -> on completion signal, call save_submission -> update state.

Ported from `tap-buddy/src/tap_buddy/core/turn_logic.py`. Every call site
that assumed tap-buddy's single Vertex client now resolves a `MODEL_ROUTES`
key via `ModelRouter` (Task 1) — no call site constructs a provider client
directly (R-1, AC-1). No FastAPI/Redis/Glific imports (isolation, Task 19
discipline) — the `ModelRouter` and `lms_client`-like object are passed in
by the caller (the worker, Task 7/8).

Guardrail pass (code-review fix, 2026-07-05, Finding 2): the actual reply
validation call goes through `app.guardrails.validators.run_all_validators`
(the Guardrails AI validator classes, R-5/P-004/ADR-007) — NOT
`app.core.guardrails_checks.run_guardrails` (the bespoke pure-Python
aggregate, retained only as a directly-tested pure function, no longer the
production call site). Both wrap the same 4 deterministic checks; only
`run_all_validators` was actually reachable from the live turn loop before
this fix — `run_guardrails` was dead code outside its own tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.config.flow_registry import FlowRegistry
from app.config.language_map import resolve_locale
from app.config.scripted_replies import get_scripted_reply
from app.core.guardrails_checks import (
    detect_distress,
    detect_doubt_signal,
    detect_off_turn_signal,
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
from app.guardrails.validators import run_all_validators
from app.llm.types import ModelResponse, ToolCall
from app.tools.definitions import (
    GET_NEXT_CONTENT_TOOL,
    GET_QUIZ_QUESTION_DETAILS_TOOL,
    SAVE_SUBMISSION_TOOL,
    start_glific_flow_tool,
)
from app.tools.lms_client import LmsApiError

logger = logging.getLogger("app.core.turn_logic")

# CR-007 GAP 1: these five reply "constants" are no longer literal English
# strings — every call site below resolves the CURRENT scripted text via
# get_scripted_reply(key, tap_language) at the moment it's needed, keyed by
# the session's resolved language (falls back to English when
# tap_language is falsy/unrecognized/missing that key — see
# app/config/scripted_replies.py). The English copy itself lives ONLY in
# app/config/scripted_replies.yaml now (AC-2's reachability check asserts
# no literal duplicate survives here).

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
    # CR-014/ADR-015: set to the flow registry's NAME key (never a flow_id)
    # when this turn's model call resolved a valid `start_glific_flow`
    # request. The caller (app.worker.turn_processor.process_turn) is the
    # one that actually dispatches to Glific — this module stays
    # Glific-import-free (isolation invariant, tests/core/test_isolation.py).
    pending_flow_start: str | None = None


def _tools_for_state(
    state: SessionState, flow_registry: FlowRegistry | None = None
) -> list[dict[str, Any]]:
    """Fixed tool set bound to state — the model may only call
    get_next_content at content_intro, get_quiz_question_details only
    during doubt_clearing, save_submission only when a completion signal
    is plausible (activity_in_progress/redirect).

    CR-014/ADR-015: `start_glific_flow` is appended for EVERY
    run_turn-reachable state (not gated by which LMS tool that state
    already binds) whenever `flow_registry` is non-None AND resolves to
    >=1 entry — the two independent structural kill switches (AC-1): a
    `None` registry (flag off) or an empty one (no/empty
    flow_registry.yaml) both mean the tool is never added, never "added
    with an empty enum".
    """
    tools: list[dict[str, Any]]
    if state == SessionState.CONTENT_INTRO:
        tools = [GET_NEXT_CONTENT_TOOL]
    elif state == SessionState.DOUBT_CLEARING:
        tools = [GET_QUIZ_QUESTION_DETAILS_TOOL]
    elif state in (SessionState.ACTIVITY_IN_PROGRESS, SessionState.REDIRECT):
        tools = [SAVE_SUBMISSION_TOOL]
    else:
        tools = []

    if flow_registry is not None:
        registry_names = flow_registry.names()
        if registry_names:
            tools = [*tools, start_glific_flow_tool(registry_names)]

    return tools


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
    """`tool_names` must contain only SUCCESSFUL tool calls — the
    deterministic state-advance policy (AD-002, ported from tap-buddy)
    keys every advance on "a successful <tool> call", never on the call
    merely having been attempted. See `_tool_call_succeeded`.
    """
    for tool_name in tool_names:
        target = _ADVANCE_ON_TOOL.get(tool_name)
        if target is not None and can_transition(session.state, target):
            transition(session, target)
            return target
    return None


def _tool_call_succeeded(name: str, summary: dict[str, Any] | None) -> bool:
    """Whether a tool call's REAL result qualifies it to drive a state
    advance. Live-caught 2026-07-08 (CR-004 360° round-2 live pass): a
    `save_submission` returning `success=False` (a real LMS business
    failure) still advanced the session to `close` — the child was
    honestly told the save failed (L-005 held) while the session
    terminated anyway, leaving session state contradicting LMS reality
    (L-001-adjacent). Advance keys on the result, not the attempt.
    """
    if summary is None:
        return False
    if name == "save_submission":
        return summary.get("success") is True
    return True


def _session_authoritative_id(
    session_value: str | None, call: ToolCall, arg_key: str
) -> str:
    """Identifier authority (F-026): the session's value wins; the model's
    argument is a last-resort fallback that must never be silent (code-review
    H-1, 2026-07-16) — a silent fallback would reproduce F-026's exact
    symptom (invented ids rejected by the LMS) with no signal pointing at
    the cause if a state-machine regression ever left the session value
    empty.
    """
    if session_value:
        return session_value
    logger.warning(
        "%s: session has no authoritative %s; falling back to the "
        "model-supplied argument (F-026 class — expect the LMS to reject it)",
        call.name,
        arg_key,
    )
    return str(call.arguments.get(arg_key, ""))


async def _execute_tool_call(
    call: ToolCall,
    session: SessionData,
    lms_client: Any,
    flow_registry: FlowRegistry | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Executes exactly one of the bound tools (3 LMS tools + CR-014's
    `start_glific_flow`). Returns (name, result_summary). `result_summary`
    is stashed back on session state where relevant (e.g. content_reference)
    so the reply reflects the tool's REAL return value (L-005).
    """
    if call.name == "get_next_content":
        # Same identifier-authority rule as save_submission below: the
        # session's resolved student_id wins over whatever the model put in
        # the arguments (the deterministic content_intro prefetch already
        # uses the session id; this aligns the model-tool path with it).
        content = await lms_client.get_next_content(
            _session_authoritative_id(session.identity.student_id, call, "student_id")
        )
        if content is None:
            return call.name, None
        summary = {
            "content_id": content.content_id,
            "title": content.title,
            "content_type": content.content_type,
            "course_level": content.raw.get("course_level", ""),
        }
        session.content_reference = summary
        return call.name, summary

    if call.name == "get_quiz_question_details":
        # Identifier authority (F-028): the question id comes from the
        # session's stashed active quiz question (populated by the
        # deterministic start_quiz block in run_turn), never from the
        # model. UNLIKE student_id (where a model-arg fallback preserves
        # legacy behavior), a question id has NO legitimate model-supplied
        # case — so when nothing is stashed (non-quiz content, or
        # start_quiz failed) the call is REFUSED outright rather than
        # forwarded (code-review H2, 2026-07-17): a model-invented id must
        # never reach the LMS.
        question_id = (session.content_reference or {}).get("active_question_id")
        if not question_id:
            logger.warning(
                "get_quiz_question_details: no session-stashed active_question_id; "
                "refusing the tool call (model-supplied question ids are never forwarded)"
            )
            return call.name, None
        details = await lms_client.get_quiz_question_details(
            str(question_id),
            session.identity.tap_language or call.arguments.get("language", "English"),
        )
        if details is None:
            return call.name, None
        # Deliberately never include a "correct_option" key — the client
        # type has none (F-011/F-012).
        return call.name, {"hint": details.hint, "question_text": details.question_text}

    if call.name == "save_submission":
        # Identifier authority (F-026 root-cause fix, 2026-07-16):
        # student_id/content_id come from the SESSION, never from the model.
        # The real ids are not in the system prompt, so a model-supplied
        # value is by construction invented or empty — every live
        # save_submission before this fix went out with bogus ids and was
        # rejected by the LMS (the "business validation failure" F-026
        # originally attributed to seed data). Only `submission` — the
        # child's actual answer — is the model's to provide. The fallback to
        # model args (sessions with no resolved identity — shouldn't happen
        # in this state) logs loudly via _session_authoritative_id (H-1).
        result = await lms_client.save_submission(
            _session_authoritative_id(session.identity.student_id, call, "student_id"),
            _session_authoritative_id(
                (session.content_reference or {}).get("content_id"), call, "content_id"
            ),
            call.arguments.get("submission", ""),
        )
        return call.name, {"success": result.success, "status": result.status}

    if call.name == "start_glific_flow":
        # CR-014/ADR-015: identifier authority extended to a new identifier
        # type (Glific flow NAME, not an LMS record id) — L-014's discipline
        # applies unchanged. The model's `flow_name` argument is looked up
        # against the loaded registry; a miss is REFUSED here, never
        # forwarded — there is no legitimate model-invented flow name, same
        # as get_quiz_question_details' question_id refusal above. NO
        # Glific client call happens in this module (isolation invariant,
        # AC-2) — a hit only stashes the registry key for the caller
        # (app.worker.turn_processor.process_turn) to dispatch.
        flow_name = str(call.arguments.get("flow_name", ""))
        entry = flow_registry.resolve(flow_name) if flow_registry is not None else None
        if entry is None:
            logger.warning(
                "start_glific_flow: flow_name=%r not found in the loaded flow "
                "registry; refusing (model-supplied flow names are never "
                "forwarded to Glific)",
                flow_name,
            )
            return call.name, None
        return call.name, {"pending_flow_start": entry.name}

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
    flow_registry: FlowRegistry | None = None,
) -> TurnOutcome:
    """Orchestrates exactly one conversational turn. Caller (the worker) is
    responsible for: resolving user_input from text or ASR, and delivering
    reply_text via the Glific client.

    `flow_registry` (CR-014/ADR-015): the caller passes the loaded registry
    ONLY when `Settings.flow_orchestrator_enabled` is True — `None` here
    means the `start_glific_flow` tool is never offered this turn,
    regardless of what the registry file contains (kill switch #1). See
    `_tools_for_state` for kill switch #2 (an empty registry).
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
        reply_text = get_scripted_reply("distress_close", session.identity.tap_language)
        return TurnOutcome(reply_text=reply_text, session=session, escalated=True)

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
        reply_text = get_scripted_reply(
            "unresolved_doubt_escalation", session.identity.tap_language
        )
        return TurnOutcome(reply_text=reply_text, session=session, escalated=True)

    tool_names: list[str] = []
    tool_summaries: list[str] = []
    # Only SUCCESSFUL tool calls may drive a state advance (AD-002's
    # deterministic policy: "a successful <tool> call advances ...") —
    # tool_names records every attempt (TurnOutcome.tool_calls_made),
    # this list records only the ones whose real result succeeded.
    advance_eligible: list[str] = []

    # Deterministic content prefetch: entering content_intro MUST fetch
    # today's content — leaving it to the LLM's tool whim produced generic
    # no-content replies live in tap-buddy's build. Same stash shape and
    # advance semantics as the get_next_content tool path.
    if session.state == SessionState.CONTENT_INTRO and not session.content_reference:
        try:
            content = await lms_client.get_next_content(session.identity.student_id)
        except LmsApiError as exc:
            logger.warning("content prefetch failed: %s", exc)
            fallback_reply = get_scripted_reply("fallback", session.identity.tap_language)
            return TurnOutcome(reply_text=fallback_reply, session=session)
        if content is not None:
            summary = {
                "content_id": content.content_id,
                "title": content.title,
                "content_type": content.content_type,
                # course_level rides along for start_quiz (F-028) — quiz-type
                # content needs it as an argument.
                "course_level": content.raw.get("course_level", ""),
            }
            session.content_reference = summary
            tool_names.append("get_next_content")
            tool_summaries.append(f"get_next_content -> {summary}")
            advance_eligible.append("get_next_content")  # prefetch path is success-gated above

    # Deterministic quiz-question sourcing (F-028): entering doubt_clearing
    # on QUIZ-type content needs an authoritative question_id — the model
    # cannot know it (not in the prompt; the identifier-authority rule).
    # `start_quiz` is called ONCE per quiz (it is NOT a pure read: it
    # creates/resumes the StudentQuizAttempt server-side — the same call
    # the legacy Glific quiz sub-flow makes) with session-sourced ids;
    # quiz_id IS the quiz-type content's content_id (live-verified,
    # ANSWERS-TAPLMS-2026-07-17 Q3). The result is stashed on the session
    # so repeat doubt turns never re-call it. Crash-retry safety
    # (code-review H1, 2026-07-17): if the session save after this turn
    # fails and the consumer loop retries, the duplicate call is HARMLESS —
    # live-verified 2026-07-17 against ST00491459: a second start_quiz for
    # an in-progress attempt returns status "quiz_resumed" with the SAME
    # attempt id + current question, not a new attempt.
    if (
        session.state == SessionState.DOUBT_CLEARING
        and session.content_reference
        and str(session.content_reference.get("content_type", "")).lower() == "quiz"
        and not session.content_reference.get("active_question_id")
    ):
        try:
            quiz = await lms_client.start_quiz(
                session.identity.student_id,
                str(session.content_reference.get("course_level") or ""),
                str(session.content_reference.get("content_id") or ""),
            )
        except LmsApiError as exc:
            logger.warning("start_quiz failed: %s", exc)
            quiz = None
        if quiz is not None and quiz.question_id:
            session.content_reference["active_question_id"] = quiz.question_id
            session.content_reference["quiz_attempt_id"] = quiz.quiz_attempt_id
            tool_names.append("start_quiz")
            # Only answer-safe fields ever reach the model (the client has
            # already scrubbed correct_option* from the stored raw).
            tool_summaries.append(
                "start_quiz -> "
                f"{{'question_id': {quiz.question_id!r}, "
                f"'question_text': {quiz.question_text!r}}}"
            )

    system_prompt = build_system_prompt(
        child_name=session.identity.child_name or "there",
        grade=session.identity.grade or "unknown",
        language=locale.tap_language,
        today_content=_today_content_text(session),
        progress_state=_progress_state_text(session),
    )

    conversation: list[dict[str, str]] = [{"role": "user", "content": user_input}]
    tools = _tools_for_state(session.state, flow_registry)

    # ModelRouter's shared generate() interface takes only (route_name,
    # messages, tools) — no separate system-prompt argument (ADR-011) — so
    # the system prompt is prepended as the first message, uniformly for
    # every provider adapter.
    messages_with_system = [{"role": "system", "content": system_prompt}, *conversation]
    result = await model_router.generate(TAP_BUDDY_TURN_ROUTE, messages_with_system, tools)

    # CR-014/ADR-015: set once a `start_glific_flow` call resolves against
    # the registry; the caller (turn_processor.process_turn) dispatches the
    # ACTUAL Glific call — this module only stashes the decision (AC-2).
    pending_flow_start: str | None = None
    # An unregistered flow_name the model tried this turn — drives exactly
    # one corrective followup round below (AC-3), mirroring the guardrail
    # "regenerate once" shape.
    invalid_flow_name_requested: str | None = None

    for call in result.tool_calls:
        try:
            name, tool_summary = await _execute_tool_call(call, session, lms_client, flow_registry)
            tool_names.append(name)
            tool_summaries.append(f"{name} -> {tool_summary}")
            if name == "start_glific_flow":
                if tool_summary and tool_summary.get("pending_flow_start"):
                    pending_flow_start = str(tool_summary["pending_flow_start"])
                else:
                    invalid_flow_name_requested = str(call.arguments.get("flow_name", ""))
            if _tool_call_succeeded(name, tool_summary):
                advance_eligible.append(name)
        except LmsApiError as exc:
            # LMS failure mid-turn: degrade to the fallback reply rather than
            # crash the turn or invent LMS data.
            logger.warning("LMS tool call %s failed: %s", call.name, exc)
            fallback_reply = get_scripted_reply("fallback", session.identity.tap_language)
            return TurnOutcome(
                reply_text=fallback_reply, session=session, tool_calls_made=tool_names
            )

    if invalid_flow_name_requested is not None and pending_flow_start is None:
        # AC-3: exactly ONE corrective followup round — tell the model which
        # names are valid and let it either retry with a valid name or just
        # answer normally. A SECOND invalid attempt in this round is not
        # retried again; its (empty-of-a-valid-flow) text becomes the reply
        # via the normal fallback plumbing below.
        registry_names = flow_registry.names() if flow_registry is not None else []
        logger.warning(
            "start_glific_flow: model requested unregistered flow_name=%r "
            "(valid names: %s); refusing and running one corrective followup round",
            invalid_flow_name_requested,
            registry_names,
        )
        corrective = await model_router.generate(
            TAP_BUDDY_TURN_ROUTE,
            messages_with_system
            + [
                {
                    "role": "user",
                    "content": (
                        f"[system note] '{invalid_flow_name_requested}' is not a "
                        "registered flow. Valid flow_name values: "
                        f"{', '.join(registry_names)}. Call start_glific_flow again "
                        "with one of those exact names if appropriate, or just reply "
                        "normally if none fit."
                    ),
                }
            ],
            tools,
        )
        for call in corrective.tool_calls:
            if call.name != "start_glific_flow":
                continue
            name, tool_summary = await _execute_tool_call(call, session, lms_client, flow_registry)
            tool_names.append(name)
            tool_summaries.append(f"{name} -> {tool_summary}")
            if tool_summary and tool_summary.get("pending_flow_start"):
                pending_flow_start = str(tool_summary["pending_flow_start"])
            # A second miss here is deliberately NOT retried again (AC-3's
            # "exactly one" round) — falls through to a plain reply below.
        result = corrective

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
    reply_text = reply_text or get_scripted_reply("fallback", session.identity.tap_language)

    allowed_fields = set()
    if session.content_reference:
        allowed_fields = {str(v) for v in session.content_reference.values()}

    # Code-review fix (2026-07-05, Finding 2): wired to the Guardrails AI
    # validator pass (`app/guardrails/validators.py::run_all_validators`,
    # R-5/P-004/ADR-007) — the actual, live call site, not the bespoke
    # `guardrails_checks.run_guardrails` aggregate this replaced. Both wrap
    # the SAME 4 deterministic checks (length, single-question,
    # no-answer-leakage, no-fabricated-content); `run_all_validators`
    # additionally routes through the real Guardrails AI `Validator`
    # classes, so a violation is enforced by the library ADR-010 R-5
    # mandates, not merely by a parallel pure-Python reimplementation that
    # was never reachable from production.
    guardrail_failures = run_all_validators(reply_text, allowed_content_fields=allowed_fields)
    if guardrail_failures:
        # One corrective regeneration ("regenerate once"): tell the model
        # which rule broke, no tools bound. If the rewrite still violates,
        # fall back — never ship a violating reply.
        failures = ", ".join(f.value for f in guardrail_failures)
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
        reply_text = rewrite.text.strip() or get_scripted_reply(
            "fallback", session.identity.tap_language
        )
        recheck_failures = run_all_validators(reply_text, allowed_content_fields=allowed_fields)
        if recheck_failures:
            logger.warning(
                "guardrails rejected rewrite (%s); falling back",
                ", ".join(f.value for f in recheck_failures),
            )
            reply_text = get_scripted_reply("fallback", session.identity.tap_language)

    advanced_to = _maybe_advance_state(session, advance_eligible)

    return TurnOutcome(
        reply_text=reply_text,
        session=session,
        tool_calls_made=tool_names,
        advanced_to=advanced_to,
        pending_flow_start=pending_flow_start,
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
    return get_scripted_reply("off_turn_nudge", session.identity.tap_language)


def handle_low_confidence_asr(session: SessionData) -> tuple[str, bool]:
    """Low-confidence ASR triggers a clarification turn. Returns
    (reply_text, should_fallback_to_text) — should_fallback_to_text is True
    once record_asr_failure's cap (2 consecutive) is reached.
    """
    should_fallback = record_asr_failure(session)
    if should_fallback:
        return get_scripted_reply("asr_fallback", session.identity.tap_language), True
    return get_scripted_reply("clarification", session.identity.tap_language), False


def handle_doubt_reexplanation(session: SessionData) -> EscalationReason | None:
    """Wraps state_machine.record_reexplanation for turn-logic callers."""
    return record_reexplanation(session)
