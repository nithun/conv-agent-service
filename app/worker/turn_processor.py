"""Turn-processing entrypoint (CR-004 Task 7/8): the worker-side
counterpart to tap-buddy's `adapters/glific_webhook.py::_process_turn`.

Distress-gate layering (Invariant 4/BR-003 AC-9 + BR-004 Task 3 — the
two-gate-by-modality shape recorded in AD-002, 2026-07-17): for TEXT turns
the PRIMARY check runs at ingress (`app/ingress/glific_webhook.py`), before
this entrypoint is ever reached, and this module adds no second text check.
For AUDIO turns the transcript does not exist until this worker transcribes
it (transcription is forbidden on the fast-ack path, L-002), so the PRIMARY
check for voice runs HERE, on the raw best-effort transcript — even a
low-confidence one — before any state handling, via the same
`_escalate_for_distress` surface the ingress gate uses. `run_turn`'s own
`detect_distress` call remains as defense-in-depth for turns that reach the
state machine. The layering is deliberate, not redundant — do not
"simplify" either gate away.

Every state-branch reachable here (identity_confirmation, language_choice,
greeting, mode_choice, and the run_turn-driven states) is
deterministic/structural — "never an LLM's own narrative sense of
progress" — mirroring tap-buddy's `_process_turn` decision tree.

CR-007: every scripted (non-LLM) reply below is sourced from
`app/config/scripted_replies.py::get_scripted_reply`, keyed by
`session.identity.tap_language`, EXCEPT the four pre-identity-resolution
literals (each marked "PRE-IDENTITY LITERAL" at its call site) — those
genuinely cannot know the child's language before identity resolves. GAP 0
(the LANGUAGE_CHOICE state) is the scripted, language-agnostic ask-turn
that fires whenever identity resolution didn't yield a `tap_language` —
self-retiring once the LMS starts returning one (see
`app/core/identity.py`).

CR-011/ADR-013 (2026-07-27 CONTRACT UPDATE, tasks 6/7): two deterministic,
never-LLM-routed additions (L-006), both keyed off `lms_client`'s two new
whitelisted methods (`app/tools/lms_client.py`):
  - `TESTAIRESET` dispatch (Task 6) — a top-of-function literal-text match,
    ahead of every other branch (including the audio-transcription block),
    same class as the existing mode/language-choice keyword matching. NO
    eligibility guard (Option B): wipes the Redis session and calls
    `reset_test_student` with the session-resolved `student_id` (L-014),
    unconditionally, for whichever identity happens to be resolved.
  - Auto-provisioning on a genuinely not-found identity (Task 7) — the
    zero-candidates branch inside IDENTITY_CONFIRMATION now calls
    `register_test_student` (a SINGLE call; supersedes CR-011's originally
    -assumed two-step register-then-re-resolve shape) instead of
    dead-ending in "I couldn't find that name...".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.config.scripted_replies import (
    get_language_choice_keywords,
    get_mode_choice_keywords,
    get_reset_keyword,
    get_scripted_reply,
    language_choice_prompt,
    language_choice_reask,
)
from app.config.settings import Settings
from app.core.guardrails_checks import detect_distress
from app.core.identity import confirm_identity, resolve_identity_by_name
from app.core.state_machine import (
    MAX_LANGUAGE_CHOICE_ATTEMPTS,
    EscalationReason,
    SessionData,
    SessionState,
    escalate,
    new_session,
    record_language_choice_attempt,
    reset_asr_failures,
    transition,
)
from app.core.turn_logic import ModelRouterLike, handle_low_confidence_asr, run_turn
from app.ingress.glific_payload import GlificWebhookPayload
from app.ingress.media import synthesize_and_store, transcribe_inbound_media
from app.tools.lms_client import LmsApiError
from app.worker.distress_gate import _escalate_for_distress
from app.worker.escalation import open_ticket_for_escalation
from app.worker.session_store import RedisSessionStore

logger = logging.getLogger("app.worker.turn_processor")


class _GlificSender(Protocol):
    """Structural type for the delivery client this module needs — a
    subset of `app.delivery.glific_client.GlificClient`.
    """

    async def send_text(self, receiver_id: str, body_text: str) -> object: ...
    async def create_ticket(self, contact_id: str, topic: str, body_text: str) -> object: ...
    async def send_audio(
        self,
        receiver_id: str,
        media_url: str,
        caption: str | None = None,
        sender_id: str | None = None,
    ) -> object: ...


class _FlowStarter(Protocol):
    """Structural type for the turn-flow re-arm — the one method of
    `app.delivery.glific_client.GlificClient` that `maybe_rearm_turn_flow`
    needs.
    """

    async def start_contact_flow(self, flow_id: str, contact_id: str) -> object: ...


@dataclass
class TurnProcessingResult:
    """What one call to `process_turn` did — the worker/DLQ layer inspects
    this to decide retry/dead-letter outcomes and the escalation-handoff
    caller inspects `escalated`/`escalation_reason`.
    """

    handled: bool
    escalated: bool = False
    escalation_reason: EscalationReason | None = None
    session: SessionData | None = None
    reply_sent: bool = False


async def _deliver_reply(
    contact_id: str,
    reply_text: str,
    session: SessionData,
    *,
    speech_client: Any | None,
    settings: Settings | None,
    glific_client: _GlificSender,
) -> None:
    """BR-004 Task 4: voice-mode sessions with a configured speech client
    get a TTS reply (`synthesize_and_store` + `send_audio` — `send_audio`
    itself calls `register_media` internally, see
    `app/delivery/glific_client.py::GlificClient.send_audio`); every other
    case (text-mode, or a voice-mode session with no speech client
    configured) gets the plain `send_text` reply, unchanged.

    Failure classes are deliberately distinct (code-review note, Task 4):
    a `PUBLIC_BASE_URL`-unset `ValueError` from `synthesize_and_store` is a
    CONFIG BUG and propagates loudly — it must not be silently swallowed
    into a text fallback. A synthesis/registration/send failure (the
    speech provider erroring, or the Glific audio-send call failing) is a
    genuine "TTS didn't work this turn" case and DOES fall back to
    `send_text`, so the reply is never silently dropped.
    """
    if session.mode == "voice" and speech_client is not None and settings is not None:
        try:
            media_url = await synthesize_and_store(reply_text, session, speech_client, settings)
        except ValueError:
            raise
        except Exception:
            logger.exception(
                "TTS synthesis failed for contact=%s; falling back to text reply", contact_id
            )
        else:
            try:
                await glific_client.send_audio(contact_id, media_url)
                return
            except Exception:
                logger.exception(
                    "TTS audio delivery failed for contact=%s; falling back to text reply",
                    contact_id,
                )

    await glific_client.send_text(contact_id, reply_text)


async def process_turn(
    payload: GlificWebhookPayload,
    *,
    session_store: RedisSessionStore,
    lms_client: Any,
    model_router: ModelRouterLike,
    glific_client: _GlificSender,
    speech_client: Any | None = None,
    settings: Settings | None = None,
) -> TurnProcessingResult:
    """Processes exactly one enqueued turn. Ported decision tree from
    tap-buddy's `_process_turn` (identity resolution -> greeting/readiness
    -> mode_choice -> run_turn), re-platformed onto Redis session state and
    ModelRouter. The caller (the consumer loop, `app/worker/consumer_loop.py`)
    is responsible for ack/retry/dead-letter around this call — this
    function itself does not touch the Redis Stream.

    `speech_client`/`settings` (BR-004 Tasks 2-4): optional — constructed
    once by the worker entrypoint (`app/worker/main.py::_build_speech_client`,
    gated on `settings.google_application_credentials`) and threaded through
    here for BOTH inbound transcription and outbound TTS. `speech_client`
    is `None` whenever credentials aren't configured (CI, a fresh dev box)
    — every call site below degrades gracefully in that case (audio turns
    fall through with an empty transcript; voice-mode replies fall back to
    text), never crashing the worker.
    """
    user_text = payload.text or ""

    session = await session_store.load_session(payload.contact_id)

    # ---- CR-011/ADR-013 Task 6: TESTAIRESET literal-keyword dispatch ----
    # Deterministic, config-driven (L-006 — same class as the existing
    # mode/language-choice keyword matching), placed ahead of EVERY other
    # branch below (including the audio-transcription block), per CR-011
    # §3. Option B (Nithun's explicit choice): NO eligibility guard —
    # whichever student_id is currently resolved on the session gets reset
    # unconditionally, test-registered or real-shaped alike. A session with
    # nothing resolved yet (session is None, or student_id unset) is a
    # structural no-op — there is nothing to call reset_test_student with,
    # not an eligibility check.
    if user_text.strip().casefold() == get_reset_keyword().casefold():
        if session is not None and session.identity.student_id:
            # L-014: the id passed to reset_test_student is the
            # SESSION-resolved student_id — never a model-supplied or
            # re-derived one. Captured into locals BEFORE the session is
            # deleted below.
            reset_student_id = session.identity.student_id
            reset_tap_language = session.identity.tap_language
            await session_store.delete_session(payload.contact_id)
            try:
                await lms_client.reset_test_student(reset_student_id)
            except LmsApiError:
                # A downstream reset failure never crashes the worker or
                # blocks the confirmation reply (mirrors
                # maybe_rearm_turn_flow's never-raises discipline for a
                # non-core-conversation side effect) — the Redis session is
                # already wiped either way.
                logger.exception(
                    "reset_test_student failed for contact=%s student_id=%s",
                    payload.contact_id,
                    reset_student_id,
                )
            await glific_client.send_text(
                payload.contact_id, get_scripted_reply("reset_confirmation", reset_tap_language)
            )
            return TurnProcessingResult(handled=True, reply_sent=True)

        # No-op: nothing resolved yet to reset (session is None, or
        # student_id unset) — falls through to the generic `fallback`
        # scripted reply; a structural consequence of having no
        # student_id to call with, not an eligibility check (CR-011 Edge
        # cases).
        await glific_client.send_text(
            payload.contact_id,
            get_scripted_reply("fallback", session.identity.tap_language if session else None),
        )
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    # ---- BR-004 Task 2/3: audio-turn transcription, ASR-failure
    # clarification/fallback/escalation, and the worker-side distress check
    # — ALL placed here, before any of the state branches below, mirroring
    # tap-buddy's `_process_turn` structural position (transcribe -> handle
    # low-confidence ASR -> distress check -> state handling). Text turns
    # are entirely untouched by this block (no second distress check for
    # text — the ingress-level gate already covers it).
    media_url = payload.media_url
    if payload.message_type == "audio" and media_url:
        transcript = ""
        low_confidence = False
        if speech_client is not None:
            transcription = await transcribe_inbound_media(media_url, session, speech_client)
            transcript = transcription.transcript
            low_confidence = transcription.low_confidence
        else:
            # No speech client configured (credentials absent, Task 1's
            # gate) — cannot transcribe at all. Degrade gracefully: proceed
            # exactly as if this turn carried no resolvable text, same as a
            # low-confidence transcript, but without touching the
            # asr_failure_count (there's nothing to attribute a genuine ASR
            # failure to when ASR never ran).
            low_confidence = True

        # SAFETY FIRST (live-caught 2026-07-17, BR-004 Task 10): the
        # distress check runs on ANY non-empty transcript — INCLUDING a
        # low-confidence one — BEFORE the clarification path discards it. A
        # spoken distress cry transcribed at 0.49 confidence (the live
        # repro: threshold is 0.5) must escalate, not get "say it again";
        # children's speech in noisy homes is routinely below the bar.
        # Over-escalation on a garbled transcript is the safe failure
        # direction — tickets land in a human-reviewed queue. Conversation
        # logic below still only ever consumes a CONFIDENT transcript.
        if transcript and detect_distress(
            transcript, session.identity.tap_language if session else None
        ):
            if session is None:
                session = new_session(
                    payload.contact_id, payload.phone, needs_identity_confirmation=False
                )
            await _escalate_for_distress(
                session,
                payload.contact_id,
                session_store=session_store,
                glific_client=glific_client,
            )
            return TurnProcessingResult(
                handled=True,
                escalated=True,
                escalation_reason=EscalationReason.DISTRESS,
                session=session,
                reply_sent=True,
            )

        if not low_confidence:
            user_text = transcript
            if session is not None:
                reset_asr_failures(session)
                await session_store.save_session(session)
        elif speech_client is not None and session is not None:
            # A real (attempted) low-confidence transcription against an
            # EXISTING session — the consecutive-failure cap is meaningful
            # here. A brand-new contact (session is None) or a missing
            # speech client falls through with empty user_text instead;
            # the "session is None" branch below already handles a
            # first-ever contact identically regardless of input content.
            clarification_reply, should_fallback = handle_low_confidence_asr(session)
            if should_fallback:
                session.mode = "text"
                escalate(session, EscalationReason.ASR_FAILURE)
            await session_store.save_session(session)
            await glific_client.send_text(payload.contact_id, clarification_reply)
            if should_fallback and session.asr_escalation_ticket_message_id != payload.message_id:
                # Idempotency guard (F-039): a job redelivery for the SAME
                # message_id — e.g. a transient send_text failure ABOVE this
                # line on an earlier attempt, or a crash between a prior
                # successful ticket-open and the queue's ack — must not open
                # a SECOND Glific Ticket for the same ASR-failure escalation.
                # Mirrors the `language_choice_last_message_id` pattern
                # already established for the LANGUAGE_CHOICE counter
                # (2026-07-17 code review fix). Ticket-open recorded AFTER
                # the (never-raising, see escalation.py) attempt so a crash
                # before this line still retries on redelivery rather than
                # silently dropping the escalation.
                await open_ticket_for_escalation(payload.contact_id, session, glific_client)
                session.asr_escalation_ticket_message_id = payload.message_id
                await session_store.save_session(session)
            return TurnProcessingResult(
                handled=True,
                escalated=should_fallback,
                escalation_reason=session.escalation_reason if should_fallback else None,
                session=session,
                reply_sent=True,
            )

    if session is None:
        # First message from this contact — ask for their name before any
        # LMS call (get_student_by_phone_and_name requires buddy_name).
        # PRE-IDENTITY LITERAL (CR-007 Proposed behavior, #1 of 4): the
        # child's language is unknown until identity resolves — this string
        # genuinely cannot be looked up by tap_language at send time.
        session = new_session(payload.contact_id, payload.phone, needs_identity_confirmation=True)
        await session_store.save_session(session)
        await glific_client.send_text(payload.contact_id, "Hi! Before we start, what's your name?")
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    if session.state == SessionState.IDENTITY_CONFIRMATION and not session.identity.candidates:
        buddy_name = user_text.strip()
        resolution = await resolve_identity_by_name(
            payload.contact_id, payload.phone, buddy_name, lms_client
        )
        session = resolution.session
        if resolution.needs_confirmation:
            await session_store.save_session(session)
            # PRE-IDENTITY LITERAL (#2 of 4): a multi-candidate household can
            # have candidates with DIFFERENT languages — there is no single
            # language to pick until one candidate is confirmed.
            names = ", ".join(c["child_name"] for c in resolution.candidates)
            await glific_client.send_text(
                payload.contact_id, f"I found a few names: {names}. Which one is you?"
            )
            return TurnProcessingResult(handled=True, session=session, reply_sent=True)
        if not resolution.candidates:
            # CR-011/ADR-013 Task 7 (2026-07-27 CONTRACT UPDATE): a
            # genuinely not-found identity now auto-provisions a real TAP
            # LMS test student via the SINGLE-call `register_test_student`
            # (creates Student+ProgramEnrollment in a server-hardcoded test
            # batch and returns student_id directly — supersedes CR-011's
            # originally-assumed two-step register-then-re-resolve shape;
            # see AD-002's VERIFIED external-contracts rows) rather than
            # dead-ending in "I couldn't find that name...". A registration
            # failure (LmsApiError, or a response with success=False/no
            # student_id — a genuine business rejection, not the happy
            # path) falls back to today's reply instead of crashing or
            # silently retrying a second time this turn.
            try:
                registration = await lms_client.register_test_student(
                    name1=buddy_name,
                    phone=payload.phone,
                    glific_id=payload.contact_id,
                )
            except LmsApiError:
                logger.exception(
                    "register_test_student failed for contact=%s; falling back to "
                    "'couldn't find that name' reply",
                    payload.contact_id,
                )
                registration = None

            if registration is not None and registration.success and registration.student_id:
                # Treated exactly like a single-match resolution: this
                # endpoint never returns a tap_language, so the same
                # self-retiring GAP-0 check the single-match branch below
                # uses applies here too. session.state is already
                # IDENTITY_CONFIRMATION (set by resolve_identity_by_name's
                # zero-candidate new_session call above), so
                # IDENTITY_CONFIRMATION -> LANGUAGE_CHOICE is a directly
                # valid transition — no restore-then-transition dance
                # needed (unlike the found single-match branch below,
                # which pre-sets state=GREETING via a different
                # new_session call).
                session.identity.student_id = registration.student_id
                session.identity.child_name = buddy_name
                session.identity.tap_language = None
                transition(session, SessionState.LANGUAGE_CHOICE)
                await session_store.save_session(session)
                await glific_client.send_text(payload.contact_id, language_choice_prompt())
                return TurnProcessingResult(handled=True, session=session, reply_sent=True)

            if registration is not None:
                logger.warning(
                    "register_test_student returned a non-success/empty-id response for "
                    "contact=%s; falling back to 'couldn't find that name' reply",
                    payload.contact_id,
                )
            await session_store.save_session(session)
            # PRE-IDENTITY LITERAL (#3 of 4): no candidate resolved at all,
            # and auto-provisioning also failed — still no identity, so
            # still no language to look up by.
            await glific_client.send_text(
                payload.contact_id,
                "I couldn't find that name for this phone number. Can you check the spelling?",
            )
            return TurnProcessingResult(handled=True, session=session, reply_sent=True)
        # Single unambiguous match. GAP 0 (CR-007, root cause): the LMS
        # never actually returns a language today (grep-confirmed against
        # the live tap_lms.api.get_student_by_phone_and_name response
        # shape), so tap_language is almost always falsy here — enter the
        # scripted, language-agnostic LANGUAGE_CHOICE turn instead of an
        # English-only greeting. Self-retiring: once the LMS starts
        # returning a language, this branch is unreachable for that
        # contact (tap_language truthy -> straight to GREETING, exactly
        # today's behavior).
        if not session.identity.tap_language:
            # Code-review fix (2026-07-17 Medium finding): use transition()
            # here, consistently with the sibling multi-candidate-confirm
            # branch below (line ~336-ish), instead of assigning
            # session.state directly. Root cause of the prior direct
            # assignment: `resolve_identity_by_name`'s single-match path
            # builds `session` via `new_session(..., needs_identity_confirmation=False)`,
            # which pre-sets state=GREETING — GREETING -> LANGUAGE_CHOICE is
            # not an allowed edge (`_ALLOWED_TRANSITIONS`), so a literal
            # transition() call would have raised InvalidTransition. This
            # branch IS still identity resolution in progress (GAP 0's
            # language ask is a continuation of it, not a new state), so
            # state is first restored to IDENTITY_CONFIRMATION — the state
            # this session was actually in when the outer `if` above
            # matched — before the validated transition.
            # Behavior change confirmed bounded to turn_count: this now
            # bumps turn_count by 1 (transition()'s side effect), where the
            # direct assignment previously left it at 0. turn_count has no
            # other reader anywhere in this codebase (grep-confirmed
            # against app/ and tests/) — this only brings the single-match
            # branch in line with the multi-candidate-confirm branch, which
            # already bumps turn_count for the identical target state.
            session.state = SessionState.IDENTITY_CONFIRMATION
            transition(session, SessionState.LANGUAGE_CHOICE)
            await session_store.save_session(session)
            await glific_client.send_text(payload.contact_id, language_choice_prompt())
            return TurnProcessingResult(handled=True, session=session, reply_sent=True)
        await session_store.save_session(session)
        reply = get_scripted_reply("greeting_direct", session.identity.tap_language).format(
            child_name=session.identity.child_name or "there"
        )
        await glific_client.send_text(payload.contact_id, reply)
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    if session.state == SessionState.IDENTITY_CONFIRMATION and session.identity.candidates:
        if confirm_identity(session, user_text.strip()):
            # confirm_identity (CR-007 in-scope fix) populates tap_language
            # from the matched candidate when the LMS supplied one for THIS
            # candidate — same self-retiring GAP 0 check as the single-match
            # branch above.
            if session.identity.tap_language:
                transition(session, SessionState.GREETING)
                await session_store.save_session(session)
                reply = get_scripted_reply(
                    "greeting_candidate_confirm", session.identity.tap_language
                ).format(child_name=session.identity.child_name or "there")
                await glific_client.send_text(payload.contact_id, reply)
            else:
                transition(session, SessionState.LANGUAGE_CHOICE)
                await session_store.save_session(session)
                await glific_client.send_text(payload.contact_id, language_choice_prompt())
        else:
            await session_store.save_session(session)
            # PRE-IDENTITY LITERAL (#4 of 4): a failed candidate confirm is
            # still pre-identity — the child hasn't been matched to any
            # candidate yet, so (per #2 above) there is still no single
            # language to look this up by.
            await glific_client.send_text(
                payload.contact_id, "Sorry, I didn't recognize that name — can you try again?"
            )
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    # CR-007 GAP 0: the language-choice turn. Matches user_text against
    # every language's language_name_keywords, in language_map.yaml's
    # declared order (English first) — first substring match wins
    # (documented, deterministic; see CR-007 Edge cases). One scripted
    # re-ask on the first non-match; on the second non-match, defaults to
    # English and proceeds — never blocks the conversation.
    if session.state == SessionState.LANGUAGE_CHOICE:
        lowered = user_text.strip().lower()
        matched_language: str | None = None
        for language, keywords in get_language_choice_keywords():
            if any(kw.lower() in lowered for kw in keywords):
                matched_language = language
                break

        if matched_language is not None:
            session.identity.tap_language = matched_language
            transition(session, SessionState.GREETING)
            await session_store.save_session(session)
            reply = get_scripted_reply("greeting_direct", session.identity.tap_language).format(
                child_name=session.identity.child_name or "there"
            )
            await glific_client.send_text(payload.contact_id, reply)
            return TurnProcessingResult(handled=True, session=session, reply_sent=True)

        # Retry-safety (code review 2026-07-17 High finding): a transient
        # send_text failure below (the re-ask or the English-default reply)
        # can leave the session's state unchanged (LANGUAGE_CHOICE, on the
        # re-ask branch) or already saved-past-this-point (on the
        # cap-exceeded branch) while the exception still propagates —
        # `process_turn`'s caller (`app/worker/consumer_loop.py`) then
        # redelivers the SAME still-pending stream entry, re-running this
        # whole branch for the SAME `payload.message_id`. Without a guard,
        # `record_language_choice_attempt` would double-count that ONE real
        # user reply and silently skip the promised one-time re-ask.
        # `language_choice_last_message_id` tracks which inbound message
        # this session has already counted an attempt for; redelivery of
        # that same message_id re-derives the prior cap verdict instead of
        # incrementing again. A genuinely different message_id (the child's
        # actual next reply) still increments normally.
        if session.language_choice_last_message_id == payload.message_id:
            exceeded_cap = session.language_choice_attempts > MAX_LANGUAGE_CHOICE_ATTEMPTS
        else:
            exceeded_cap = record_language_choice_attempt(session)
            session.language_choice_last_message_id = payload.message_id

        if exceeded_cap:
            logger.warning(
                "language_choice: no match after %d unmatched repl%s for contact=%s; "
                "defaulting to English",
                session.language_choice_attempts,
                "y" if session.language_choice_attempts == 1 else "ies",
                payload.contact_id,
            )
            session.identity.tap_language = "English"
            transition(session, SessionState.GREETING)
            await session_store.save_session(session)
            reply = get_scripted_reply("greeting_direct", session.identity.tap_language).format(
                child_name=session.identity.child_name or "there"
            )
            await glific_client.send_text(payload.contact_id, reply)
            return TurnProcessingResult(handled=True, session=session, reply_sent=True)

        await session_store.save_session(session)
        await glific_client.send_text(payload.contact_id, language_choice_reask())
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    # Turn 2's readiness reply: the answer itself is stored raw (no
    # sentiment classifier). This transition and its reply are
    # deterministic/scripted — no model call on this turn.
    if session.state == SessionState.GREETING:
        session.readiness_response = user_text.strip()
        transition(session, SessionState.MODE_CHOICE)
        await session_store.save_session(session)
        reply = get_scripted_reply("readiness_mode_choice", session.identity.tap_language)
        await glific_client.send_text(payload.contact_id, reply)
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    # Turn 3's mode choice: capture "voice"/"text" verbatim from the
    # child's reply (the mode itself isn't model-decided). Per-language
    # keyword matching (CR-007 GAP 2) — the session language's own
    # voice/text keyword lists, unioned with English's (English always
    # works, regardless of the session's resolved language). Voice is
    # checked before text/type, preserving today's implicit precedence on
    # an ambiguous reply (documented in CR-007 Edge cases).
    if session.state == SessionState.MODE_CHOICE and session.mode is None:
        lowered = user_text.strip().lower()
        mode_keywords = get_mode_choice_keywords(session.identity.tap_language)
        if any(kw.lower() in lowered for kw in mode_keywords["voice"]):
            session.mode = "voice"
        elif any(kw.lower() in lowered for kw in mode_keywords["text"]):
            session.mode = "text"
        if session.mode is not None:
            transition(session, SessionState.CONTENT_INTRO)
            await session_store.save_session(session)
        else:
            reply = get_scripted_reply("mode_choice_reask", session.identity.tap_language)
            await glific_client.send_text(payload.contact_id, reply)
            return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    outcome = await run_turn(
        session=session, user_input=user_text, model_router=model_router, lms_client=lms_client
    )
    await session_store.save_session(outcome.session)

    await _deliver_reply(
        payload.contact_id,
        outcome.reply_text,
        outcome.session,
        speech_client=speech_client,
        settings=settings,
        glific_client=glific_client,
    )

    if outcome.escalated:
        # Task 13/Invariant 5: every escalation trigger reachable through
        # run_turn (distress defense-in-depth, unresolved-doubt cap) opens
        # a Glific Ticket — the same handoff surface the ingress-level
        # distress gate uses (app/worker/distress_gate.py), never the
        # dropped ad-hoc review dashboard.
        await open_ticket_for_escalation(payload.contact_id, outcome.session, glific_client)

    return TurnProcessingResult(
        handled=True,
        escalated=outcome.escalated,
        escalation_reason=outcome.session.escalation_reason,
        session=outcome.session,
        reply_sent=True,
    )


async def maybe_rearm_turn_flow(
    result: TurnProcessingResult,
    payload: GlificWebhookPayload,
    *,
    turn_flow_id: str,
    glific_client: _FlowStarter,
) -> bool:
    """Re-arm the 'TAP Buddy Turn' relay flow for this contact after a
    processed turn. Ported from tap-buddy's
    `adapters/glific_webhook.py::_maybe_rearm_turn_flow` — the CR-004 port
    carried the `GLIFIC_TURN_FLOW_ID` setting and the
    `GlificClient.start_contact_flow` method but no call site, found live
    when the F-019 cutover smoke stalled after turn 1 (2026-07-16).

    Why re-arm at all: Glific's webhook Oban jobs are unique on
    (context_id, url, action_id) for 60s, so a wait->webhook loop inside ONE
    flow context silently drops every repeat call — each turn needs a fresh
    flow run. After every reply we start the turn flow again: it parks at a
    wait node, the child's next message resumes it, its webhook node calls
    us, and it ends. No re-arm after close/escalation — the conversation is
    over and the keyword flow is the way back in.

    Never raises: the turn itself already succeeded (reply sent); a re-arm
    failure must not push the already-processed entry into retry/DLQ. It is
    logged and returns False.
    """
    if not turn_flow_id:
        return False
    session = result.session
    if session is not None and session.state in (SessionState.CLOSE, SessionState.ESCALATED):
        return False
    try:
        await glific_client.start_contact_flow(turn_flow_id, payload.contact_id)
        return True
    except Exception:
        logger.exception("turn-flow re-arm failed for contact=%s", payload.contact_id)
        return False
