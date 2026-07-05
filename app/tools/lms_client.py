"""TAP LMS (Frappe) client — the COMPLETE LMS integration surface.

Ported from `tap-buddy/src/tap_buddy/clients/lms_client.py` (CR-004 Task
12). Exactly 4 whitelisted endpoints. No other method exists on this
client, and none should ever be added without a corresponding PRD/CR —
`save_submission` is the ONLY write path (state authority, L-001).

Live-verified dotted paths (tap-buddy/CLAUDE.md, 2026-07-03, against
lms.evalix.xyz with a real API key — re-verification against THIS stack is
Phase 7's job per L-009, not assumed carried over just because the paths
are unchanged):
  - get_student_by_phone_and_name -> tap_lms.api.get_student_by_phone_and_name
  - get_quiz_question_details     -> tap_lms.api.get_quiz_question_details
  - save_submission                -> tap_lms.summer_program.save_submission.save_submission
  - get_next_content               -> tap_lms.summer_program.student_progression_sp.get_next_content
    (requires Frappe token auth via `Authorization: token <value>` —
    allow_guest=False on the live server, NOT a whitelist gap. See
    Settings.tap_lms_token.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

GET_STUDENT_METHOD = "tap_lms.api.get_student_by_phone_and_name"
GET_NEXT_CONTENT_METHOD = "tap_lms.summer_program.student_progression_sp.get_next_content"
GET_QUIZ_QUESTION_METHOD = "tap_lms.api.get_quiz_question_details"
SAVE_SUBMISSION_METHOD = "tap_lms.summer_program.save_submission.save_submission"


class LmsApiError(Exception):
    """Raised when the LMS returns a non-2xx or a business-logic failure envelope."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class StudentCandidate:
    student_id: str
    child_name: str
    grade: str | None = None
    tap_language: str | None = None


@dataclass
class StudentLookupResult:
    """Result of get_student_by_phone_and_name. `candidates` holds every
    match (usually one); the caller decides whether disambiguation
    (identity_confirmation) is needed based on len(candidates) > 1.
    """

    success: bool
    candidates: list[StudentCandidate]
    error: str | None = None


@dataclass
class ContentReference:
    content_id: str
    title: str
    content_type: str  # "video" | "quiz" | "assignment"
    raw: dict[str, Any]


@dataclass
class QuizQuestionDetails:
    """Deliberately has NO `correct_option` attribute — F-011's type-level
    guard. TAP LMS's live endpoint DOES return `correct_option`/
    `correct_option_text` (F-012 correction), but this type has nowhere to
    put it even if the raw response includes it, which is the enforcement
    mechanism: any code constructing this dataclass from the live response
    must explicitly choose not to carry that field forward.
    """

    question_id: str
    question_text: str
    hint: str | None
    options: list[str]
    raw: dict[str, Any]


@dataclass
class SubmissionResult:
    success: bool
    status: str
    message: str | None = None


class TapLmsClient:
    """The complete TAP LMS integration surface — exactly 4 methods. Do not
    add a 5th without a PRD/CR.
    """

    def __init__(
        self, base_url: str, api_key: str, timeout: float = 45.0, frappe_token: str | None = None
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        # get_next_content is allow_guest=False on the live server and
        # requires Frappe token auth — optional TAP_LMS_TOKEN, sent as
        # `Authorization: token <value>` when set. Harmless to send on all 4
        # calls (the other 3 endpoints don't require it but don't reject the
        # extra header either), so it's applied client-wide.
        self.frappe_token = frappe_token
        headers = {"Authorization": f"token {frappe_token}"} if frappe_token else {}
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, headers=headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload = {"api_key": self.api_key, **payload}
        try:
            resp = await self._client.post(f"/api/method/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise LmsApiError(f"LMS request failed: {exc}") from None
        if resp.status_code >= 400:
            raise LmsApiError(
                f"LMS returned {resp.status_code} for {method}: {resp.text[:300]}",
                status_code=resp.status_code,
            )
        try:
            body: dict[str, Any] = resp.json()
        except ValueError as exc:
            raise LmsApiError(f"LMS returned non-JSON body for {method}") from exc
        return body

    async def get_student_by_phone_and_name(
        self, phone_number: str, buddy_name: str
    ) -> StudentLookupResult:
        body = await self._call(
            GET_STUDENT_METHOD, {"phone_number": phone_number, "buddy_name": buddy_name}
        )
        message = body.get("message", {})
        if isinstance(message, dict) and message.get("success") is False:
            return StudentLookupResult(success=False, candidates=[], error=message.get("error"))
        # Live success shape: the student payload nests under
        # message["data"] as a single dict with student_id/buddy_name/
        # current_grade keys. Fall back to the flat message for fixture/mock
        # shapes, and accept a list under either for shared-phone
        # multi-child responses.
        payload = message.get("data", message) if isinstance(message, dict) else message
        raw_candidates = payload if isinstance(payload, list) else [payload]
        candidates = [
            StudentCandidate(
                student_id=str(c.get("student_id") or c.get("name") or ""),
                child_name=c.get("child_name") or c.get("buddy_name") or "",
                grade=c.get("grade") or c.get("current_grade"),
                tap_language=c.get("tap_language") or c.get("language"),
            )
            for c in raw_candidates
            if isinstance(c, dict)
        ]
        return StudentLookupResult(success=True, candidates=candidates)

    async def get_next_content(self, student_id: str) -> ContentReference | None:
        body = await self._call(GET_NEXT_CONTENT_METHOD, {"student_id": student_id})
        # This endpoint is wrapped in tap_lms's @glific_response decorator,
        # which returns flat JSON with no Frappe "message" envelope. Fall
        # back to the flat body when the envelope is absent.
        message_raw = body.get("message")
        message: dict[str, Any] = message_raw if isinstance(message_raw, dict) else body
        if message.get("success") is False:
            return None
        return ContentReference(
            content_id=str(message.get("content_id") or message.get("name") or ""),
            title=message.get("title", ""),
            content_type=message.get("content_type", ""),
            raw=message,
        )

    async def get_quiz_question_details(
        self, question_id: str, language: str
    ) -> QuizQuestionDetails | None:
        body = await self._call(
            GET_QUIZ_QUESTION_METHOD, {"question_id": question_id, "language": language}
        )
        message = body.get("message", {})
        inner = message.get("message", message) if isinstance(message, dict) else message
        if isinstance(inner, dict) and inner.get("success") is False:
            return None
        return QuizQuestionDetails(
            question_id=question_id,
            question_text=inner.get("question_text", ""),
            hint=inner.get("hint"),
            options=inner.get("options", []),
            raw=inner,
        )

    async def save_submission(
        self, student_id: str, content_id: str, submission: str
    ) -> SubmissionResult:
        body = await self._call(
            SAVE_SUBMISSION_METHOD,
            {"student_id": student_id, "content_id": content_id, "submission": submission},
        )
        message = body.get("message", body)
        if isinstance(message, dict):
            return SubmissionResult(
                success=bool(message.get("success", False)),
                status=message.get("status", "unknown"),
                message=message.get("error_detail") or message.get("user_message"),
            )
        return SubmissionResult(
            success=False, status="unknown", message="unrecognized response shape"
        )
