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

Every state-branch reachable here (identity_confirmation, greeting,
mode_choice, and the run_turn-driven states) is deterministic/structural —
"never an LLM's own narrative sense of progress" — mirroring tap-buddy's
`_process_turn` decision tree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from app.config.settings import Settings
from app.core.guardrails_checks import detect_distress
from app.core.identity import confirm_identity, resolve_identity_by_name
from app.core.state_machine import (
    EscalationReason,
    SessionData,
    SessionState,
    escalate,
    new_session,
    reset_asr_failures,
    transition,
)
from app.core.turn_logic import ModelRouterLike, handle_low_confidence_asr, run_turn
from app.ingress.glific_payload import GlificWebhookPayload
from app.ingress.media import synthesize_and_store, transcribe_inbound_media
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
            if should_fallback:
                await open_ticket_for_escalation(payload.contact_id, session, glific_client)
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
        session = new_session(payload.contact_id, payload.phone, needs_identity_confirmation=True)
        await session_store.save_session(session)
        await glific_client.send_text(payload.contact_id, "Hi! Before we start, what's your name?")
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    if session.state == SessionState.IDENTITY_CONFIRMATION and not session.identity.candidates:
        resolution = await resolve_identity_by_name(
            payload.contact_id, payload.phone, user_text.strip(), lms_client
        )
        session = resolution.session
        await session_store.save_session(session)
        if resolution.needs_confirmation:
            names = ", ".join(c["child_name"] for c in resolution.candidates)
            await glific_client.send_text(
                payload.contact_id, f"I found a few names: {names}. Which one is you?"
            )
            return TurnProcessingResult(handled=True, session=session, reply_sent=True)
        if not resolution.candidates:
            await glific_client.send_text(
                payload.contact_id,
                "I couldn't find that name for this phone number. Can you check the spelling?",
            )
            return TurnProcessingResult(handled=True, session=session, reply_sent=True)
        reply = (
            f"Hi {session.identity.child_name}! How are you feeling today "
            "— excited, okay, or tired?"
        )
        await glific_client.send_text(payload.contact_id, reply)
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    if session.state == SessionState.IDENTITY_CONFIRMATION and session.identity.candidates:
        if confirm_identity(session, user_text.strip()):
            transition(session, SessionState.GREETING)
            await session_store.save_session(session)
            reply = f"Great, hi {session.identity.child_name}! How are you feeling today?"
            await glific_client.send_text(payload.contact_id, reply)
        else:
            await glific_client.send_text(
                payload.contact_id, "Sorry, I didn't recognize that name — can you try again?"
            )
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    # Turn 2's readiness reply: the answer itself is stored raw (no
    # sentiment classifier). This transition and its reply are
    # deterministic/scripted — no model call on this turn.
    if session.state == SessionState.GREETING:
        session.readiness_response = user_text.strip()
        transition(session, SessionState.MODE_CHOICE)
        await session_store.save_session(session)
        reply = (
            "Great. We can talk in voice, or you can type if that feels easier. "
            "What would you like?"
        )
        await glific_client.send_text(payload.contact_id, reply)
        return TurnProcessingResult(handled=True, session=session, reply_sent=True)

    # Turn 3's mode choice: capture "voice"/"text" verbatim from the
    # child's reply (the mode itself isn't model-decided).
    if session.state == SessionState.MODE_CHOICE and session.mode is None:
        lowered = user_text.strip().lower()
        if "voice" in lowered:
            session.mode = "voice"
        elif "text" in lowered or "type" in lowered:
            session.mode = "text"
        if session.mode is not None:
            transition(session, SessionState.CONTENT_INTRO)
            await session_store.save_session(session)
        else:
            reply = "Would you like to continue in voice, or by typing?"
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
