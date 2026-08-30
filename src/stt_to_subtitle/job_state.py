"""Structured job phase, state, and reason contracts.

The legacy ``jobs.status`` column remains as a scheduler compatibility field.
All user-facing state classification must use the structured projection here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class JobPhase(StrEnum):
    EXTRACTION = "extraction"
    TRANSCRIPTION = "transcription"
    TRANSLATION = "translation"
    DRAFT_TRANSLATION = "draft_translation"
    REVIEW_TRANSLATION = "review_translation"
    EXTERNAL_REVIEW = "external_review"
    RENDER = "render"
    COMPLETE = "complete"


class JobState(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    PAUSED = "paused"
    BLOCKED = "blocked"
    STOPPED = "stopped"
    FAILED = "failed"
    DONE = "done"


class JobReason(StrEnum):
    USER_STOP = "user_stop"
    LM_UNAVAILABLE = "lm_unavailable"
    DRAFT_TRANSLATION_UNAVAILABLE = "draft_translation_unavailable"
    REVIEW_TRANSLATION_UNAVAILABLE = "review_translation_unavailable"
    EXTERNAL_MODEL_UNAVAILABLE = "external_model_unavailable"
    STT_UNAVAILABLE = "stt_unavailable"
    SERVICE_RESTARTED = "service_restarted"
    ARTIFACT_MISSING = "artifact_missing"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    INVALID_INPUT = "invalid_input"
    AUTH_REQUIRED = "auth_required"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    TRANSCRIPTION_PROCESSING_ERROR = "transcription_processing_error"
    INTERNAL_ERROR = "internal_error"


LEGACY_USER_STOP_MESSAGES = frozenset(
    {
        "사용자 요청으로 전체 작업이 중단되었습니다.",
        "사용자 요청으로 작업이 중단되었습니다.",
    }
)


@dataclass(frozen=True)
class StructuredJobState:
    phase: JobPhase
    state: JobState
    reason_code: JobReason | None = None


_RUNNING_PHASES = {
    "extracting": JobPhase.EXTRACTION,
    "transcription_running": JobPhase.TRANSCRIPTION,
    "translation_running": JobPhase.TRANSLATION,
    "rendering": JobPhase.RENDER,
}
_WAITING_PHASES = {
    "audio_ready": JobPhase.TRANSCRIPTION,
    "transcribed": JobPhase.TRANSLATION,
    "translated": JobPhase.RENDER,
}
_DONE_STATUSES = {
    "audio_completed",
    "transcription_completed",
    "completed",
}
_LEGACY_STAGE_PHASES = {
    "audio extraction": JobPhase.EXTRACTION,
    "extraction": JobPhase.EXTRACTION,
    "transcription": JobPhase.TRANSCRIPTION,
    "translation": JobPhase.TRANSLATION,
    "render": JobPhase.RENDER,
}

_INDEPENDENT_OPERATION_PHASES = {
    "transcribe": JobPhase.TRANSCRIPTION,
    "draft_translate": JobPhase.DRAFT_TRANSLATION,
    "review_translate": JobPhase.REVIEW_TRANSLATION,
    "external_review": JobPhase.EXTERNAL_REVIEW,
}


def phase_from_legacy_stage(
    stage: str | None,
    *,
    operation: str,
) -> JobPhase:
    independent_phase = _INDEPENDENT_OPERATION_PHASES.get(operation)
    if independent_phase is not None:
        return independent_phase
    normalized = str(stage or "").strip().lower()
    if normalized in _LEGACY_STAGE_PHASES:
        return _LEGACY_STAGE_PHASES[normalized]
    if operation == "translate":
        return JobPhase.TRANSLATION
    return JobPhase.EXTRACTION


def structured_state_from_legacy(
    *,
    status: str,
    operation: str,
    blocked_stage: str | None = None,
    error: str | None = None,
    detect_legacy_user_stop: bool = True,
) -> StructuredJobState:
    """Project a legacy scheduler status into the stable domain contract."""
    independent_phase = _INDEPENDENT_OPERATION_PHASES.get(operation)
    if status in _RUNNING_PHASES:
        return StructuredJobState(
            independent_phase or _RUNNING_PHASES[status],
            JobState.RUNNING,
        )
    if status in _WAITING_PHASES:
        return StructuredJobState(
            independent_phase or _WAITING_PHASES[status],
            JobState.WAITING,
        )
    if status in _DONE_STATUSES:
        return StructuredJobState(
            independent_phase or JobPhase.COMPLETE,
            JobState.DONE,
        )
    if status == "translation_paused":
        return StructuredJobState(
            independent_phase or JobPhase.TRANSLATION,
            JobState.PAUSED,
        )
    if status == "queued":
        phase = independent_phase or (
            JobPhase.TRANSLATION
            if operation == "translate"
            else JobPhase.EXTRACTION
        )
        return StructuredJobState(phase, JobState.WAITING)

    phase = phase_from_legacy_stage(blocked_stage, operation=operation)
    if status == "blocked":
        if (
            detect_legacy_user_stop
            and str(error or "") in LEGACY_USER_STOP_MESSAGES
        ):
            return StructuredJobState(
                phase,
                JobState.STOPPED,
                JobReason.USER_STOP,
            )
        if str(error or "") == "service restarted during this stage":
            reason = JobReason.SERVICE_RESTARTED
        elif phase == JobPhase.DRAFT_TRANSLATION:
            reason = JobReason.DRAFT_TRANSLATION_UNAVAILABLE
        elif phase == JobPhase.REVIEW_TRANSLATION:
            reason = JobReason.REVIEW_TRANSLATION_UNAVAILABLE
        elif phase == JobPhase.EXTERNAL_REVIEW:
            reason = JobReason.EXTERNAL_MODEL_UNAVAILABLE
        elif phase == JobPhase.TRANSLATION:
            reason = JobReason.LM_UNAVAILABLE
        elif phase == JobPhase.TRANSCRIPTION:
            reason = JobReason.STT_UNAVAILABLE
        else:
            reason = JobReason.INTERNAL_ERROR
        return StructuredJobState(phase, JobState.BLOCKED, reason)
    if status == "failed":
        return StructuredJobState(
            phase,
            JobState.FAILED,
            JobReason.INTERNAL_ERROR,
        )
    return StructuredJobState(phase, JobState.WAITING)
