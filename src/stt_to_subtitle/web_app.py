"""Container-friendly web UI for the subtitle pipeline."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
import hmac
import json
import logging
import math
import os
from pathlib import Path
import secrets
import time
from typing import Any, AsyncIterator
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import pass_context
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .artifacts import artifact_filename
from .contracts import validate_transcript, validate_translation_items
from .gpu_monitoring import GpuSnapshot, PrometheusGpuMonitor
from .files import sha256_file
from .job_store import RETRYABLE_STATUSES, SUCCESS_STATUSES
from .job_state import JobReason, JobState
from .media_preview import (
    guess_media_type,
    iter_file_range,
    parse_byte_range,
    srt_to_webvtt,
)
from .operational_metrics import prometheus_exposition
from .path_display import shorten_display_path
from .web_config import (
    group_multipart_media,
    MediaLibrary,
    RemoteServerSettings,
    SubtitleValidatorSettings,
    WebSettings,
    normalize_server_url,
)
from .orchestrator import (
    COMPARISON_AUDIO_SOURCE_JOB_ID_OPTION,
    COMPARISON_PARENT_ID_OPTION,
    DEFAULT_ARTIFACT_CLEANUP_AGE_DAYS,
    SubtitleOrchestrator,
    TRANSCRIPTION_COMPARISON_BACKENDS,
)
from .service_clients import (
    ExternalServiceError,
    list_openai_compatible_models,
)
from .subtitle import render_webvtt
from .subtitle_validation import (
    compare_subtitles,
    parse_subtitle,
    render_webvtt as render_external_webvtt,
    subtitle_asset_hash,
)
from .translation_comparison import compare_translation_items
from .time_display import (
    configure_kst_logging,
    format_kst_iso,
    format_kst_timestamp,
)
from .whisperx_worker import WHISPERX_MAX_CHUNK_LENGTH_SECONDS
from .whisperjav_worker import (
    DEFAULT_ANIME_MAX_GROUP_SECONDS,
    DEFAULT_QWEN_MAX_GROUP_SECONDS,
    MAX_MAX_GROUP_SECONDS,
    MIN_MAX_GROUP_SECONDS,
)

LOGGER = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent
RECENT_JOB_LIMIT = 20
COMPARISON_HISTORY_LIMIT = 20
DASHBOARD_JOB_LIMIT = 5
ACTOR_PROGRESS_LIMIT = 5
DASHBOARD_ATTENTION_LIMIT = 3
DASHBOARD_COMPLETED_LIMIT = 4
DASHBOARD_QUEUE_LIMIT = 6
DASHBOARD_MODE_COOKIE = "stt_dashboard_mode"
WEBGPU_QUEUE_LIMIT = 6
WEBGPU_PHASE_JOB_LIMIT = 6
WEBGPU_STOPPED_LIMIT = 3
WEBGPU_COMPLETED_LIMIT = 4
WEBGPU_MEDIA_ZONE_LIMIT = 8
WEBGPU_TRANSPORT_WINDOW_SECONDS = 30.0
JOB_STATUS_GROUPS = {
    "running": {JobState.RUNNING.value},
    "waiting": {JobState.WAITING.value},
    "paused": {JobState.PAUSED.value},
    "blocked": {JobState.BLOCKED.value},
    "stopped": {JobState.STOPPED.value},
    "failed": {JobState.FAILED.value},
    "completed": {JobState.DONE.value},
}
JOB_STATUS_GROUP_LABELS = {
    "running": "진행 중",
    "blocked": "중단",
    "failed": "실패",
    "waiting": "대기",
    "paused": "일시 정지",
    "completed": "완료",
    "stopped": "정지",
}
JOB_STATUS_FILTER_NAV = tuple(
    {"key": key, "label": JOB_STATUS_GROUP_LABELS[key]}
    for key in (
        "waiting",
        "running",
        "paused",
        "blocked",
        "stopped",
        "failed",
        "completed",
    )
)
TRANSLATION_GENERATION_STATE_LABELS = {
    "partial": "부분 저장",
    "running": "진행 중",
    "paused": "일시 정지",
    "blocked": "중단",
    "failed": "실패",
    "stopped": "사용자 정지",
    "completed": "완료",
    "interrupted": "재시작으로 종료",
}
TRANSLATION_GENERATION_ORIGIN_LABELS = {
    "automatic": "자동 번역",
    "legacy": "이전 JSON 가져옴",
    "restart": "재번역",
    "manual": "직접 편집",
}
TRANSCRIPT_REVISION_ORIGIN_LABELS = {
    "automatic": "자동 전사",
    "manual": "직접 편집",
    "imported": "비교 결과 선택",
    "legacy": "이전 형식",
}
SUBTITLE_GENERATION_ORIGIN_LABELS = {
    "rendered": "시스템 생성",
    "legacy": "기존 자막 가져옴",
}
TRANSLATION_COMPARISON_PAGE_LIMIT = 100
TRANSLATION_COMPARISON_FILTERS = (
    ("changes", "변경 항목"),
    ("all", "전체"),
    ("changed", "번역 변경"),
    ("added", "추가"),
    ("removed", "삭제"),
    ("source_changed", "전사 세그먼트 변경"),
    ("unchanged", "완전 동일"),
)
JOB_STAGE_FILTERS = {
    "extraction": {
        "phases": {"extraction"},
        "legacy_phase_statuses": {"audio_completed"},
    },
    "transcription": {
        "phases": {"transcription"},
        "legacy_phase_statuses": {"transcription_completed"},
    },
    "transcription_waiting": {
        "phases": {"transcription"},
        "states": {"waiting"},
    },
    "transcription_running": {
        "phases": {"transcription"},
        "states": {"running"},
    },
    "transcription_completed": {
        "statuses": {"transcription_completed"},
    },
    "translation": {
        "phases": {"translation"},
        "legacy_phase_statuses": {"translated"},
    },
    "translation_waiting": {
        "phases": {"translation"},
        "states": {"waiting"},
    },
    "translation_running": {
        "phases": {"translation"},
        "states": {"running"},
    },
    "translation_completed": {"statuses": {"translated"}},
    "completed": {"states": {"done"}},
}
JOB_STAGE_FILTER_LABELS = {
    "extraction": "추출",
    "transcription": "전사",
    "transcription_waiting": "전사 · 대기",
    "transcription_running": "전사 · 진행 중",
    "transcription_completed": "전사 · 완료",
    "translation": "번역",
    "translation_waiting": "번역 · 대기",
    "translation_running": "번역 · 진행 중",
    "translation_completed": "번역 · 완료",
    "completed": "완료",
}
JOB_STAGE_FILTER_NAV = (
    {"key": "extraction", "label": "추출", "children": ()},
    {
        "key": "transcription",
        "label": "전사",
        "children": (
            {"key": "transcription_waiting", "label": "대기"},
            {"key": "transcription_running", "label": "진행 중"},
            {"key": "transcription_completed", "label": "완료"},
        ),
    },
    {
        "key": "translation",
        "label": "번역",
        "children": (
            {"key": "translation_waiting", "label": "대기"},
            {"key": "translation_running", "label": "진행 중"},
            {"key": "translation_completed", "label": "완료"},
        ),
    },
    {"key": "completed", "label": "완료", "children": ()},
)
JOB_STATUS_LABELS = {
    "queued": "대기 중",
    "extracting": "오디오 추출 중",
    "audio_ready": "전사 대기",
    "audio_completed": "오디오 추출 완료",
    "transcription_running": "전사 중",
    "transcribed": "번역 대기",
    "transcription_completed": "전사 완료",
    "translation_running": "번역 중",
    "translation_paused": "번역 일시 정지",
    "translated": "자막 생성 대기",
    "rendering": "자막 생성 중",
    "completed": "완료",
    "blocked": "중단",
    "failed": "실패",
}
JOB_STATE_LABELS = {
    "waiting": "대기",
    "running": "진행 중",
    "paused": "일시 정지",
    "blocked": "중단",
    "stopped": "정지",
    "failed": "실패",
    "done": "완료",
}
JOB_PHASE_LABELS = {
    "extraction": "추출",
    "transcription": "전사",
    "translation": "번역",
    "render": "작업 완료",
    "complete": "완료",
}
MEDIA_PROCESSING_LABELS = {
    "queued": "작업 대기",
    "extracting": "오디오 추출 중",
    "audio_ready": "오디오 추출 완료 · 전사 대기",
    "audio_completed": "오디오 추출 완료",
    "transcription_running": "전사 중",
    "transcribed": "전사 완료 · 번역 대기",
    "transcription_completed": "전사 완료",
    "translation_running": "번역 중",
    "translation_paused": "번역 일시 정지",
    "translated": "번역 완료 · 자막 생성 대기",
    "rendering": "자막 생성 중",
    "completed": "자막 생성 완료",
}
STT_BACKEND_LABELS = {
    "whisperjav": "WhisperJAV",
    "hybrid": "하이브리드",
    "whisperx": "WhisperX",
    "kotoba": "Kotoba",
}
JOB_OPERATION_LABELS = {
    "extract": "추출",
    "transcribe": "전사",
    "translate": "번역",
    "full": "전체",
}
JOB_OPERATION_FILTER_NAV = (
    {"key": "extract", "label": "추출 요청"},
    {"key": "transcribe", "label": "전사 요청"},
    {"key": "translate", "label": "번역 요청"},
    {"key": "full", "label": "전체 파이프라인"},
)
JOB_OPERATION_FILTER_LABELS = {
    str(option["key"]): str(option["label"])
    for option in JOB_OPERATION_FILTER_NAV
}
JOB_PHASE_FILTERS = {
    "extraction": {"phases": {"extraction"}},
    "transcription": {"phases": {"transcription"}},
    "translation": {"phases": {"translation"}},
    "completion": {"phases": {"render", "complete"}},
}
JOB_PHASE_FILTER_LABELS = {
    "extraction": "추출",
    "transcription": "전사",
    "translation": "번역",
    "completion": "작업 완료",
}
JOB_PHASE_FILTER_NAV = tuple(
    {"key": key, "label": label}
    for key, label in JOB_PHASE_FILTER_LABELS.items()
)
JOB_STATE_FILTER_NAV = tuple(
    {"key": key, "label": JOB_STATE_LABELS[key]}
    for key in (
        "waiting",
        "running",
        "paused",
        "blocked",
        "stopped",
        "failed",
        "done",
    )
)
JOB_REASON_LABELS = {
    JobReason.USER_STOP.value: "사용자 정지",
    JobReason.LM_UNAVAILABLE.value: "번역 모델 사용 불가",
    JobReason.STT_UNAVAILABLE.value: "전사 서버 사용 불가",
    JobReason.SERVICE_RESTARTED.value: "서비스 재시작",
    JobReason.ARTIFACT_MISSING.value: "산출물 누락",
    JobReason.MODEL_OUTPUT_INVALID.value: "모델 출력 오류",
    JobReason.INVALID_INPUT.value: "입력 오류",
    JobReason.AUTH_REQUIRED.value: "인증 필요",
    JobReason.RESOURCE_EXHAUSTED.value: "자원 부족",
    JobReason.TRANSCRIPTION_PROCESSING_ERROR.value: "전사 처리 오류",
    JobReason.INTERNAL_ERROR.value: "내부 오류",
}
JOB_STAGE_LABELS = {
    "audio extraction": "추출",
    "transcription": "전사",
    "translation": "번역",
    "render": "작업 완료",
}
PHASE_SEQUENCE = (
    "audio extraction",
    "transcription",
    "translation",
)
JOB_ENDPOINT_KEY = "render"
STAGE_SEQUENCE = (*PHASE_SEQUENCE, JOB_ENDPOINT_KEY)
OPERATION_PHASES = {
    "extract": ("audio extraction",),
    "transcribe": ("transcription",),
    "translate": ("translation",),
    "full": PHASE_SEQUENCE,
}
STAGE_STATE_LABELS = {
    "done": "완료",
    "running": "진행 중",
    "waiting": "대기",
    "paused": "일시 정지",
    "blocked": "중단",
    "stopped": "정지",
    "failed": "실패",
    "pending": "대기",
}


def _job_progress_step(
    job: Any,
    key: str,
    state: str,
    *,
    kind: str,
) -> dict[str, Any]:
    chunks_created = int(job.chunks_created)
    chunks_total_estimate = int(getattr(job, "chunks_total_estimate", 0))
    transcription_total = max(chunks_created, chunks_total_estimate)
    chunk_counts = {
        "transcription": (job.chunks_completed, transcription_total),
        "translation": (
            job.translation_chunks_completed,
            job.translation_chunks_total,
        ),
    }
    completed, total = chunk_counts.get(key, (0, 0))
    percent = (
        round(completed * 100 / total)
        if total
        else (100 if state == "done" else 0)
    )
    if state != "done":
        percent = min(99, percent)
    count_label = ""
    total_is_estimate = (
        key == "transcription" and chunks_total_estimate > chunks_created
    )
    if total:
        count_label = (
            f"{completed}/{('≈' if total_is_estimate else '')}{total}"
        )
    return {
        "key": key,
        "label": JOB_STAGE_LABELS.get(key, key),
        "kind": kind,
        "state": state,
        "state_label": STAGE_STATE_LABELS[state],
        "completed": completed,
        "total": total,
        "total_is_estimate": total_is_estimate,
        "percent": percent,
        "progress_label": " · ".join(
            part
            for part in (STAGE_STATE_LABELS[state], count_label)
            if part
        ),
        "display_label": (
            {
                "done": "작업 완료",
                "running": "작업 마무리 중",
                "waiting": "작업 완료 대기",
                "pending": "작업 완료 대기",
                "paused": "작업 일시 정지",
                "blocked": "작업 중단",
                "stopped": "작업 정지",
                "failed": "작업 실패",
            }[state]
            if kind == "endpoint"
            else JOB_STAGE_LABELS.get(key, key)
        ),
    }


def job_pipeline_phase_view(job: Any) -> list[dict[str, Any]]:
    """실제 파이프라인의 세 phase 상태를 작업 범위와 무관하게 계산한다."""
    operation = str(job.operation)
    domain_phase = str(job.phase)
    domain_state = str(job.state)
    active = {
        "extraction": "audio extraction",
        "transcription": "transcription",
        "translation": "translation",
    }.get(domain_phase)
    finished_through = -1
    active_state = domain_state
    if domain_phase == "complete" and domain_state == "done":
        finished_through = {
            "extract": 0,
            "transcribe": 1,
            "translate": 2,
            "full": 2,
        }.get(operation, 2)
        active = None
    elif domain_phase == "render":
        finished_through = len(PHASE_SEQUENCE) - 1

    view: list[dict[str, Any]] = []
    active_index = (
        PHASE_SEQUENCE.index(active) if active in PHASE_SEQUENCE else None
    )
    for index, phase in enumerate(PHASE_SEQUENCE):
        if index <= finished_through or (
            active_index is not None and index < active_index
        ):
            state = "done"
        elif phase == active:
            state = active_state
        else:
            state = "pending"
        view.append(
            _job_progress_step(job, phase, state, kind="phase")
        )
    return view


def job_stage_view(job: Any) -> list[dict[str, Any]]:
    """선택한 phase와 작업 완료 endpoint를 사용자 표시용으로 돌려준다."""
    operation = str(job.operation)
    phases_by_key = {
        str(phase["key"]): phase for phase in job_pipeline_phase_view(job)
    }
    phases = [
        dict(phases_by_key[key])
        for key in OPERATION_PHASES.get(operation, PHASE_SEQUENCE)
    ]
    if (
        operation == "transcribe"
        and phases
        and phases[0]["state"] == "pending"
    ):
        phases[0] = _job_progress_step(
            job,
            "transcription",
            "waiting",
            kind="phase",
        )

    phase = str(job.phase)
    state = str(job.state)
    if phase == "complete" and state == "done":
        endpoint_state = "done"
    elif phase == "render" and state in {
        "waiting",
        "running",
        "paused",
        "blocked",
        "stopped",
        "failed",
    }:
        endpoint_state = state
    else:
        endpoint_state = "pending"
    endpoint = _job_progress_step(
        job,
        JOB_ENDPOINT_KEY,
        endpoint_state,
        kind="endpoint",
    )
    return [*phases, endpoint]


def job_progress_view(job: Any) -> dict[str, Any]:
    """작업 목록에 표시할 전체 파이프라인 진행 상태를 계산한다."""
    stages = job_stage_view(job)
    percent = (
        int(sum(stage["percent"] for stage in stages) / len(stages) + 0.5)
        if stages
        else 0
    )
    current = next(
        (
            stage
            for stage in stages
            if stage["state"]
            in {
                "running",
                "blocked",
                "stopped",
                "failed",
                "paused",
                "waiting",
            }
        ),
        stages[-1] if stages else None,
    )
    phases = [stage for stage in stages if stage["kind"] == "phase"]
    endpoint = next(
        (stage for stage in stages if stage["kind"] == "endpoint"),
        None,
    )
    return {
        "stages": stages,
        "phases": phases,
        "endpoint": endpoint,
        "percent": percent,
        "current": current,
        "complete": bool(stages) and all(
            stage["state"] == "done" for stage in stages
        ),
    }


def job_contract_status_label(job: Any) -> str:
    """Render the stable phase/state contract without exposing legacy status."""
    if str(job.state) == JobState.DONE:
        return JOB_STATUS_LABELS.get(str(job.status), "완료")
    phase = JOB_PHASE_LABELS.get(str(job.phase), str(job.phase))
    state = JOB_STATE_LABELS.get(str(job.state), str(job.state))
    return " · ".join(part for part in (phase, state) if part)


def translation_generation_view(
    generations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    views = []
    last_index = len(generations) - 1
    for index, generation in enumerate(generations):
        prompt_category_name = str(
            generation.get("prompt_category_name") or ""
        ).strip()
        prompt_revision_number = generation.get("prompt_revision_number")
        prompt_label = (
            f"{prompt_category_name} · v{prompt_revision_number}"
            if prompt_category_name and prompt_revision_number is not None
            else None
        )
        views.append(
            {
                **dict(generation),
                "state_label": TRANSLATION_GENERATION_STATE_LABELS.get(
                    str(generation.get("state", "")),
                    str(generation.get("state", "")),
                ),
                "origin_label": TRANSLATION_GENERATION_ORIGIN_LABELS.get(
                    str(generation.get("origin", "")),
                    str(generation.get("origin", "")),
                ),
                "prompt_label": prompt_label,
                "is_default_base": index == last_index - 1,
                "is_default_candidate": index == last_index,
            }
        )
    return views


def translation_generation_history(
    service: SubtitleOrchestrator,
    job_id: str,
) -> list[dict[str, Any]]:
    return translation_generation_view(
        service.store.list_translation_generations(job_id)
    )


def transcript_revision_history(
    service: SubtitleOrchestrator,
    job: Any,
) -> list[dict[str, Any]]:
    revisions = service.store.transcript_revisions(job.id)
    history = [
        {
            **revision,
            "revision_number": index,
            "origin_label": TRANSCRIPT_REVISION_ORIGIN_LABELS.get(
                str(revision.get("origin", "")),
                str(revision.get("origin", "")),
            ),
            "is_active": revision["id"] == job.transcript_revision_id,
            "selector_label": f"전사 리비전 {index}",
        }
        for index, revision in enumerate(revisions, start=1)
    ]
    if job.transcript_path and not any(
        revision["is_active"] for revision in history
    ):
        history.append(
            {
                "id": "__current__",
                "revision_number": None,
                "origin": "legacy",
                "origin_label": TRANSCRIPT_REVISION_ORIGIN_LABELS["legacy"],
                "backend": str(job.options.get("backend", "")),
                "model_revision": "",
                "created_at": job.updated_at,
                "is_active": True,
                "selector_label": "현재 전사",
            }
        )
    return history


def translation_source_texts(
    service: SubtitleOrchestrator,
    job: Any,
    generation: Mapping[str, Any],
) -> dict[str, str]:
    jobs_root = service.settings.jobs_dir.resolve()
    raw_paths = [generation.get("transcript_artifact_path")]
    if job.transcript_path not in raw_paths:
        raw_paths.append(job.transcript_path)
    for raw_path in raw_paths:
        if not raw_path:
            continue
        try:
            artifact = Path(str(raw_path)).resolve()
            artifact.relative_to(jobs_root)
            if (
                not artifact.is_file()
                or sha256_file(artifact) != generation.get("transcript_hash")
            ):
                continue
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                continue
            return {
                str(segment["id"]): str(segment["text"])
                for segment in validate_transcript(payload)
            }
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            continue
    return {}


def subtitle_generation_view(
    generations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            **dict(generation),
            "origin_label": SUBTITLE_GENERATION_ORIGIN_LABELS.get(
                str(generation.get("origin", "")),
                str(generation.get("origin", "")),
            ),
        }
        for generation in generations
    ]


def _webgpu_stage_progress(
    job: Any,
    stage_key: str,
) -> tuple[int | None, str]:
    stage = next(
        (
            item
            for item in job_pipeline_phase_view(job)
            if item["key"] == stage_key
        ),
        None,
    )
    if stage is None:
        return None, JOB_STATUS_LABELS.get(str(job.status), str(job.status))
    completed = int(stage["completed"])
    total = int(stage["total"])
    if total <= 0:
        return None, str(stage["state_label"])
    total_prefix = "≈" if stage["total_is_estimate"] else ""
    return (
        int(stage["percent"]),
        f"{completed} / {total_prefix}{total}",
    )


def _webgpu_backend_label(job: Any) -> str:
    backend = str(job.options.get("backend", "")).strip()
    return STT_BACKEND_LABELS.get(backend, backend)


def _webgpu_gpu_view(snapshot: GpuSnapshot) -> dict[str, Any]:
    if snapshot.available and snapshot.devices:
        device = snapshot.devices[0]
        return {
            "available": True,
            "index": device.index,
            "display_name": device.display_name,
            "utilization_percent": device.utilization_percent,
            "memory_percent": device.memory_percent,
            "memory_used_gib": device.memory_used_gib,
            "memory_total_gib": device.memory_total_gib,
            "temperature_celsius": device.temperature_celsius,
            "power_watts": device.power_watts,
        }
    return {
        "available": False,
        "index": "",
        "display_name": "GPU 메트릭 없음",
        "utilization_percent": None,
        "memory_percent": None,
        "memory_used_gib": None,
        "memory_total_gib": None,
        "temperature_celsius": None,
        "power_watts": None,
    }


def webgpu_scene_context(
    service: SubtitleOrchestrator,
    snapshot: GpuSnapshot,
    *,
    audio_workers: int,
) -> dict[str, Any]:
    """Build the 3D dashboard from values the application actually records."""
    jobs = service.store.list_jobs(
        limit=None,
        include_comparison_transcriptions=False,
    )
    slots = dashboard_pipeline_slots(jobs, audio_workers=audio_workers)

    phase_jobs = [job for job in jobs if job.state == JobState.RUNNING]
    phase_job_views = []
    for job in phase_jobs[:WEBGPU_PHASE_JOB_LIMIT]:
        progress = job_progress_view(job)
        phase_job_views.append(
            {
                "source_rel": job.source_rel,
                "operation": JOB_OPERATION_LABELS.get(
                    str(job.operation),
                    str(job.operation),
                ),
                "percent": progress["percent"],
                "phases": progress["phases"],
                "endpoint": progress["endpoint"],
            }
        )

    worker_total = sum(int(slot["capacity"]) for slot in slots)
    worker_active = sum(
        min(int(slot["active"]), int(slot["capacity"])) for slot in slots
    )
    worker_available = max(0, worker_total - worker_active)

    waiting = [
        job
        for job in jobs
        if job.state == JobState.WAITING
    ]
    queue = []
    for job in waiting[:WEBGPU_QUEUE_LIMIT]:
        progress = job_progress_view(job)
        current = progress["current"] or {}
        queue.append(
            {
                "title": Path(job.source_rel).name,
                "stage": str(current.get("label", "파이프라인")),
                "status": JOB_STATUS_LABELS.get(job.status, job.status),
            }
        )

    transport_cutoff = time.time() - WEBGPU_TRANSPORT_WINDOW_SECONDS

    def stopped_view(
        job: Any,
        *,
        include_error: bool = False,
    ) -> dict[str, Any]:
        view: dict[str, Any] = {
            "source_rel": job.source_rel,
            "phase": job.phase,
            "state": job.state,
            "reason_code": job.reason_code,
            "status": job_contract_status_label(job),
            "status_label": job_contract_status_label(job),
            "transport_pending": (
                float(job.status_updated_at) >= transport_cutoff
            ),
        }
        if include_error and job.error:
            view["error"] = str(job.error)
        return view

    def newest_status_first(values: Iterable[Any]) -> list[Any]:
        return sorted(
            values,
            key=lambda job: (float(job.status_updated_at), job.id),
            reverse=True,
        )

    paused_jobs = newest_status_first(
        job for job in jobs if job.state == JobState.PAUSED
    )
    stopped_jobs = newest_status_first(
        job for job in jobs if job.state == JobState.STOPPED
    )
    blocked_jobs = newest_status_first(
        job for job in jobs if job.state == JobState.BLOCKED
    )
    failed_jobs = newest_status_first(
        job for job in jobs if job.state == JobState.FAILED
    )

    stopped_views = [
        stopped_view(job) for job in stopped_jobs[:WEBGPU_STOPPED_LIMIT]
    ]
    blocked_views = [
        stopped_view(job) for job in blocked_jobs[:WEBGPU_STOPPED_LIMIT]
    ]
    paused_views = [
        stopped_view(job) for job in paused_jobs[:WEBGPU_STOPPED_LIMIT]
    ]
    failed_views = [
        stopped_view(job, include_error=True)
        for job in failed_jobs[:WEBGPU_STOPPED_LIMIT]
    ]
    transport_budget = worker_available
    transport_count = 0
    for views in (paused_views, blocked_views, stopped_views, failed_views):
        for view in views:
            active = bool(view.pop("transport_pending")) and transport_budget > 0
            view["transport_active"] = active
            if active:
                transport_budget -= 1
                transport_count += 1

    completed_jobs = [job for job in jobs if job.state == JobState.DONE]
    completed = []
    for job in completed_jobs[:WEBGPU_COMPLETED_LIMIT]:
        detail = [JOB_STATUS_LABELS.get(job.status, job.status)]
        backend_label = _webgpu_backend_label(job)
        if backend_label:
            detail.append(backend_label)
        completed.append(
            {
                "source_rel": job.source_rel,
                "detail": " · ".join(detail),
            }
        )

    rendering_jobs = [
        job
        for job in jobs
        if job.phase == "render" and job.state == JobState.RUNNING
    ]
    rendering = (
        {
            "source_rel": rendering_jobs[0].source_rel,
            "detail": JOB_STATUS_LABELS["rendering"],
        }
        if rendering_jobs
        else None
    )

    try:
        media_browser = service.library.browse()
    except ValueError:
        media_browser = {"folders": [], "files": []}
    folders = list(media_browser["folders"])
    media_tree = [
        {
            "path": str(folder["path"]),
            "shape": "mixed",
            "dirs": None,
            "files": None,
        }
        for folder in folders[:WEBGPU_MEDIA_ZONE_LIMIT]
    ]
    if not media_tree and media_browser["files"]:
        media_tree.append(
            {
                "path": "미디어 루트",
                "shape": "flat",
                "dirs": None,
                "files": len(media_browser["files"]),
            }
        )

    return {
        "slots": slots,
        "phase_jobs": phase_job_views,
        "phase_jobs_rest": max(
            0,
            len(phase_jobs) - len(phase_job_views),
        ),
        "workers": {
            "total": worker_total,
            "active": worker_active,
            "transporting": transport_count,
            "idle": max(0, worker_available - transport_count),
        },
        "state_counts": dashboard_state_counts(jobs),
        "queue": queue,
        "queue_rest": max(0, len(waiting) - len(queue)),
        "blocked": blocked_views,
        "blocked_rest": max(0, len(blocked_jobs) - WEBGPU_STOPPED_LIMIT),
        "stopped": stopped_views,
        "stopped_rest": max(0, len(stopped_jobs) - WEBGPU_STOPPED_LIMIT),
        "paused": paused_views,
        "paused_rest": max(0, len(paused_jobs) - WEBGPU_STOPPED_LIMIT),
        "failed": failed_views,
        "failed_rest": max(0, len(failed_jobs) - WEBGPU_STOPPED_LIMIT),
        "completed": completed,
        "completed_total": len(completed_jobs),
        "rendering": rendering,
        "gpu": _webgpu_gpu_view(snapshot),
        "media_tree": media_tree,
        "media_tree_rest": max(0, len(folders) - len(media_tree)),
    }


def comparison_audio_stage(jobs: Sequence[Any]) -> dict[str, str]:
    """여러 비교 작업의 오디오 준비 상태를 하나의 단계로 집계한다."""
    audio_stages = [
        stage
        for job in jobs
        for stage in job_pipeline_phase_view(job)
        if stage["key"] == "audio extraction"
    ]
    states = {str(stage["state"]) for stage in audio_stages}
    if audio_stages and states == {"done"}:
        state = "done"
    elif "failed" in states:
        state = "failed"
    elif "blocked" in states:
        state = "blocked"
    elif "running" in states or "done" in states:
        state = "running"
    elif "waiting" in states:
        state = "waiting"
    else:
        state = "pending"
    return {
        "label": "오디오 추출",
        "state": state,
        "state_label": STAGE_STATE_LABELS[state],
    }


EVENT_LEVEL_LABELS = {
    "info": "정보",
    "warning": "주의",
    "error": "오류",
}
def media_actor_label(actors: Any) -> str:
    """Return the actor line for a media card.

    A single actor is named outright, two or more collapse to "Group" because
    the card only has one line for it, and no actor at all reads "Unknown".
    """
    names = [
        str(name).strip() for name in (actors or ()) if str(name).strip()
    ]
    if not names:
        return "Unknown"
    return names[0] if len(names) == 1 else "Group"


def _library_progress_entry(
    entry: Mapping[str, Any],
    latest_jobs: Mapping[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
    media_items = list(entry["media"])
    total = len(media_items)
    counts = {
        "done": 0,
        "running": 0,
        "queued": 0,
        "attention": 0,
        "unprocessed": 0,
    }
    for media in media_items:
        source_rel = str(media["path"])
        latest = latest_jobs.get(source_rel)
        if bool(media["has_subtitle"]) or (
            latest is not None and latest.state == JobState.DONE
        ):
            counts["done"] += 1
        elif latest is not None and latest.state in {
            JobState.PAUSED,
            JobState.BLOCKED,
            JobState.STOPPED,
            JobState.FAILED,
        }:
            counts["attention"] += 1
        elif latest is not None and latest.state == JobState.RUNNING:
            counts["running"] += 1
        elif latest is not None and latest.state == JobState.WAITING:
            counts["queued"] += 1
        else:
            counts["unprocessed"] += 1
    active = counts["running"] + counts["queued"] + counts["attention"]
    return {
        "kind": kind,
        "name": entry["name"],
        "path": entry["path"],
        "image_path": entry["image_path"],
        "done": counts["done"],
        "total": total,
        "remaining": total - counts["done"],
        "active": active,
        "attention": counts["attention"],
        "segments": [
            {
                "state": state,
                "percent": round(counts[state] * 100 / total, 1),
            }
            for state in (
                "done",
                "running",
                "queued",
                "attention",
                "unprocessed",
            )
            if counts[state]
        ],
    }


def library_progress_view(service: SubtitleOrchestrator) -> list[dict[str, Any]]:
    latest_jobs = service.store.latest_jobs_by_source()
    actors: list[dict[str, Any]] = []
    for actor in service.library.actor_library_entries():
        actors.append(
            _library_progress_entry(actor, latest_jobs, kind="actor")
        )
    actors.sort(
        key=lambda actor: (
            -int(bool(actor["active"])),
            -int(actor["attention"]),
            -int(actor["active"]),
            -int(actor["remaining"]),
            str(actor["name"]).casefold(),
        )
    )

    collections: list[dict[str, Any]] = []
    variety = service.library.collection_library_entry(
        "Variety",
        name="버라이어티",
    )
    if variety is not None:
        collections.append(
            _library_progress_entry(variety, latest_jobs, kind="collection")
        )
    return [*collections, *actors[:ACTOR_PROGRESS_LIMIT]]


def dashboard_state_counts(jobs: Sequence[Any]) -> dict[str, int]:
    return {
        "running": sum(job.state == JobState.RUNNING for job in jobs),
        "waiting": sum(job.state == JobState.WAITING for job in jobs),
        "paused": sum(job.state == JobState.PAUSED for job in jobs),
        "blocked": sum(job.state == JobState.BLOCKED for job in jobs),
        "stopped": sum(job.state == JobState.STOPPED for job in jobs),
        "failed": sum(job.state == JobState.FAILED for job in jobs),
        "completed": sum(job.state == JobState.DONE for job in jobs),
    }


def dashboard_pipeline_slots(
    jobs: Sequence[Any],
    *,
    audio_workers: int,
) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for stage, phase_name, stage_key, capacity in (
        ("추출", "extraction", "audio extraction", max(1, audio_workers)),
        ("전사", "transcription", "transcription", 1),
        ("번역", "translation", "translation", 1),
    ):
        active = [
            job
            for job in jobs
            if job.phase == phase_name and job.state == JobState.RUNNING
        ]
        slot: dict[str, Any] = {
            "stage": stage,
            "capacity": capacity,
            "active": len(active),
            "job": None,
            "job_id": None,
            "detail": "",
            "percent": None,
        }
        if active:
            job = active[0]
            percent, progress_detail = _webgpu_stage_progress(job, stage_key)
            detail = []
            if stage_key == "transcription":
                backend = _webgpu_backend_label(job)
                if backend:
                    detail.append(backend)
            if progress_detail:
                detail.append(progress_detail)
            if len(active) > 1:
                detail.append(f"외 {len(active) - 1}건")
            slot.update(
                {
                    "job": job.source_rel,
                    "job_id": job.id,
                    "detail": " · ".join(detail),
                    "percent": percent,
                }
            )
        slots.append(slot)
    return slots


def dashboard_job_view(job: Any, *, now: float) -> dict[str, Any]:
    pipeline = job_progress_view(job)
    backend = _webgpu_backend_label(job)
    prompt = (
        job.prompt_category_name
        if job.operation in {"translate", "full"}
        else ""
    )
    metadata = [
        JOB_OPERATION_LABELS.get(str(job.operation), str(job.operation)),
        backend,
        prompt,
    ]
    return {
        "id": job.id,
        "source_rel": job.source_rel,
        "status": job.status,
        "phase": job.phase,
        "state": job.state,
        "reason_code": job.reason_code,
        "attempt": job.attempt,
        "status_label": job_contract_status_label(job),
        "phases": pipeline["phases"],
        "endpoint": pipeline["endpoint"],
        "percent": pipeline["percent"],
        "elapsed_seconds": max(0, now - float(job.status_updated_at)),
        "changed_at": job.status_updated_at,
        "metadata": " · ".join(part for part in metadata if part),
        "error": str(job.error or ""),
        "can_retry": job.can_retry,
        "can_delete": job.can_delete_record,
    }


def dashboard_2d_data(
    service: SubtitleOrchestrator,
    *,
    audio_workers: int,
    include_library_progress: bool = True,
) -> dict[str, Any]:
    jobs = service.store.list_jobs(
        limit=None,
        include_comparison_transcriptions=False,
    )
    now = time.time()
    running = [job for job in jobs if job.state == JobState.RUNNING]
    attention = [
        job
        for job in jobs
        if job.state
        in {
            JobState.PAUSED,
            JobState.BLOCKED,
            JobState.STOPPED,
            JobState.FAILED,
        }
    ]
    completed = [job for job in jobs if job.state == JobState.DONE]
    waiting = sorted(
        (
            job
            for job in jobs
            if job.state == JobState.WAITING
        ),
        key=lambda job: (job.created_at, job.id),
    )

    attention_views = []
    for job in attention[:DASHBOARD_ATTENTION_LIMIT]:
        view = dashboard_job_view(job, now=now)
        if job.state == JobState.PAUSED:
            view.update(
                status_key="paused",
                status_label="일시 정지",
                action="resume",
            )
        elif job.state == JobState.STOPPED:
            view.update(
                status_key="stopped",
                status_label="정지",
                action="retry",
            )
        elif job.state == JobState.BLOCKED:
            view.update(
                status_key="blocked",
                status_label="중단",
                action="retry",
            )
        else:
            view.update(
                status_key="failed",
                status_label="실패",
                action="retry",
            )
        current = job_progress_view(job)["current"] or {}
        blocked_stage = str(job.blocked_stage or "")
        stage_detail = (
            JOB_STAGE_LABELS.get(blocked_stage, blocked_stage)
            if job.state in {JobState.BLOCKED, JobState.FAILED} and blocked_stage
            else str(current.get("label", ""))
        )
        if current.get("key") == blocked_stage and current.get("total"):
            estimate = "≈" if current.get("total_is_estimate") else ""
            stage_detail += (
                f" {current['completed']}/{estimate}{current['total']}"
            )
        view["stage_detail"] = stage_detail
        view["attention_detail"] = (
            str(job.error or "처리 오류")
            if job.state == JobState.FAILED
            else (
                "사용자 일시 정지"
                if job.state == JobState.PAUSED
                else JOB_REASON_LABELS.get(
                    str(job.reason_code or ""),
                    "사용자 정지"
                    if job.state == JobState.STOPPED
                    else "외부 조건 확인 필요",
                )
            )
        )
        attention_views.append(view)

    completed_views = []
    for job in completed[:DASHBOARD_COMPLETED_LIMIT]:
        view = dashboard_job_view(job, now=now)
        view["result_label"] = {
            "audio_completed": "추출 완료",
            "transcription_completed": "전사 완료",
            "completed": "자막 완료",
        }.get(job.status, view["status_label"])
        completed_views.append(view)

    queue_views = []
    for position, job in enumerate(
        waiting[:DASHBOARD_QUEUE_LIMIT],
        start=1,
    ):
        current = job_progress_view(job)["current"] or {}
        queue_views.append(
            {
                "position": position,
                "id": job.id,
                "source_rel": job.source_rel,
                "stage": current.get("label", "대기"),
                "status": job_contract_status_label(job),
            }
        )

    slots = dashboard_pipeline_slots(jobs, audio_workers=audio_workers)
    return {
        "dashboard_counts": dashboard_state_counts(jobs),
        "pipeline_slots": slots,
        "pipeline_active": sum(int(slot["active"]) for slot in slots),
        "pipeline_capacity": sum(int(slot["capacity"]) for slot in slots),
        "running_jobs": [dashboard_job_view(job, now=now) for job in running],
        "attention_jobs": attention_views,
        "attention_rest": max(0, len(attention) - len(attention_views)),
        "completed_jobs": completed_views,
        "queue": queue_views,
        "queue_rest": max(0, len(waiting) - len(queue_views)),
        "pausable_translation_count": sum(
            job.can_pause_translation for job in jobs
        ),
        "stoppable_job_count": sum(job.can_stop for job in jobs),
        "retriable_job_count": sum(job.can_retry for job in jobs),
        "library_progress": (
            library_progress_view(service) if include_library_progress else []
        ),
        "updated_at": now,
    }


def flatten_media_display_folders(
    library: MediaLibrary,
    browser: Mapping[str, object],
    rules: Sequence[object],
) -> dict[str, object]:
    """Lift files out of directories removed by a display-path rule."""
    if not rules or not browser.get("folders"):
        return dict(browser)
    current_folder = str(browser.get("current_folder", ""))
    files = list(browser.get("files", ()))
    folders = []
    for folder in browser.get("folders", ()):
        if not isinstance(folder, Mapping):
            continue
        child = library.browse(str(folder["path"]))
        child_files = list(child["files"])
        lifted_files = []
        for media in child_files:
            source_path = str(media["path"])
            display_path = shorten_display_path(source_path, rules)
            display_parent = Path(display_path).parent
            display_parent_text = (
                "" if display_parent == Path(".") else display_parent.as_posix()
            )
            if (
                display_path != source_path
                and display_parent_text.casefold() == current_folder.casefold()
            ):
                lifted_files.append(media)
        can_lift_folder = (
            bool(child_files)
            and len(lifted_files) == len(child_files)
            and not child["folders"]
            and len(files) + len(lifted_files) <= library.maximum_files
        )
        if can_lift_folder:
            files.extend(lifted_files)
        else:
            folders.append(dict(folder))
    files.sort(key=lambda media: str(media["path"]).casefold())
    return {**browser, "folders": folders, "files": files}


def path_display_template_context(request: Request) -> dict[str, object]:
    service = getattr(request.app.state, "orchestrator", None)
    return {
        "path_display_rules": (
            service.path_display_rules if service is not None else ()
        )
    }


@pass_context
def display_path_filter(context: Mapping[str, Any], value: object) -> str:
    rules = context.get("path_display_rules", ())
    return shorten_display_path(value, rules)


@pass_context
def display_filename_filter(
    context: Mapping[str, Any],
    value: object,
) -> str:
    return Path(display_path_filter(context, value)).name


@pass_context
def display_parent_path_filter(
    context: Mapping[str, Any],
    value: object,
) -> str:
    parent = Path(display_path_filter(context, value)).parent
    return "" if str(parent) == "." else str(parent)


@pass_context
def display_paths_filter(
    context: Mapping[str, Any],
    values: Sequence[object],
) -> list[str]:
    return [display_path_filter(context, value) for value in values]


TEMPLATES = Jinja2Templates(
    directory=PACKAGE_DIR / "templates",
    context_processors=[path_display_template_context],
)
TEMPLATES.env.filters["datetime"] = format_kst_timestamp
TEMPLATES.env.filters["datetime_iso"] = format_kst_iso
TEMPLATES.env.filters["filesize"] = lambda value: (
    f"{float(value) / 1024 / 1024 / 1024:.2f} GiB"
)


def format_storage_size(value: object) -> str:
    size = max(0.0, float(value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return (
                f"{size:.0f} {unit}"
                if unit == "B"
                else f"{size:.2f} {unit}"
            )
        size /= 1024
    return "0 B"


TEMPLATES.env.filters["storage_size"] = format_storage_size
TEMPLATES.env.filters["job_status"] = lambda value: JOB_STATUS_LABELS.get(
    str(value),
    str(value),
)
TEMPLATES.env.filters["job_contract_status"] = job_contract_status_label
TEMPLATES.env.filters["job_operation"] = lambda value: (
    JOB_OPERATION_LABELS.get(str(value), str(value))
)
TEMPLATES.env.filters["stt_backend"] = lambda value: (
    STT_BACKEND_LABELS.get(str(value), str(value))
)
TEMPLATES.env.filters["job_stage"] = lambda value: JOB_STAGE_LABELS.get(
    str(value),
    str(value),
)
TEMPLATES.env.filters["job_stages"] = job_stage_view
TEMPLATES.env.filters["actor_label"] = media_actor_label
TEMPLATES.env.filters["job_progress"] = job_progress_view
TEMPLATES.env.filters["event_level"] = lambda value: EVENT_LEVEL_LABELS.get(
    str(value),
    str(value),
)
TEMPLATES.env.filters["display_path"] = display_path_filter
TEMPLATES.env.filters["display_paths"] = display_paths_filter
TEMPLATES.env.filters["filename"] = display_filename_filter
TEMPLATES.env.filters["parent_path"] = display_parent_path_filter


def format_media_duration(value: object) -> str:
    if value is None:
        return "알 수 없음"
    try:
        total_seconds = max(0, round(float(value)))
    except (TypeError, ValueError):
        return "알 수 없음"
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


TEMPLATES.env.filters["duration"] = format_media_duration
TEMPLATES.env.filters["clock"] = lambda value: format_kst_timestamp(value)[11:19]


def decode_source_groups(values: Sequence[str] | None) -> list[str]:
    """Decode multipart card selections into their individual media paths."""
    decoded: list[str] = []
    for value in values or ():
        try:
            group = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(
                "멀티파트 미디어 선택 정보가 올바르지 않습니다."
            ) from error
        if (
            not isinstance(group, list)
            or not group
            or any(
                not isinstance(path, str) or not path.strip()
                for path in group
            )
        ):
            raise ValueError(
                "멀티파트 미디어 선택 정보가 올바르지 않습니다."
            )
        decoded.extend(path.strip() for path in group)
    return decoded


class JobChangeHook:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._version = 0
        self._changed = asyncio.Event()

    @property
    def version(self) -> int:
        return self._version

    def publish(self, _job_id: str) -> None:
        try:
            self._loop.call_soon_threadsafe(self._mark_changed)
        except RuntimeError:
            return

    def _mark_changed(self) -> None:
        self._version += 1
        self._changed.set()

    async def wait(self, version: int, timeout: float = 15.0) -> int:
        while self._version == version:
            self._changed.clear()
            if self._version != version:
                break
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            except TimeoutError:
                break
        return self._version


def create_app(settings: WebSettings | None = None) -> FastAPI:
    configured_settings = settings or WebSettings.from_env()
    authentication_enabled = bool(configured_settings.admin_password.strip())
    gpu_monitor = PrometheusGpuMonitor(
        configured_settings.gpu_prometheus_url,
        bearer_token=configured_settings.gpu_prometheus_token,
        timeout_seconds=configured_settings.gpu_metrics_timeout_seconds,
        cache_seconds=configured_settings.gpu_metrics_refresh_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        orchestrator = SubtitleOrchestrator(configured_settings)
        job_change_hook = JobChangeHook(asyncio.get_running_loop())
        orchestrator.store.set_change_hook(job_change_hook.publish)
        app.state.orchestrator = orchestrator
        app.state.job_change_hook = job_change_hook
        orchestrator.start()
        try:
            yield
        finally:
            orchestrator.store.set_change_hook(None)
            orchestrator.stop()

    app = FastAPI(
        title="stt-to-subtitle orchestrator",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.authentication_enabled = authentication_enabled
    app.state.gpu_monitor = gpu_monitor

    @app.middleware("http")
    async def allow_same_origin_webxr(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault(
            "Permissions-Policy",
            "xr-spatial-tracking=(self)",
        )
        if (
            request.method == "POST"
            and (
                request.url.path == "/jobs"
                or request.url.path.startswith("/jobs/")
                or request.url.path.startswith("/comparisons/")
            )
            and response.status_code
            in {status.HTTP_302_FOUND, status.HTTP_303_SEE_OTHER}
            and response.headers.get("location") != "/login"
        ):
            referer = request.headers.get("referer", "").strip()
            parsed_referer = urlsplit(referer)
            if (
                parsed_referer.scheme == request.url.scheme
                and parsed_referer.netloc == request.url.netloc
                and parsed_referer.path.startswith("/")
                and not parsed_referer.path.startswith("//")
            ):
                return_location = parsed_referer.path
                if parsed_referer.query:
                    return_location += f"?{parsed_referer.query}"
                response.headers["location"] = return_location
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=configured_settings.session_secret
        or secrets.token_urlsafe(48),
        session_cookie="stt_web_session",
        same_site="strict",
        https_only=configured_settings.secure_cookie,
        max_age=12 * 60 * 60,
    )
    app.mount(
        "/static",
        StaticFiles(directory=PACKAGE_DIR / "static"),
        name="static",
    )

    def orchestrator(request: Request) -> SubtitleOrchestrator:
        return request.app.state.orchestrator

    def is_authenticated(request: Request) -> bool:
        return (
            not authentication_enabled
            or request.session.get("authenticated") is True
        )

    def login_redirect() -> RedirectResponse:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

    def dashboard_location(folder: str = "", **values: object) -> str:
        query = {
            key: value
            for key, value in {"folder": folder, **values}.items()
            if value not in (None, "")
        }
        if folder:
            return f"/media?{urlencode(query)}"
        return f"/?{urlencode(query)}" if query else "/"

    def media_location(folder: str = "", **values: object) -> str:
        query = {
            key: value
            for key, value in {"folder": folder, **values}.items()
            if value not in (None, "")
        }
        return f"/media?{urlencode(query)}" if query else "/media"

    def job_action_location(
        job_id: str,
        return_folder: str | None,
        **values: object,
    ) -> str:
        if return_folder is None:
            return f"/jobs/{job_id}"
        return dashboard_location(return_folder, **values)

    def job_list_action_location(
        *,
        return_folder: str,
        return_jobs_page: int,
        return_operation: str = "",
        return_phase: str = "",
        return_state: str = "",
        return_reason_code: str = "",
        return_status_group: str = "",
        return_stage_filter: str = "",
        **values: object,
    ) -> str:
        filters = normalize_job_list_filters(
            operation_filter=return_operation or None,
            phase_filter=return_phase or None,
            state_filter=return_state or None,
            reason_filter=return_reason_code or None,
            status_group=return_status_group or None,
            stage_filter=return_stage_filter or None,
        )
        if any(filters):
            return job_filter_url(
                operation_filter=filters[0],
                phase_filter=filters[1],
                state_filter=filters[2],
                reason_filter=filters[3],
                jobs_page=max(1, return_jobs_page),
                **values,
            )
        if return_folder:
            return media_location(return_folder, **values)
        query = {
            key: value
            for key, value in {
                "jobs_page": (
                    return_jobs_page if return_jobs_page > 1 else None
                ),
                **values,
            }.items()
            if value not in (None, "")
        }
        return f"/jobs?{urlencode(query)}" if query else "/jobs"

    def job_stats(service: SubtitleOrchestrator) -> dict[str, int]:
        return {
            group: service.store.count_jobs(
                states=states,
                include_comparison_transcriptions=False,
            )
            for group, states in JOB_STATUS_GROUPS.items()
        }

    def normalize_job_list_filters(
        *,
        operation_filter: str | None = None,
        phase_filter: str | None = None,
        state_filter: str | None = None,
        reason_filter: str | None = None,
        status_group: str | None = None,
        stage_filter: str | None = None,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        operation_filter = operation_filter or None
        phase_filter = phase_filter or None
        state_filter = state_filter or None
        reason_filter = reason_filter or None
        if status_group and state_filter is None:
            if status_group not in JOB_STATUS_GROUPS:
                raise ValueError("지원하지 않는 작업 상태 필터입니다.")
            state_filter = "done" if status_group == "completed" else status_group
        if stage_filter:
            if stage_filter not in JOB_STAGE_FILTERS:
                raise ValueError("지원하지 않는 작업 단계 필터입니다.")
            legacy_phase, legacy_state, legacy_operation = {
                "extraction": ("extraction", None, None),
                "transcription": ("transcription", None, None),
                "transcription_waiting": (
                    "transcription",
                    "waiting",
                    None,
                ),
                "transcription_running": (
                    "transcription",
                    "running",
                    None,
                ),
                "transcription_completed": (
                    "completion",
                    "done",
                    "transcribe",
                ),
                "translation": ("translation", None, None),
                "translation_waiting": (
                    "translation",
                    "waiting",
                    None,
                ),
                "translation_running": (
                    "translation",
                    "running",
                    None,
                ),
                "translation_completed": (
                    "completion",
                    None,
                    None,
                ),
                "completed": ("completion", "done", None),
            }[stage_filter]
            phase_filter = phase_filter or legacy_phase
            state_filter = state_filter or legacy_state
            operation_filter = operation_filter or legacy_operation
        if (
            operation_filter is not None
            and operation_filter not in JOB_OPERATION_LABELS
        ):
            raise ValueError("지원하지 않는 작업 범위 필터입니다.")
        if (
            phase_filter is not None
            and phase_filter not in JOB_PHASE_FILTERS
        ):
            raise ValueError("지원하지 않는 작업 단계 필터입니다.")
        if state_filter is not None and state_filter not in JOB_STATE_LABELS:
            raise ValueError("지원하지 않는 작업 상태 필터입니다.")
        if reason_filter is not None and reason_filter not in JOB_REASON_LABELS:
            raise ValueError("지원하지 않는 중단 사유 필터입니다.")
        return (
            operation_filter,
            phase_filter,
            state_filter,
            reason_filter,
        )

    def job_filter_spec(
        *,
        operation_filter: str | None,
        phase_filter: str | None,
        state_filter: str | None,
        reason_filter: str | None,
    ) -> dict[str, Any]:
        spec: dict[str, Any] = {}
        if operation_filter is not None:
            spec["operations"] = {operation_filter}
        if phase_filter is not None:
            spec.update(JOB_PHASE_FILTERS[phase_filter])
        if state_filter is not None:
            spec["states"] = {state_filter}
        if reason_filter is not None:
            spec["reason_codes"] = {reason_filter}
        return spec

    def job_filter_url(
        *,
        operation_filter: str | None,
        phase_filter: str | None,
        state_filter: str | None,
        reason_filter: str | None,
        jobs_page: int | None = None,
        **values: object,
    ) -> str:
        query = {
            key: value
            for key, value in {
                "operation": operation_filter,
                "phase": phase_filter,
                "state": state_filter,
                "reason_code": reason_filter,
                "jobs_page": jobs_page if jobs_page and jobs_page > 1 else None,
                **values,
            }.items()
            if value not in (None, "")
        }
        return f"/jobs?{urlencode(query)}" if query else "/jobs"

    def job_stage_filter_context(
        service: SubtitleOrchestrator,
        *,
        operation_filter: str | None,
        phase_filter: str | None,
        state_filter: str | None,
        reason_filter: str | None,
    ) -> dict[str, Any]:
        (
            operation_filter,
            phase_filter,
            state_filter,
            reason_filter,
        ) = normalize_job_list_filters(
            operation_filter=operation_filter,
            phase_filter=phase_filter,
            state_filter=state_filter,
            reason_filter=reason_filter,
        )

        selected = {
            "operation": operation_filter,
            "phase": phase_filter,
            "state": state_filter,
            "reason_code": reason_filter,
        }

        def options_for(
            dimension: str,
            options: Sequence[Mapping[str, str]],
        ) -> list[dict[str, Any]]:
            views = []
            for option in options:
                values = dict(selected)
                values[dimension] = str(option["key"])
                spec = job_filter_spec(
                    operation_filter=values["operation"],
                    phase_filter=values["phase"],
                    state_filter=values["state"],
                    reason_filter=values["reason_code"],
                )
                views.append(
                    {
                        **dict(option),
                        "count": service.store.count_jobs(
                            **spec,
                            include_comparison_transcriptions=False,
                        ),
                        "selected": selected[dimension] == option["key"],
                        "url": job_filter_url(
                            operation_filter=values["operation"],
                            phase_filter=values["phase"],
                            state_filter=values["state"],
                            reason_filter=values["reason_code"],
                        ),
                    }
                )
            return views

        reason_options = options_for(
            "reason_code",
            tuple(
                {"key": key, "label": label}
                for key, label in JOB_REASON_LABELS.items()
            ),
        )
        reason_options = [
            option
            for option in reason_options
            if option["count"] or option["selected"]
        ]

        def clear_dimension_url(dimension: str) -> str:
            values = dict(selected)
            values[dimension] = None
            return job_filter_url(
                operation_filter=values["operation"],
                phase_filter=values["phase"],
                state_filter=values["state"],
                reason_filter=values["reason_code"],
            )

        groups = [
            {
                "key": "operation",
                "label": "요청 범위",
                "selected": operation_filter is not None,
                "clear_url": clear_dimension_url("operation"),
                "options": options_for(
                    "operation",
                    JOB_OPERATION_FILTER_NAV,
                ),
            },
            {
                "key": "phase",
                "label": "현재 단계",
                "selected": phase_filter is not None,
                "clear_url": clear_dimension_url("phase"),
                "options": options_for("phase", JOB_PHASE_FILTER_NAV),
            },
            {
                "key": "state",
                "label": "작업 상태",
                "selected": state_filter is not None,
                "clear_url": clear_dimension_url("state"),
                "options": options_for("state", JOB_STATE_FILTER_NAV),
            },
        ]
        if reason_options:
            groups.append(
                {
                    "key": "reason_code",
                    "label": "중단·실패 사유",
                    "selected": reason_filter is not None,
                    "clear_url": clear_dimension_url("reason_code"),
                    "options": reason_options,
                }
            )
        return {
            "job_filter_groups": groups,
            "job_filters_clear_url": "/jobs",
            "job_filters_active": any(selected.values()),
            "selected_operation": operation_filter,
            "selected_phase": phase_filter,
            "selected_state": state_filter,
            "selected_reason_code": reason_filter,
        }

    def job_list_context(
        service: SubtitleOrchestrator,
        *,
        jobs_page: int,
        operation_filter: str | None = None,
        phase_filter: str | None = None,
        state_filter: str | None = None,
        reason_filter: str | None = None,
        folder: str = "",
        limit: int = RECENT_JOB_LIMIT,
        paginated: bool = True,
    ) -> dict[str, Any]:
        (
            operation_filter,
            phase_filter,
            state_filter,
            reason_filter,
        ) = normalize_job_list_filters(
            operation_filter=operation_filter,
            phase_filter=phase_filter,
            state_filter=state_filter,
            reason_filter=reason_filter,
        )
        filter_spec = job_filter_spec(
            operation_filter=operation_filter,
            phase_filter=phase_filter,
            state_filter=state_filter,
            reason_filter=reason_filter,
        )
        jobs_page = max(1, jobs_page) if paginated else 1
        job_count = service.store.count_jobs(
            **filter_spec,
            include_comparison_transcriptions=False,
        )
        jobs_offset = (jobs_page - 1) * limit
        all_visible_jobs = service.store.list_jobs(
            limit=None,
            include_comparison_transcriptions=False,
        )
        matching_jobs = service.store.list_jobs(
            limit=None,
            **filter_spec,
            include_comparison_transcriptions=False,
        )
        open_jobs = [
            job
            for job in all_visible_jobs
            if job.state != JobState.DONE
        ]
        recent_jobs = service.store.list_jobs(
            limit=limit,
            offset=jobs_offset,
            **filter_spec,
            include_comparison_transcriptions=False,
        )
        latest_jobs = service.store.latest_jobs_by_source()
        translatable_job_ids = {
            job.id
            for job in recent_jobs
            if job.can_start_translation
            and not job.options.get("comparison_id")
            and latest_jobs.get(job.source_rel) is not None
            and latest_jobs[job.source_rel].id == job.id
        }
        stoppable_job_ids = (
            {
                job.id
                for job in matching_jobs
                if job.can_stop
            }
            if paginated
            else set()
        )
        retriable_job_ids = (
            {
                job.id
                for job in matching_jobs
                if job.can_retry
            }
            if paginated
            else set()
        )

        def page_location(page: int) -> str:
            if folder:
                return media_location(folder, jobs_page=page)
            return job_filter_url(
                operation_filter=operation_filter,
                phase_filter=phase_filter,
                state_filter=state_filter,
                reason_filter=reason_filter,
                jobs_page=page,
            )

        retry_groups: dict[str, list[str]] = {}
        if phase_filter is None and state_filter in {
            "blocked",
            "stopped",
            "failed",
        }:
            for job in matching_jobs:
                if not job.can_retry:
                    continue
                phase_key = (
                    str(job.phase)
                    if str(job.phase) in JOB_PHASE_FILTER_LABELS
                    else "completion"
                )
                retry_groups.setdefault(phase_key, []).append(job.id)
        retriable_phase_groups = [
            {
                "key": key,
                "label": JOB_PHASE_FILTER_LABELS[key],
                "job_ids": retry_groups[key],
                "count": len(retry_groups[key]),
            }
            for key in JOB_PHASE_FILTER_LABELS
            if retry_groups.get(key)
        ]

        label = " · ".join(
            part
            for part in (
                (
                    JOB_OPERATION_FILTER_LABELS[operation_filter]
                    if operation_filter is not None
                    else ""
                ),
                (
                    JOB_PHASE_FILTER_LABELS[phase_filter]
                    if phase_filter is not None
                    else ""
                ),
                (
                    JOB_STATE_LABELS[state_filter]
                    if state_filter is not None
                    else ""
                ),
                (
                    JOB_REASON_LABELS[reason_filter]
                    if reason_filter is not None
                    else ""
                ),
            )
            if part
        ) or "전체"
        filtered = any(
            value is not None
            for value in (
                operation_filter,
                phase_filter,
                state_filter,
                reason_filter,
            )
        )
        return {
            "recent_jobs": recent_jobs,
            "translatable_job_ids": translatable_job_ids,
            "translatable_job_count": len(translatable_job_ids),
            "stoppable_job_ids": stoppable_job_ids,
            "stoppable_selection_count": len(stoppable_job_ids),
            "retriable_job_ids": retriable_job_ids,
            "retriable_selection_count": len(retriable_job_ids),
            "retriable_phase_groups": retriable_phase_groups,
            "stoppable_job_count": sum(job.can_stop for job in open_jobs),
            "retriable_job_count": sum(job.can_retry for job in open_jobs),
            "pausable_translation_count": sum(
                job.can_pause_translation for job in open_jobs
            ),
            "jobs_page": jobs_page,
            "jobs_has_previous": paginated and jobs_page > 1,
            "jobs_has_next": (
                paginated and jobs_offset + limit < job_count
            ),
            "jobs_previous_url": (
                page_location(jobs_page - 1)
                if paginated and jobs_page > 1
                else None
            ),
            "jobs_next_url": (
                page_location(jobs_page + 1)
                if paginated and jobs_offset + limit < job_count
                else None
            ),
            "job_count": job_count,
            "job_list_title": f"{label} 작업",
            "job_list_empty_message": (
                "선택한 조건에 해당하는 작업이 없습니다."
                if filtered
                else "등록된 작업이 없습니다."
            ),
            "show_bulk_actions": paginated,
            "selected_operation": operation_filter,
            "selected_phase": phase_filter,
            "selected_state": state_filter,
            "selected_reason_code": reason_filter,
            "selected_status_group": (
                "completed" if state_filter == "done" else state_filter
            ),
            "selected_stage_filter": phase_filter,
            "job_filter_query": job_filter_url(
                operation_filter=operation_filter,
                phase_filter=phase_filter,
                state_filter=state_filter,
                reason_filter=reason_filter,
            ).removeprefix("/jobs?")
            if filtered
            else "",
            "job_detail_return_query": urlencode(
                {
                    key: value
                    for key, value in {
                        "return_operation": operation_filter,
                        "return_phase": phase_filter,
                        "return_state": state_filter,
                        "return_reason_code": reason_filter,
                        "return_jobs_page": (
                            jobs_page if jobs_page > 1 else None
                        ),
                    }.items()
                    if value not in (None, "")
                }
            ),
            "current_folder": folder,
        }

    def validate_csrf(request: Request, csrf_token: str) -> None:
        if not authentication_enabled:
            return
        expected = str(request.session.get("csrf_token", ""))
        if not expected or not hmac.compare_digest(expected, csrf_token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="invalid CSRF token",
            )

    def artifact_details(
        job: Any,
        kind: str,
    ) -> tuple[str | None, str, str]:
        details = {
            "transcript": (
                job.transcript_path,
                artifact_filename(job.source_rel, "transcript"),
                "일본어 전사 JSON",
            ),
            "translation": (
                job.translation_path,
                artifact_filename(job.source_rel, "translation"),
                "한국어 결과 JSON",
            ),
        }
        try:
            return details[kind]
        except KeyError as error:
            raise HTTPException(
                status_code=404,
                detail="artifact not found",
            ) from error

    def styled_webvtt(
        job: Any,
        *,
        translation_override: Path | None = None,
    ) -> str | None:
        if not job.transcript_path or not job.translation_path:
            return None
        transcript_path = Path(job.transcript_path)
        translation_path = translation_override or Path(job.translation_path)
        if not transcript_path.is_file() or not translation_path.is_file():
            return None
        transcript_payload = json.loads(
            transcript_path.read_text(encoding="utf-8")
        )
        translation_payload = json.loads(
            translation_path.read_text(encoding="utf-8")
        )
        if not isinstance(transcript_payload, Mapping):
            raise ValueError("transcript JSON document must be an object")
        if not isinstance(translation_payload, Mapping):
            raise ValueError("translation JSON document must be an object")
        segments = validate_transcript(transcript_payload)
        translations = validate_translation_items(
            translation_payload.get("translations"),
            [str(segment["id"]) for segment in segments],
        )
        return render_webvtt(segments, translations)

    def dashboard_context(
        request: Request,
        *,
        error: str | None = None,
        notice: str | None = None,
    ) -> dict[str, Any]:
        service = orchestrator(request)
        return {
            "request": request,
            **dashboard_2d_data(
                service,
                audio_workers=configured_settings.audio_workers,
            ),
            "job_stats": job_stats(service),
            "csrf_token": request.session.get("csrf_token", ""),
            "error": error,
            "notice": notice,
            "remote_servers": service.remote_servers_view(),
            "gpu_snapshot": request.app.state.gpu_monitor.snapshot(),
            "gpu_refresh_milliseconds": int(
                configured_settings.gpu_metrics_refresh_seconds * 1000
            ),
        }

    def webgpu_dashboard_response(request: Request) -> HTMLResponse:
        service = orchestrator(request)
        snapshot = request.app.state.gpu_monitor.snapshot()
        scene_data = webgpu_scene_context(
            service,
            snapshot,
            audio_workers=configured_settings.audio_workers,
        )
        return TEMPLATES.TemplateResponse(
            request,
            "webgpu.html",
            {
                "gpu": scene_data["gpu"],
                "scene_data": scene_data,
            },
        )

    def media_context(
        request: Request,
        *,
        error: str | None = None,
        notice: str | None = None,
        folder: str = "",
        query: str = "",
        actor: str = "",
    ) -> dict[str, Any]:
        service = orchestrator(request)
        normalized_query = query.strip()
        normalized_actor = actor.strip()
        browser = (
            service.library.search_media(
                title_query=normalized_query,
                actor_query=normalized_actor,
                relative_directory=folder,
            )
            if normalized_query or normalized_actor
            else service.library.browse(folder)
        )
        browser = flatten_media_display_folders(
            service.library,
            browser,
            service.path_display_rules,
        )
        for media_folder in browser["folders"]:
            try:
                media_folder["actor_image_path"] = (
                    service.library.actor_profile_for_directory(
                        str(media_folder["path"])
                    )
                )
            except ValueError:
                media_folder["actor_image_path"] = None
        latest_jobs = service.store.latest_jobs_by_source()
        completed_subtitles = service.store.latest_completed_subtitle_jobs()
        for media in browser["files"]:
            source_rel = str(media["path"])
            latest = latest_jobs.get(source_rel)
            linked_job = None
            if latest is not None and latest.state in {
                JobState.PAUSED,
                JobState.BLOCKED,
                JobState.STOPPED,
                JobState.FAILED,
            }:
                media["subtitle_state"] = latest.state
                stage = JOB_STAGE_LABELS.get(
                    str(latest.blocked_stage),
                    str(latest.blocked_stage or ""),
                )
                media["processing_label"] = JOB_STATE_LABELS[latest.state]
                if stage:
                    media["processing_label"] += f" · {stage}"
                linked_job = latest
            elif latest is not None and latest.state != JobState.DONE:
                media["subtitle_state"] = "running"
                media["processing_label"] = MEDIA_PROCESSING_LABELS.get(
                    latest.status,
                    JOB_STATUS_LABELS.get(latest.status, latest.status),
                )
                linked_job = latest
            elif media["has_subtitle"]:
                media["subtitle_state"] = "completed"
                media["processing_label"] = (
                    "자막 생성 완료"
                    if latest is not None and latest.status == "completed"
                    else "한국어 자막 있음"
                )
                linked_job = completed_subtitles.get(source_rel) or latest
            elif latest is not None:
                media["subtitle_state"] = (
                    "completed" if latest.status == "completed" else "progress"
                )
                media["processing_label"] = MEDIA_PROCESSING_LABELS.get(
                    latest.status,
                    JOB_STATUS_LABELS.get(latest.status, latest.status),
                )
                linked_job = latest
            else:
                media["subtitle_state"] = "pending"
                media["processing_label"] = "미처리"
            media["processing_status"] = (
                latest.status
                if latest is not None
                else ("subtitle_present" if media["has_subtitle"] else "pending")
            )
            media["job_id"] = linked_job.id if linked_job else None
            # 진행 중이 아니면 개별 선택은 열어 둔다. 완료된 항목을 다시
            # 번역하려면 직접 골라야 하기 때문이다.
            media["selectable"] = (
                latest is None or latest.state == JobState.DONE
            )
            # '전체 선택'은 아직 자막이 없는 항목만 담는다.
            media["auto_selectable"] = not media["has_subtitle"] and (
                latest is None
                or latest.status
                in {"audio_completed", "transcription_completed"}
            )

        browser["files"] = group_multipart_media(browser["files"])
        for media in browser["files"]:
            if not media.get("multipart"):
                continue
            parts = list(media["parts"])
            states = {str(part["subtitle_state"]) for part in parts}
            labels = {str(part["processing_label"]) for part in parts}
            processing_statuses = {
                str(part["processing_status"]) for part in parts
            }
            if len(states) == 1 and len(labels) == 1:
                media["subtitle_state"] = next(iter(states))
                media["processing_label"] = (
                    f"{next(iter(labels))} · {len(parts)}파트"
                )
            elif "failed" in states:
                media["subtitle_state"] = "failed"
                failed_parts = sum(
                    state == "failed"
                    for state in (
                        str(part["subtitle_state"]) for part in parts
                    )
                )
                blocked_parts = sum(
                    state == "blocked"
                    for state in (
                        str(part["subtitle_state"]) for part in parts
                    )
                )
                media["processing_label"] = (
                    f"실패 {failed_parts}파트"
                    + (
                        f" · 중단 {blocked_parts}파트"
                        if blocked_parts
                        else ""
                    )
                )
            elif "blocked" in states:
                media["subtitle_state"] = "blocked"
                media["processing_label"] = "일부 파트 중단"
            elif "running" in states:
                media["subtitle_state"] = "running"
                media["processing_label"] = "일부 파트 처리 중"
            else:
                media["subtitle_state"] = "progress"
                media["processing_label"] = "파트별 처리 상태 다름"
            media["processing_status"] = (
                next(iter(processing_statuses))
                if len(processing_statuses) == 1
                else "multipart_mixed"
            )
            media["job_id"] = None
            media["selectable"] = any(
                bool(part["selectable"]) for part in parts
            )
            media["auto_selectable"] = any(
                bool(part["auto_selectable"]) for part in parts
            )

        return {
            "request": request,
            "csrf_token": request.session.get("csrf_token", ""),
            "error": error,
            "notice": notice,
            "remote_servers": service.remote_servers_view(),
            "prompt_categories": service.prompt_revision_choices(),
            "search_query": normalized_query,
            "actor_filter": normalized_actor,
            **browser,
        }

    def transcription_comparison_jobs(
        service: SubtitleOrchestrator,
        comparison_id: str,
    ) -> list[Any]:
        comparison_jobs = [
            job
            for job in service.store.list_jobs(limit=None)
            if str(job.options.get("comparison_id", "")) == comparison_id
        ]
        if not comparison_jobs:
            raise HTTPException(
                status_code=404,
                detail="transcription comparison not found",
            )
        return comparison_jobs

    def transcription_comparison_chunk_lengths(
        comparison_jobs: Sequence[Any],
    ) -> dict[str, int | float]:
        chunk_lengths = {
            "kotoba_chunk_length_seconds": 15,
            "whisperx_chunk_length_seconds": 30,
            "anime_max_group_duration_seconds": (
                DEFAULT_ANIME_MAX_GROUP_SECONDS
            ),
            "qwen_max_group_duration_seconds": (
                DEFAULT_QWEN_MAX_GROUP_SECONDS
            ),
        }
        for job in comparison_jobs:
            if str(job.options.get("backend", "")) == "hybrid":
                rescue = job.options.get("hybrid_rescue", {})
                if isinstance(rescue, Mapping):
                    for key in chunk_lengths:
                        try:
                            chunk_lengths[key] = int(rescue[key])
                        except (KeyError, TypeError, ValueError):
                            pass
        for job in comparison_jobs:
            backend = str(job.options.get("backend", ""))
            if backend in {"kotoba", "whisperx"}:
                key = f"{backend}_chunk_length_seconds"
                try:
                    chunk_lengths[key] = int(
                        job.options["chunk_length_seconds"]
                    )
                except (KeyError, TypeError, ValueError):
                    pass
            if backend == "whisperjav":
                raw_whisperjav = job.options.get("whisperjav", {})
                if isinstance(raw_whisperjav, Mapping):
                    for key in (
                        "anime_max_group_duration_seconds",
                        "qwen_max_group_duration_seconds",
                    ):
                        try:
                            chunk_lengths[key] = float(raw_whisperjav[key])
                        except (KeyError, TypeError, ValueError):
                            pass
        return chunk_lengths

    def transcription_comparison_backends(
        comparison_jobs: Sequence[Any],
    ) -> tuple[str, ...]:
        for job in comparison_jobs:
            configured = job.options.get("comparison_backends")
            if isinstance(configured, list):
                normalized = tuple(
                    backend
                    for backend in configured
                    if backend in TRANSCRIPTION_COMPARISON_BACKENDS
                )
                if normalized:
                    return normalized
        present = {
            str(job.options.get("backend", "")) for job in comparison_jobs
        }
        return tuple(
            backend
            for backend in TRANSCRIPTION_COMPARISON_BACKENDS
            if backend in present
        )

    def transcription_comparison_rerun_options(
        comparison_jobs: Sequence[Any],
        *,
        kotoba_chunk_length_seconds: int,
        whisperx_chunk_length_seconds: int,
        anime_max_group_duration_seconds: float,
        qwen_max_group_duration_seconds: float,
    ) -> dict[str, Any]:
        template_job = next(
            (
                job
                for job in comparison_jobs
                if str(job.options.get("backend", "")) == "hybrid"
            ),
            comparison_jobs[0],
        )
        options = dict(template_job.options)
        options.pop("comparison_id", None)
        options.pop("comparison_schema_version", None)
        options.pop("comparison_backends", None)
        options.pop(COMPARISON_PARENT_ID_OPTION, None)
        options.pop(COMPARISON_AUDIO_SOURCE_JOB_ID_OPTION, None)
        options["backend"] = "hybrid"
        raw_rescue = options.get("hybrid_rescue", {})
        rescue = dict(raw_rescue) if isinstance(raw_rescue, Mapping) else {}
        rescue.update(
            {
                "kotoba_chunk_length_seconds": (
                    kotoba_chunk_length_seconds
                ),
                "whisperx_chunk_length_seconds": (
                    whisperx_chunk_length_seconds
                ),
            }
        )
        options["hybrid_rescue"] = rescue
        options["whisperjav"] = {
            "anime_max_group_duration_seconds": (
                anime_max_group_duration_seconds
            ),
            "qwen_max_group_duration_seconds": (
                qwen_max_group_duration_seconds
            ),
        }
        options["chunk_length_seconds"] = kotoba_chunk_length_seconds
        return options

    def transcription_comparison_jobs_by_id(
        service: SubtitleOrchestrator,
    ) -> dict[str, list[Any]]:
        jobs_by_comparison: dict[str, list[Any]] = {}
        for job in service.store.list_jobs(limit=None):
            candidate_id = str(job.options.get("comparison_id", "")).strip()
            if candidate_id:
                jobs_by_comparison.setdefault(candidate_id, []).append(job)
        return jobs_by_comparison

    def transcription_comparison_source_key(
        comparison_jobs: Sequence[Any],
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                {job.source_rel for job in comparison_jobs},
                key=str.casefold,
            )
        )

    def transcription_comparison_run_summary(
        comparison_id: str,
        comparison_jobs: Sequence[Any],
    ) -> dict[str, Any]:
        source_rels = transcription_comparison_source_key(comparison_jobs)
        completed_count = sum(
            job.state == JobState.DONE for job in comparison_jobs
        )
        attention_count = sum(
            job.state
            in {JobState.BLOCKED, JobState.STOPPED, JobState.FAILED}
            for job in comparison_jobs
        )
        blocked_count = sum(
            job.state == JobState.BLOCKED for job in comparison_jobs
        )
        failed_count = sum(
            job.state == JobState.FAILED for job in comparison_jobs
        )
        active_count = sum(
            job.state == JobState.RUNNING for job in comparison_jobs
        )
        terminal_count = sum(
            job.state
            in {
                JobState.DONE,
                JobState.BLOCKED,
                JobState.STOPPED,
                JobState.FAILED,
            }
            for job in comparison_jobs
        )
        waiting_count = max(
            0,
            len(comparison_jobs) - terminal_count - active_count,
        )
        if failed_count:
            status_group = "attention"
            status_label = "실패"
            status_value = "failed"
        elif blocked_count:
            status_group = "attention"
            status_label = "중단"
            status_value = "blocked"
        elif active_count:
            status_group = "running"
            status_label = "진행 중"
            status_value = "transcription_running"
        elif terminal_count == len(comparison_jobs):
            status_group = "completed"
            status_label = "완료"
            status_value = "transcription_completed"
        else:
            status_group = "waiting"
            status_label = "대기 중"
            status_value = "queued"
        return {
            "id": comparison_id,
            "source_rels": list(source_rels),
            "source_names": [
                Path(source_rel).name for source_rel in source_rels
            ],
            "source_count": len(source_rels),
            "job_count": len(comparison_jobs),
            "completed_count": completed_count,
            "attention_count": attention_count,
            "blocked_count": blocked_count,
            "failed_count": failed_count,
            "active_count": active_count,
            "waiting_count": waiting_count,
            "terminal_count": terminal_count,
            "progress_percent": round(
                terminal_count * 100 / len(comparison_jobs)
            ),
            "status_group": status_group,
            "status_label": status_label,
            "status_value": status_value,
            "created_at": min(job.created_at for job in comparison_jobs),
            "updated_at": max(job.updated_at for job in comparison_jobs),
            "chunks": transcription_comparison_chunk_lengths(comparison_jobs),
        }

    def transcription_comparison_records(
        service: SubtitleOrchestrator,
        comparison_id: str,
        comparison_jobs: Sequence[Any],
    ) -> list[dict[str, Any]]:
        source_key = transcription_comparison_source_key(comparison_jobs)
        records = [
            transcription_comparison_run_summary(candidate_id, jobs)
            for candidate_id, jobs in transcription_comparison_jobs_by_id(
                service
            ).items()
            if transcription_comparison_source_key(jobs) == source_key
        ]
        for record in records:
            record["current"] = record["id"] == comparison_id
        records.sort(
            key=lambda record: (record["created_at"], record["id"]),
            reverse=True,
        )
        return records

    def transcription_comparison_context(
        request: Request,
        comparison_id: str,
        *,
        skipped: int = 0,
        notice: str | None = None,
        error: str | None = None,
        chunk_values: Mapping[str, object] | None = None,
        selected_translation_job_ids: Sequence[str] = (),
        translation_prompt_category_id: str = "",
    ) -> dict[str, Any]:
        service = orchestrator(request)
        comparison_jobs = transcription_comparison_jobs(
            service,
            comparison_id,
        )
        selected_translation_ids = set(selected_translation_job_ids)
        chunk_lengths: dict[str, object] = {
            **transcription_comparison_chunk_lengths(comparison_jobs),
            **(dict(chunk_values) if chunk_values is not None else {}),
        }
        comparison_backends = transcription_comparison_backends(
            comparison_jobs
        )

        jobs_by_source: dict[str, dict[str, Any]] = {}
        for job in comparison_jobs:
            backend = str(job.options.get("backend", ""))
            if backend in comparison_backends:
                jobs_by_source.setdefault(job.source_rel, {})[backend] = job

        sources: list[dict[str, Any]] = []
        for source_rel in sorted(jobs_by_source, key=str.casefold):
            jobs_by_backend = jobs_by_source[source_rel]
            engines: list[dict[str, Any]] = []
            for backend in comparison_backends:
                job = jobs_by_backend.get(backend)
                transcription_stage = None
                if job is not None:
                    transcription_stage = next(
                        (
                            stage
                            for stage in job_stage_view(job)
                            if stage["key"] == "transcription"
                        ),
                        None,
                    )
                segments: list[dict[str, Any]] = []
                transcript_error: str | None = None
                if job is not None and job.transcript_path:
                    try:
                        payload = json.loads(
                            Path(job.transcript_path).read_text(encoding="utf-8")
                        )
                        if not isinstance(payload, Mapping):
                            raise ValueError(
                                "transcript JSON document must be an object"
                            )
                        segments = validate_transcript(payload)
                    except (
                        OSError,
                        UnicodeError,
                        ValueError,
                        json.JSONDecodeError,
                    ):
                        transcript_error = "전사 결과를 읽을 수 없습니다."
                engines.append(
                    {
                        "backend": backend,
                        "label": STT_BACKEND_LABELS[backend],
                        "job": job,
                        "transcription_stage": transcription_stage,
                        "segments": segments,
                        "segment_count": len(segments),
                        "character_count": sum(
                            len(str(segment["text"])) for segment in segments
                        ),
                        "duration_seconds": (
                            max(float(segment["end"]) for segment in segments)
                            if segments
                            else None
                        ),
                        "transcript_error": transcript_error,
                        "translatable": bool(
                            job is not None
                            and job.can_start_translation
                            and transcript_error is None
                        ),
                        "translation_selected": bool(
                            job is not None
                            and job.id in selected_translation_ids
                        ),
                    }
                )
            translatable_engines = [
                engine for engine in engines if engine["translatable"]
            ]
            sources.append(
                {
                    "source_rel": source_rel,
                    "audio_stage": comparison_audio_stage(
                        list(jobs_by_backend.values())
                    ),
                    "engines": engines,
                    "translatable_engines": translatable_engines,
                }
            )

        terminal_statuses = SUCCESS_STATUSES | RETRYABLE_STATUSES
        return {
            "request": request,
            "comparison_id": comparison_id,
            "sources": sources,
            "jobs": comparison_jobs,
            "csrf_token": request.session.get("csrf_token", ""),
            "retriable_count": sum(
                job.status in RETRYABLE_STATUSES for job in comparison_jobs
            ),
            "comparison_chunks": chunk_lengths,
            "comparison_backends": comparison_backends,
            "comparison_backend_labels": ", ".join(
                STT_BACKEND_LABELS[backend]
                for backend in comparison_backends
            ),
            "prompt_categories": service.prompt_revision_choices(),
            "translation_prompt_category_id": (
                translation_prompt_category_id
            ),
            "translatable_source_count": sum(
                bool(source["translatable_engines"]) for source in sources
            ),
            "completed_count": sum(
                job.status == "transcription_completed"
                for job in comparison_jobs
            ),
            "all_terminal": all(
                job.status in terminal_statuses for job in comparison_jobs
            ),
            "skipped": skipped,
            "notice": notice,
            "error": error,
            "comparison_records": transcription_comparison_records(
                service, comparison_id, comparison_jobs
            ),
        }

    def transcription_comparison_history_context(
        service: SubtitleOrchestrator,
        *,
        comparisons_page: int,
    ) -> dict[str, Any]:
        comparisons = [
            transcription_comparison_run_summary(comparison_id, jobs)
            for comparison_id, jobs in transcription_comparison_jobs_by_id(
                service
            ).items()
        ]
        grouped_comparisons: dict[tuple[str, ...], list[dict[str, Any]]] = {}
        for comparison in comparisons:
            source_key = tuple(comparison["source_rels"])
            grouped_comparisons.setdefault(source_key, []).append(comparison)

        comparison_groups: list[dict[str, Any]] = []
        for source_rels, records in grouped_comparisons.items():
            records.sort(
                key=lambda record: (record["created_at"], record["id"]),
                reverse=True,
            )
            comparison_groups.append(
                {
                    "source_rels": list(source_rels),
                    "source_names": [
                        Path(source_rel).name for source_rel in source_rels
                    ],
                    "source_count": len(source_rels),
                    "record_count": len(records),
                    "records": records,
                    "created_at": min(
                        record["created_at"] for record in records
                    ),
                    "updated_at": max(
                        record["updated_at"] for record in records
                    ),
                }
            )
        comparison_groups.sort(
            key=lambda group: (
                group["updated_at"],
                group["source_rels"],
            ),
            reverse=True,
        )

        comparisons_page = max(1, comparisons_page)
        comparison_count = len(comparisons)
        comparison_group_count = len(comparison_groups)
        offset = (comparisons_page - 1) * COMPARISON_HISTORY_LIMIT
        return {
            "comparison_groups": comparison_groups[
                offset : offset + COMPARISON_HISTORY_LIMIT
            ],
            "comparison_count": comparison_count,
            "comparison_group_count": comparison_group_count,
            "comparisons_page": comparisons_page,
            "comparisons_has_previous": comparisons_page > 1,
            "comparisons_has_next": (
                offset + COMPARISON_HISTORY_LIMIT < comparison_group_count
            ),
        }

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        service = orchestrator(request)
        return {
            "status": "ok",
            "media_root_available": service.library.root.is_dir(),
            "authentication_enabled": authentication_enabled,
            "remote_servers_configured": service.remote_servers_configured,
        }

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Any:
        if not authentication_enabled:
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        if is_authenticated(request):
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        return TEMPLATES.TemplateResponse(
            request,
            "login.html",
            {"error": None},
        )

    @app.post("/login", response_class=HTMLResponse)
    def login(request: Request, password: str = Form(...)) -> Any:
        if not authentication_enabled:
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        if not hmac.compare_digest(
            password,
            configured_settings.admin_password,
        ):
            return TEMPLATES.TemplateResponse(
                request,
                "login.html",
                {"error": "비밀번호가 올바르지 않습니다."},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        request.session.clear()
        request.session["authenticated"] = True
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form("")) -> Any:
        if not authentication_enabled:
            request.session.clear()
            return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        request.session.clear()
        return login_redirect()

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        queued: int | None = None,
        translation_pause_requested: int | None = None,
        translations_paused: int | None = None,
        jobs_stopped: int | None = None,
        jobs_retried: int | None = None,
        translations_queued: int | None = None,
        skipped: int | None = None,
        folder: str = "",
        view: str | None = None,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        if view is not None:
            if view not in {"2d", "3d"}:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid dashboard view",
                )
            response = RedirectResponse(
                "/",
                status_code=status.HTTP_303_SEE_OTHER,
            )
            response.set_cookie(
                DASHBOARD_MODE_COOKIE,
                view,
                max_age=365 * 24 * 60 * 60,
                httponly=True,
                secure=configured_settings.secure_cookie,
                samesite="strict",
            )
            return response
        if folder:
            return RedirectResponse(
                media_location(folder),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        if request.cookies.get(DASHBOARD_MODE_COOKIE) == "3d":
            return webgpu_dashboard_response(request)
        notice = None
        if queued is not None and queued > 0:
            notice = f"작업 {queued}개를 등록했습니다."
            if skipped:
                notice += f" 기존 작업·자막 {skipped}개는 제외했습니다."
        elif translation_pause_requested is not None:
            notice = "번역 일시 정지 요청을 반영했습니다."
        elif translations_paused is not None:
            notice = (
                f"번역 작업 {translations_paused}개에 일시 정지를 요청했습니다."
            )
        elif jobs_stopped is not None:
            notice = f"진행 중인 작업 {jobs_stopped}개에 정지를 요청했습니다."
        elif jobs_retried is not None:
            notice = f"작업 {jobs_retried}개를 재시도했습니다."
        elif translations_queued is not None:
            notice = (
                f"선택한 전사 작업 {translations_queued}개를 번역으로 "
                "전환했습니다."
            )
        try:
            context = dashboard_context(request, notice=notice)
            response_status = status.HTTP_200_OK
        except ValueError as error:
            context = dashboard_context(request, error=str(error))
            response_status = status.HTTP_400_BAD_REQUEST
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            context,
            status_code=response_status,
        )

    @app.get("/media", response_class=HTMLResponse)
    def media_page(
        request: Request,
        queued: int | None = None,
        skipped: int | None = None,
        translation_pause_requested: int | None = None,
        translations_paused: int | None = None,
        jobs_stopped: int | None = None,
        jobs_retried: int | None = None,
        translations_queued: int | None = None,
        folder: str = "",
        q: str = "",
        actor: str = "",
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        notice = None
        if queued is not None and queued > 0:
            notice = f"작업 {queued}개를 등록했습니다."
            if skipped:
                notice += f" 기존 작업·자막 {skipped}개는 제외했습니다."
        elif translation_pause_requested is not None:
            notice = "번역 일시 정지 요청을 반영했습니다."
        elif translations_paused is not None:
            notice = (
                f"번역 작업 {translations_paused}개에 일시 정지를 요청했습니다."
            )
        elif jobs_stopped is not None:
            notice = f"진행 중인 작업 {jobs_stopped}개에 정지를 요청했습니다."
        elif jobs_retried is not None:
            notice = f"작업 {jobs_retried}개를 재시도했습니다."
        elif translations_queued is not None:
            notice = (
                f"선택한 전사 작업 {translations_queued}개를 번역으로 "
                "전환했습니다."
            )
        try:
            context = media_context(
                request,
                notice=notice,
                folder=folder,
                query=q,
                actor=actor,
            )
            response_status = status.HTTP_200_OK
        except ValueError as error:
            context = media_context(request, error=str(error))
            response_status = status.HTTP_400_BAD_REQUEST
        return TEMPLATES.TemplateResponse(
            request,
            "media.html",
            context,
            status_code=response_status,
        )

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(
        request: Request,
        operation: str = "",
        phase: str = "",
        state: str = "",
        reason_code: str = "",
        status_group: str = "",
        stage_filter: str = "",
        jobs_page: int = 1,
        translations_queued: int | None = None,
        translations_paused: int | None = None,
        jobs_stopped: int | None = None,
        jobs_retried: int | None = None,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        try:
            filters = normalize_job_list_filters(
                operation_filter=operation or None,
                phase_filter=phase or None,
                state_filter=state or None,
                reason_filter=reason_code or None,
                status_group=status_group or None,
                stage_filter=stage_filter or None,
            )
            context = {
                "request": request,
                **job_list_context(
                    orchestrator(request),
                    jobs_page=jobs_page,
                    operation_filter=filters[0],
                    phase_filter=filters[1],
                    state_filter=filters[2],
                    reason_filter=filters[3],
                ),
                **job_stage_filter_context(
                    orchestrator(request),
                    operation_filter=filters[0],
                    phase_filter=filters[1],
                    state_filter=filters[2],
                    reason_filter=filters[3],
                ),
                "status_groups": JOB_STATUS_GROUP_LABELS,
                "csrf_token": request.session.get("csrf_token", ""),
                "prompt_categories": orchestrator(
                    request
                ).prompt_revision_choices(),
                "notice": (
                    f"선택한 전사 작업 {translations_queued}개를 번역으로 "
                    "전환했습니다."
                    if translations_queued is not None
                    else (
                        f"번역 작업 {translations_paused}개에 일시 정지를 "
                        "요청했습니다."
                        if translations_paused is not None
                        else (
                            f"진행 중인 작업 {jobs_stopped}개에 정지를 "
                            "요청했습니다."
                            if jobs_stopped is not None
                            else (
                                f"작업 {jobs_retried}개를 "
                                "재시도했습니다."
                                if jobs_retried is not None
                                else None
                            )
                        )
                    )
                ),
                "error": None,
            }
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return TEMPLATES.TemplateResponse(request, "jobs.html", context)

    @app.get("/job-stage-filters-fragment", response_class=HTMLResponse)
    def job_stage_filters_fragment(
        request: Request,
        operation: str = "",
        phase: str = "",
        state: str = "",
        reason_code: str = "",
        status_group: str = "",
        stage_filter: str = "",
    ) -> Any:
        if not is_authenticated(request):
            raise HTTPException(
                status_code=401,
                detail="authentication required",
            )
        try:
            filters = normalize_job_list_filters(
                operation_filter=operation or None,
                phase_filter=phase or None,
                state_filter=state or None,
                reason_filter=reason_code or None,
                status_group=status_group or None,
                stage_filter=stage_filter or None,
            )
            context = job_stage_filter_context(
                orchestrator(request),
                operation_filter=filters[0],
                phase_filter=filters[1],
                state_filter=filters[2],
                reason_filter=filters[3],
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return TEMPLATES.TemplateResponse(
            request,
            "_job_stage_filters.html",
            {"request": request, **context},
        )

    @app.get("/comparisons", response_class=HTMLResponse)
    def transcription_comparison_history_page(
        request: Request,
        comparisons_page: int = 1,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        return TEMPLATES.TemplateResponse(
            request,
            "comparisons.html",
            {
                "request": request,
                **transcription_comparison_history_context(
                    orchestrator(request),
                    comparisons_page=comparisons_page,
                ),
            },
        )

    def settings_context(
        request: Request,
        *,
        error: str | None = None,
        notice: str | None = None,
        values: Mapping[str, Any] | None = None,
        include_artifact_audit: bool = False,
        artifact_minimum_age_days: int = DEFAULT_ARTIFACT_CLEANUP_AGE_DAYS,
    ) -> dict[str, Any]:
        service = orchestrator(request)
        server_values = service.remote_servers_view()
        if values is not None:
            server_values.update(values)
        return {
            "request": request,
            "csrf_token": request.session.get("csrf_token", ""),
            "error": error,
            "notice": notice,
            "remote_servers": server_values,
            "subtitle_validator": service.subtitle_validator_view(),
            "prompt_categories": service.all_prompt_categories(),
            "prompt_revisions_by_category": {
                category.id: list(
                    reversed(
                        service.store.list_prompt_revisions(category.id)
                    )
                )
                for category in service.all_prompt_categories()
            },
            "artifact_audit": (
                service.artifact_audit(
                    minimum_age_days=artifact_minimum_age_days,
                )
                if include_artifact_audit
                else None
            ),
        }

    @app.get("/settings", response_class=HTMLResponse)
    def server_settings_page(
        request: Request,
        saved: bool = False,
        prompt_saved: bool = False,
        path_saved: bool = False,
        stt_started: bool = False,
        validator_saved: bool = False,
        stt_resumed: int = 0,
        artifact_audit: bool = False,
        artifact_cleanup_run: bool = False,
        artifact_cleaned: int = 0,
        artifact_reclaimed_bytes: int = 0,
        artifact_cleanup_failed: int = 0,
        artifact_minimum_age_days: int = DEFAULT_ARTIFACT_CLEANUP_AGE_DAYS,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        artifact_error = None
        if artifact_audit and not 1 <= artifact_minimum_age_days <= 3650:
            artifact_error = "정리 보류기간은 1~3650일이어야 합니다."
            artifact_audit = False
        artifact_cleanup_notice = None
        if artifact_cleanup_run:
            artifact_cleanup_notice = (
                f"미참조 산출물 {artifact_cleaned}개를 정리했습니다"
                f" ({format_storage_size(artifact_reclaimed_bytes)})."
            )
            if artifact_cleanup_failed:
                artifact_cleanup_notice += (
                    f" 정리하지 못한 파일은 {artifact_cleanup_failed}개입니다."
                )
        page_notice = artifact_cleanup_notice
        if stt_started:
            page_notice = "전사 서버 연결을 확인했습니다."
            if stt_resumed:
                page_notice += f" 중단 작업 {stt_resumed}건을 재개했습니다."
        elif saved:
            page_notice = "서버 설정을 저장했습니다."
        elif validator_saved:
            page_notice = "상용 LLM 검증 설정을 저장했습니다."
        elif path_saved:
            page_notice = "경로 표시 규칙을 저장했습니다."
        elif prompt_saved:
            page_notice = "번역 프롬프트 설정을 저장했습니다."
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            settings_context(
                request,
                error=artifact_error,
                notice=page_notice,
                include_artifact_audit=artifact_audit,
                artifact_minimum_age_days=artifact_minimum_age_days,
            ),
        )

    @app.post("/settings/artifacts/cleanup", response_class=HTMLResponse)
    def cleanup_artifact_storage(
        request: Request,
        csrf_token: str = Form(""),
        minimum_age_days: int = Form(DEFAULT_ARTIFACT_CLEANUP_AGE_DAYS),
        cleanup_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        if not 1 <= minimum_age_days <= 3650:
            return TEMPLATES.TemplateResponse(
                request,
                "settings.html",
                settings_context(
                    request,
                    error="정리 보류기간은 1~3650일이어야 합니다.",
                    include_artifact_audit=True,
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        try:
            cleanup = orchestrator(request).cleanup_artifacts(
                minimum_age_days=minimum_age_days,
                expected_token=cleanup_token,
            )
        except ValueError as error:
            return TEMPLATES.TemplateResponse(
                request,
                "settings.html",
                settings_context(
                    request,
                    error=str(error),
                    include_artifact_audit=True,
                    artifact_minimum_age_days=minimum_age_days,
                ),
                status_code=status.HTTP_409_CONFLICT,
            )
        query = urlencode(
            {
                "artifact_audit": "true",
                "artifact_cleanup_run": "true",
                "artifact_cleaned": cleanup["removed_files"],
                "artifact_reclaimed_bytes": cleanup["removed_bytes"],
                "artifact_cleanup_failed": len(cleanup["failed_files"]),
                "artifact_minimum_age_days": minimum_age_days,
            }
        )
        return RedirectResponse(
            f"/settings?{query}#artifact-retention",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/settings/transcription/start", response_class=HTMLResponse)
    def start_transcription_stt(
        request: Request,
        csrf_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            resumed = orchestrator(request).activate_transcription_stt()
        except (ValueError, ExternalServiceError) as error:
            return TEMPLATES.TemplateResponse(
                request,
                "settings.html",
                settings_context(request, error=str(error)),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return RedirectResponse(
            f"/settings?stt_started=true&stt_resumed={resumed}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/settings/translation-models")
    def translation_models(
        request: Request,
        csrf_token: str = Form(""),
        lm_base_url: str = Form(...),
        lm_token: str = Form(""),
        clear_lm_token: bool = Form(False),
    ) -> dict[str, list[str]]:
        if not is_authenticated(request):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="로그인이 필요합니다.",
            )
        validate_csrf(request, csrf_token)
        current = orchestrator(request).remote_servers
        token = (
            ""
            if clear_lm_token
            else lm_token if lm_token else current.lm_token
        )
        try:
            base_url = normalize_server_url(
                lm_base_url,
                "OPENAI_COMPATIBLE_BASE_URL",
            )
            models = list_openai_compatible_models(
                base_url,
                token,
                request_observer=orchestrator(request).record_external_request,
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error
        except ExternalServiceError as error:
            LOGGER.warning(
                "translation model lookup failed: %s",
                orchestrator(request).sanitize_external_error(str(error)),
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="번역 서버에서 모델 목록을 조회할 수 없습니다.",
            ) from error
        return {"models": models}

    @app.post("/settings", response_class=HTMLResponse)
    def save_server_settings(
        request: Request,
        csrf_token: str = Form(""),
        stt_base_url: str = Form(...),
        stt_token: str = Form(""),
        clear_stt_token: bool = Form(False),
        lm_base_url: str = Form(...),
        lm_token: str = Form(""),
        clear_lm_token: bool = Form(False),
        lm_model: str = Form(...),
        translation_workers: int = Form(1),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        service = orchestrator(request)
        current = service.remote_servers
        updated = RemoteServerSettings(
            stt_base_url=stt_base_url,
            stt_token=(
                ""
                if clear_stt_token
                else stt_token if stt_token else current.stt_token
            ),
            lm_base_url=lm_base_url,
            lm_token=(
                ""
                if clear_lm_token
                else lm_token if lm_token else current.lm_token
            ),
            lm_model=lm_model,
            translation_workers=translation_workers,
        )
        try:
            service.update_remote_servers(updated)
        except ValueError as error:
            return TEMPLATES.TemplateResponse(
                request,
                "settings.html",
                settings_context(
                    request,
                    error=str(error),
                    values={
                        "stt_base_url": stt_base_url,
                        "lm_base_url": lm_base_url,
                        "lm_model": lm_model,
                        "translation_workers": translation_workers,
                    },
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return RedirectResponse(
            "/settings?saved=true",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/settings/subtitle-validator", response_class=HTMLResponse)
    def save_subtitle_validator_settings(
        request: Request,
        csrf_token: str = Form(""),
        validator_base_url: str = Form(...),
        validator_token: str = Form(""),
        clear_validator_token: bool = Form(False),
        validator_model: str = Form(...),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        service = orchestrator(request)
        current = service.subtitle_validator_view()
        stored = service.store.get_subtitle_validator_settings() or {}
        updated = SubtitleValidatorSettings(
            base_url=validator_base_url,
            token=(
                ""
                if clear_validator_token
                else validator_token
                if validator_token
                else str(stored.get("token", ""))
            ),
            model=validator_model,
        )
        try:
            service.update_subtitle_validator(updated)
        except ValueError as error:
            context = settings_context(request, error=str(error))
            context["subtitle_validator"] = {
                **current,
                "base_url": validator_base_url,
                "model": validator_model,
            }
            return TEMPLATES.TemplateResponse(
                request,
                "settings.html",
                context,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return RedirectResponse(
            "/settings?validator_saved=true",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    def render_path_display_settings_error(
        request: Request,
        error: ValueError,
    ) -> Any:
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            settings_context(request, error=str(error)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @app.post("/settings/path-display-rules", response_class=HTMLResponse)
    def create_path_display_rule(
        request: Request,
        csrf_token: str = Form(""),
        source_pattern: str = Form(...),
        display_pattern: str = Form(...),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).create_path_display_rule(
                source_pattern=source_pattern,
                display_pattern=display_pattern,
            )
        except ValueError as error:
            return render_path_display_settings_error(request, error)
        return RedirectResponse(
            "/settings?path_saved=true#path-display-rules",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/settings/path-display-rules/{rule_id}",
        response_class=HTMLResponse,
    )
    def update_path_display_rule(
        request: Request,
        rule_id: str,
        csrf_token: str = Form(""),
        source_pattern: str = Form(...),
        display_pattern: str = Form(...),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).update_path_display_rule(
                rule_id,
                source_pattern=source_pattern,
                display_pattern=display_pattern,
            )
        except ValueError as error:
            return render_path_display_settings_error(request, error)
        return RedirectResponse(
            "/settings?path_saved=true#path-display-rules",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/settings/path-display-rules/{rule_id}/delete")
    def delete_path_display_rule(
        request: Request,
        rule_id: str,
        csrf_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).delete_path_display_rule(rule_id)
        except ValueError as error:
            return render_path_display_settings_error(request, error)
        return RedirectResponse(
            "/settings?path_saved=true#path-display-rules",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    def render_prompt_settings_error(
        request: Request,
        error: ValueError,
    ) -> Any:
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            settings_context(request, error=str(error)),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    @app.post("/settings/prompt-categories", response_class=HTMLResponse)
    def create_prompt_category(
        request: Request,
        csrf_token: str = Form(""),
        name: str = Form(...),
        translation_prompt: str = Form(...),
        review_prompt: str = Form(...),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).store.create_prompt_category(
                name=name,
                translation_prompt=translation_prompt,
                review_prompt=review_prompt,
            )
        except ValueError as error:
            return render_prompt_settings_error(request, error)
        return RedirectResponse(
            "/settings?prompt_saved=true#prompt-categories",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/settings/prompt-categories/{category_id}",
        response_class=HTMLResponse,
    )
    def update_prompt_category(
        request: Request,
        category_id: str,
        csrf_token: str = Form(""),
        name: str = Form(...),
        translation_prompt: str = Form(...),
        review_prompt: str = Form(...),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).store.update_prompt_category(
                category_id,
                name=name,
                translation_prompt=translation_prompt,
                review_prompt=review_prompt,
            )
        except ValueError as error:
            return render_prompt_settings_error(request, error)
        return RedirectResponse(
            "/settings?prompt_saved=true#prompt-categories",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/settings/prompt-categories/{category_id}/archive")
    def archive_prompt_category(
        request: Request,
        category_id: str,
        csrf_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).store.set_prompt_category_archived(
                category_id,
                archived=True,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            "/settings?prompt_saved=true#prompt-categories",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/settings/prompt-categories/{category_id}/restore")
    def restore_prompt_category(
        request: Request,
        category_id: str,
        csrf_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).store.set_prompt_category_archived(
                category_id,
                archived=False,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            "/settings?prompt_saved=true#prompt-categories",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get(
        "/media/posters/{poster_path:path}",
        name="media_poster",
    )
    def media_poster(request: Request, poster_path: str) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        try:
            poster = orchestrator(request).library.resolve_poster(poster_path)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="poster not found",
            ) from error
        return FileResponse(poster)

    @app.get(
        "/media/actors/{actor_path:path}",
        name="media_actor_image",
    )
    def media_actor_image(request: Request, actor_path: str) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        try:
            actor_image = orchestrator(request).library.resolve_actor_image(
                actor_path
            )
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="actor image not found",
            ) from error
        return FileResponse(actor_image)

    @app.get("/jobs-fragment", response_class=HTMLResponse)
    def jobs_fragment(
        request: Request,
        jobs_page: int = 1,
        completed_page: int | None = None,
        folder: str = "",
        operation: str | None = None,
        phase: str | None = None,
        state: str | None = None,
        reason_code: str | None = None,
        status_group: str | None = None,
        stage_filter: str | None = None,
        compact: bool = False,
        dashboard_section: str = "",
    ) -> Any:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")
        if completed_page is not None and jobs_page == 1:
            jobs_page = completed_page
        service = orchestrator(request)
        if dashboard_section:
            templates = {
                "pipeline": "_dashboard_pipeline.html",
                "work": "_dashboard_work.html",
                "side": "_dashboard_side.html",
            }
            template = templates.get(dashboard_section)
            if template is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="invalid dashboard section",
                )
            context = dashboard_2d_data(
                service,
                audio_workers=configured_settings.audio_workers,
                include_library_progress=dashboard_section == "side",
            )
            return TEMPLATES.TemplateResponse(
                request,
                template,
                {
                    **context,
                    "csrf_token": request.session.get("csrf_token", ""),
                },
            )
        try:
            filters = normalize_job_list_filters(
                operation_filter=operation or None,
                phase_filter=phase or None,
                state_filter=state or None,
                reason_filter=reason_code or None,
                status_group=status_group or None,
                stage_filter=stage_filter or None,
            )
            context = job_list_context(
                service,
                jobs_page=1 if compact else jobs_page,
                operation_filter=filters[0],
                phase_filter=filters[1],
                state_filter=filters[2],
                reason_filter=filters[3],
                folder=folder,
                limit=DASHBOARD_JOB_LIMIT if compact else RECENT_JOB_LIMIT,
                paginated=not compact,
            )
            if compact:
                context.update(
                    {
                        "job_list_title": "최근 작업",
                        "show_bulk_actions": False,
                        "translatable_job_ids": set(),
                        "translatable_job_count": 0,
                        "stoppable_job_ids": set(),
                        "stoppable_selection_count": 0,
                        "retriable_job_ids": set(),
                        "retriable_selection_count": 0,
                    }
                )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return TEMPLATES.TemplateResponse(
            request,
            "_jobs_table.html",
            {
                "request": request,
                **context,
                "prompt_categories": service.prompt_revision_choices(),
                "csrf_token": request.session.get("csrf_token", ""),
            },
        )

    @app.get("/comparisons-fragment", response_class=HTMLResponse)
    def transcription_comparison_history_fragment(
        request: Request,
        comparisons_page: int = 1,
    ) -> Any:
        if not is_authenticated(request):
            raise HTTPException(
                status_code=401,
                detail="authentication required",
            )
        return TEMPLATES.TemplateResponse(
            request,
            "_comparison_history.html",
            transcription_comparison_history_context(
                orchestrator(request),
                comparisons_page=comparisons_page,
            ),
        )

    @app.get("/job-stats-fragment", response_class=HTMLResponse)
    def job_stats_fragment(request: Request) -> Any:
        if not is_authenticated(request):
            raise HTTPException(
                status_code=401,
                detail="authentication required",
            )
        return TEMPLATES.TemplateResponse(
            request,
            "_job_stats.html",
            {
                "job_stats": job_stats(orchestrator(request)),
                "dashboard_counts": dashboard_state_counts(
                    orchestrator(request).store.list_jobs(
                        limit=None,
                        include_comparison_transcriptions=False,
                    )
                ),
            },
        )

    @app.get("/gpu-stats-fragment", response_class=HTMLResponse)
    def gpu_stats_fragment(request: Request) -> Any:
        if not is_authenticated(request):
            raise HTTPException(
                status_code=401,
                detail="authentication required",
            )
        return TEMPLATES.TemplateResponse(
            request,
            "_gpu_stats.html",
            {"gpu_snapshot": request.app.state.gpu_monitor.snapshot()},
        )

    @app.get("/jobs/events")
    def job_events(request: Request) -> Any:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")
        change_hook: JobChangeHook = request.app.state.job_change_hook

        async def stream() -> AsyncIterator[str]:
            version = change_hook.version
            yield f"retry: 3000\nevent: ready\ndata: {version}\n\n"
            while True:
                updated_version = await change_hook.wait(version)
                if updated_version == version:
                    yield ": keep-alive\n\n"
                    continue
                version = updated_version
                yield f"id: {version}\nevent: jobs\ndata: changed\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/operations/metrics")
    def operations_metrics(request: Request) -> Any:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")
        return orchestrator(request).store.operational_metrics()

    @app.get("/api/operations/metrics/prometheus")
    def operations_metrics_prometheus(request: Request) -> Response:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")
        snapshot = orchestrator(request).store.operational_metrics()
        return Response(
            prometheus_exposition(snapshot),
            media_type="text/plain; version=0.0.4",
        )

    @app.post("/jobs", response_class=HTMLResponse)
    def create_job(
        request: Request,
        source_rels: list[str] | None = Form(None),
        source_groups: list[str] | None = Form(None),
        folder_rels: list[str] | None = Form(None),
        return_folder: str = Form(""),
        return_query: str = Form(""),
        return_actor: str = Form(""),
        csrf_token: str = Form(""),
        force_overwrite: bool = Form(False),
        backend: str = Form("auto"),
        audio_stream: str = Form("0"),
        start_seconds: str = Form("0"),
        duration_seconds: str = Form(""),
        kotoba_chunk_length_seconds: str = Form("15"),
        whisperx_chunk_length_seconds: str = Form("30"),
        anime_max_group_duration_seconds: str = Form("2.0"),
        qwen_max_group_duration_seconds: str = Form("3.0"),
        num_speakers: str = Form(""),
        min_speakers: str = Form(""),
        max_speakers: str = Form(""),
        add_punctuation: bool = Form(False),
        noise_filter: list[bool] | None = Form(None),
        operation: str = Form("full"),
        prompt_category_id: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        normalized_backend = backend.strip().lower()
        effective_prompt_category_id = prompt_category_id.strip()
        if normalized_backend == "auto":
            normalized_backend = "whisperjav"
            if operation in {"translate", "full"}:
                effective_prompt_category_id = "jav"
        options = {
            "backend": normalized_backend,
            "audio_stream": audio_stream,
            "start_seconds": start_seconds,
            "duration_seconds": duration_seconds,
            "chunk_length_seconds": (
                whisperx_chunk_length_seconds
                if normalized_backend == "whisperx"
                else kotoba_chunk_length_seconds
            ),
            "num_speakers": num_speakers,
            "min_speakers": min_speakers,
            "max_speakers": max_speakers,
            "add_punctuation": add_punctuation,
            "noise_filter": noise_filter[-1] if noise_filter else True,
        }
        if normalized_backend == "hybrid" or operation == "compare":
            options["hybrid_rescue"] = {
                "kotoba_chunk_length_seconds": kotoba_chunk_length_seconds,
                "whisperx_chunk_length_seconds": whisperx_chunk_length_seconds,
            }
        if normalized_backend == "whisperjav" or operation == "compare":
            options["whisperjav"] = {
                "anime_max_group_duration_seconds": (
                    anime_max_group_duration_seconds
                ),
                "qwen_max_group_duration_seconds": (
                    qwen_max_group_duration_seconds
                ),
            }
        try:
            service = orchestrator(request)
            expanded_source_rels = [
                *(source_rels or []),
                *decode_source_groups(source_groups),
            ]
            if (
                operation in {"translate", "full"}
                and not effective_prompt_category_id
            ):
                raise ValueError("번역 프롬프트 카테고리를 선택하세요.")
            selected_sources, skipped = service.expand_job_sources(
                expanded_source_rels,
                folder_rels or [],
                force_overwrite=force_overwrite,
                operation=operation,
            )
            comparison_id: str | None = None
            if operation == "compare":
                comparison_id, jobs = (
                    service.create_transcription_comparison(
                        selected_sources,
                        options=options,
                    )
                )
            else:
                jobs = service.create_jobs(
                    selected_sources,
                    force_overwrite=force_overwrite,
                    options=options,
                    operation=operation,
                    prompt_category_id=(
                        effective_prompt_category_id
                        if operation in {"translate", "full"}
                        else None
                    ),
                )
        except (FileExistsError, OSError, ValueError) as error:
            try:
                context = media_context(
                    request,
                    error=str(error),
                    folder=return_folder,
                    query=return_query,
                    actor=return_actor,
                )
            except ValueError:
                context = media_context(request, error=str(error))
            return TEMPLATES.TemplateResponse(
                request,
                "media.html",
                context,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if comparison_id is not None:
            query = {"skipped": skipped} if skipped else {}
            suffix = f"?{urlencode(query)}" if query else ""
            return RedirectResponse(
                f"/comparisons/{comparison_id}{suffix}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        query = {"queued": len(jobs)}
        if skipped:
            query["skipped"] = skipped
        if return_folder:
            query["folder"] = return_folder
        if return_query.strip():
            query["q"] = return_query.strip()
        if return_actor.strip():
            query["actor"] = return_actor.strip()
        return RedirectResponse(
            f"/media?{urlencode(query)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.api_route(
        "/jobs/{job_id}/video",
        methods=["GET", "HEAD"],
        name="job_video",
    )
    def job_video(request: Request, job_id: str) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        try:
            source = service.library.resolve_file(job.source_rel)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="media file not found",
            ) from error

        file_size = source.stat().st_size
        base_headers = {
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-cache",
            "Content-Disposition": (
                "inline; filename*=UTF-8''" + quote(source.name)
            ),
        }
        try:
            byte_range = parse_byte_range(
                request.headers.get("range"),
                file_size,
            )
        except (OverflowError, ValueError):
            return Response(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                headers={
                    **base_headers,
                    "Content-Range": f"bytes */{file_size}",
                },
            )

        if byte_range is None:
            start, end = 0, file_size - 1
            response_status = status.HTTP_200_OK
        else:
            start, end = byte_range
            response_status = status.HTTP_206_PARTIAL_CONTENT
            base_headers["Content-Range"] = (
                f"bytes {start}-{end}/{file_size}"
            )
        base_headers["Content-Length"] = str(max(0, end - start + 1))
        media_type = guess_media_type(source.name)
        if request.method == "HEAD":
            return Response(
                status_code=response_status,
                media_type=media_type,
                headers=base_headers,
            )
        return StreamingResponse(
            iter_file_range(source, start, end),
            status_code=response_status,
            media_type=media_type,
            headers=base_headers,
        )

    @app.get(
        "/jobs/{job_id}/subtitles.vtt",
        name="job_subtitles",
    )
    def job_subtitles(request: Request, job_id: str) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="subtitle not found")
        try:
            external_subtitles = service.library.external_subtitles(job.source_rel)
            if external_subtitles:
                webvtt = render_external_webvtt(
                    parse_subtitle(external_subtitles[0])
                )
            elif job.srt_path:
                translation_override = None
                published = service.store.published_subtitle_generation(
                    job.id
                )
                if (
                    published is not None
                    and published["translation_generation_id"] is not None
                    and job.transcript_path
                    and Path(job.transcript_path).is_file()
                    and sha256_file(Path(job.transcript_path))
                    == published["transcript_hash"]
                ):
                    translation_generation = (
                        service.store.get_translation_generation(
                            published["translation_generation_id"]
                        )
                    )
                    if translation_generation is not None:
                        translation_override = Path(
                            translation_generation["artifact_path"]
                        )
                webvtt = (
                    styled_webvtt(
                        job,
                        translation_override=translation_override,
                    )
                    if published is None or translation_override is not None
                    else None
                )
                if webvtt is None:
                    source = service.library.resolve_file(job.source_rel)
                    subtitle = source.with_name(f"{source.stem}.ko.srt")
                    webvtt = srt_to_webvtt(
                        subtitle.read_text(encoding="utf-8-sig")
                    )
            else:
                raise FileNotFoundError("subtitle not found")
        except (
            KeyError,
            OSError,
            TypeError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="subtitle not found",
            ) from error
        return Response(
            webvtt,
            media_type="text/vtt",
            headers={"Cache-Control": "private, no-cache"},
        )

    @app.get(
        "/media/subtitles.vtt",
        name="media_external_subtitles",
    )
    def media_external_subtitles(request: Request, path: str) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        try:
            external_subtitles = orchestrator(request).library.external_subtitles(path)
            if not external_subtitles:
                raise FileNotFoundError("external subtitle not found")
            webvtt = render_external_webvtt(
                parse_subtitle(external_subtitles[0])
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="external subtitle not found",
            ) from error
        return Response(
            webvtt,
            media_type="text/vtt",
            headers={"Cache-Control": "private, no-cache"},
        )

    @app.get(
        "/jobs/{job_id}/subtitle.{subtitle_format}",
        name="job_subtitle_file",
    )
    def job_subtitle_file(
        request: Request,
        job_id: str,
        subtitle_format: str,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        job = orchestrator(request).store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        details = {
            "srt": (job.srt_path, "application/x-subrip"),
            "ass": (job.ass_path, "text/x-ssa"),
        }
        if subtitle_format not in details:
            raise HTTPException(status_code=404, detail="subtitle not found")
        path_value, media_type = details[subtitle_format]
        if not path_value or not Path(path_value).is_file():
            raise HTTPException(status_code=404, detail="subtitle not found")
        source_name = Path(job.source_rel).stem
        return FileResponse(
            path_value,
            media_type=media_type,
            filename=f"{source_name}.ko.{subtitle_format}",
        )

    @app.get("/comparisons/{comparison_id}", response_class=HTMLResponse)
    def transcription_comparison_page(
        request: Request,
        comparison_id: str,
        skipped: int = 0,
        retried: int | None = None,
        adjusted: int = 0,
        rerun: bool = False,
        reused_audio: int = 0,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        notice = None
        if retried is not None:
            notice = (
                f"실패한 전사 작업 {max(0, retried)}개를 재시도했습니다."
                if retried > 0
                else "재시도할 실패 작업이 없습니다."
            )
            if adjusted > 0:
                notice += (
                    f" 기존 WhisperX 청크 {adjusted}개는 30초로 "
                    "보정했습니다."
                )
        elif rerun:
            notice = "변경한 분할 설정으로 새 전사 비교를 시작했습니다."
            if reused_audio > 0:
                notice += (
                    f" 기존 추출 오디오 {reused_audio}개를 재사용하며 "
                    "전사부터 실행합니다."
                )
        context = transcription_comparison_context(
            request,
            comparison_id,
            skipped=max(0, skipped),
            notice=notice,
        )
        return TEMPLATES.TemplateResponse(
            request,
            "comparison.html",
            context,
        )

    @app.get(
        "/comparisons/{comparison_id}/panel",
        response_class=HTMLResponse,
    )
    def transcription_comparison_panel(
        request: Request,
        comparison_id: str,
    ) -> Any:
        if not is_authenticated(request):
            raise HTTPException(
                status_code=401,
                detail="authentication required",
            )
        return TEMPLATES.TemplateResponse(
            request,
            "_comparison_panel.html",
            transcription_comparison_context(request, comparison_id),
        )

    @app.post("/comparisons/{comparison_id}/retry")
    def retry_transcription_comparison(
        request: Request,
        comparison_id: str,
        csrf_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        service = orchestrator(request)
        comparison_jobs = transcription_comparison_jobs(
            service,
            comparison_id,
        )
        retried_count = 0
        adjusted_count = 0
        for job in comparison_jobs:
            if job.status not in RETRYABLE_STATUSES:
                continue
            retried = service.retry(job.id)
            if retried.options != job.options:
                adjusted_count += 1
            retried_count += 1
        query = {"retried": retried_count}
        if adjusted_count:
            query["adjusted"] = adjusted_count
        return RedirectResponse(
            f"/comparisons/{comparison_id}?{urlencode(query)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/comparisons/{comparison_id}/rerun",
        response_class=HTMLResponse,
    )
    def rerun_transcription_comparison(
        request: Request,
        comparison_id: str,
        csrf_token: str = Form(""),
        kotoba_chunk_length_seconds: str = Form("15"),
        whisperx_chunk_length_seconds: str = Form("30"),
        anime_max_group_duration_seconds: str = Form("2.0"),
        qwen_max_group_duration_seconds: str = Form("3.0"),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        service = orchestrator(request)
        comparison_jobs = transcription_comparison_jobs(
            service,
            comparison_id,
        )

        def positive_chunk_seconds(value: str, label: str) -> int:
            try:
                normalized = int(value)
            except ValueError as error:
                raise ValueError(
                    f"{label} 청크는 정수로 입력하세요."
                ) from error
            if normalized < 1:
                raise ValueError(f"{label} 청크는 1초 이상이어야 합니다.")
            if (
                label == "WhisperX"
                and normalized > WHISPERX_MAX_CHUNK_LENGTH_SECONDS
            ):
                raise ValueError(
                    "WhisperX 청크는 "
                    f"{WHISPERX_MAX_CHUNK_LENGTH_SECONDS}초 이하여야 합니다."
                )
            return normalized

        def bounded_group_seconds(value: str, label: str) -> float:
            try:
                normalized = float(value)
            except ValueError as error:
                raise ValueError(f"{label}은 숫자로 입력하세요.") from error
            if not MIN_MAX_GROUP_SECONDS <= normalized <= MAX_MAX_GROUP_SECONDS:
                raise ValueError(
                    f"{label}: "
                    f"{MIN_MAX_GROUP_SECONDS}초 이상 "
                    f"{MAX_MAX_GROUP_SECONDS}초 이하여야 합니다."
                )
            return normalized

        try:
            kotoba_chunk = positive_chunk_seconds(
                kotoba_chunk_length_seconds,
                "Kotoba",
            )
            whisperx_chunk = positive_chunk_seconds(
                whisperx_chunk_length_seconds,
                "WhisperX",
            )
            anime_max_group = bounded_group_seconds(
                anime_max_group_duration_seconds,
                "WhisperJAV 1차 그룹 길이",
            )
            qwen_max_group = bounded_group_seconds(
                qwen_max_group_duration_seconds,
                "WhisperJAV 2차 그룹 길이",
            )
            options = transcription_comparison_rerun_options(
                comparison_jobs,
                kotoba_chunk_length_seconds=kotoba_chunk,
                whisperx_chunk_length_seconds=whisperx_chunk,
                anime_max_group_duration_seconds=anime_max_group,
                qwen_max_group_duration_seconds=qwen_max_group,
            )
            source_rels = list(
                dict.fromkeys(job.source_rel for job in comparison_jobs)
            )
            new_comparison_id, new_jobs = (
                service.create_transcription_comparison(
                    source_rels,
                    options=options,
                    reuse_audio_from=comparison_jobs,
                    parent_comparison_id=comparison_id,
                )
            )
        except (OSError, ValueError) as error:
            return TEMPLATES.TemplateResponse(
                request,
                "comparison.html",
                transcription_comparison_context(
                    request,
                    comparison_id,
                    error=str(error),
                    chunk_values={
                        "kotoba_chunk_length_seconds": (
                            kotoba_chunk_length_seconds
                        ),
                        "whisperx_chunk_length_seconds": (
                            whisperx_chunk_length_seconds
                        ),
                        "anime_max_group_duration_seconds": (
                            anime_max_group_duration_seconds
                        ),
                        "qwen_max_group_duration_seconds": (
                            qwen_max_group_duration_seconds
                        ),
                    },
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        reused_audio_count = len(
            {
                job.source_rel
                for job in new_jobs
                if COMPARISON_AUDIO_SOURCE_JOB_ID_OPTION in job.options
            }
        )
        return RedirectResponse(
            f"/comparisons/{new_comparison_id}?"
            f"{urlencode({'rerun': 'true', 'reused_audio': reused_audio_count})}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post(
        "/comparisons/{comparison_id}/translate",
        response_class=HTMLResponse,
    )
    def translate_transcription_comparison_results(
        request: Request,
        comparison_id: str,
        job_ids: list[str] | None = Form(None),
        prompt_category_id: str = Form(""),
        csrf_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        service = orchestrator(request)
        try:
            if not prompt_category_id.strip():
                raise ValueError("번역 프롬프트 카테고리를 선택하세요.")
            created = service.create_comparison_translation_jobs(
                comparison_id,
                job_ids or [],
                prompt_category_id=prompt_category_id,
            )
        except (OSError, UnicodeError, ValueError) as error:
            return TEMPLATES.TemplateResponse(
                request,
                "comparison.html",
                transcription_comparison_context(
                    request,
                    comparison_id,
                    error=str(error),
                    selected_translation_job_ids=job_ids or [],
                    translation_prompt_category_id=prompt_category_id,
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return RedirectResponse(
            f"/jobs?{urlencode({'translations_queued': len(created)})}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    def external_subtitles_for_job(
        service: SubtitleOrchestrator,
        source_rel: str,
    ) -> tuple[Path, ...]:
        try:
            return service.library.external_subtitles(source_rel)
        except (OSError, ValueError):
            return ()

    def current_subtitle_validation(
        service: SubtitleOrchestrator,
        job: Any,
        external_subtitles: Sequence[Path],
    ) -> dict[str, Any] | None:
        candidate_path = next(
            (
                Path(value)
                for value in (job.srt_path, job.ass_path)
                if value and Path(value).is_file()
            ),
            None,
        )
        if not external_subtitles or candidate_path is None:
            return None
        try:
            return service.store.get_subtitle_validation(
                job_id=job.id,
                external_hash=subtitle_asset_hash(external_subtitles),
                candidate_hash=sha256_file(candidate_path),
            )
        except OSError:
            return None

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(
        request: Request,
        job_id: str,
        return_operation: str = "",
        return_phase: str = "",
        return_state: str = "",
        return_reason_code: str = "",
        return_status_group: str = "",
        return_stage_filter: str = "",
        return_jobs_page: int = 1,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        try:
            return_filters = normalize_job_list_filters(
                operation_filter=return_operation or None,
                phase_filter=return_phase or None,
                state_filter=return_state or None,
                reason_filter=return_reason_code or None,
                status_group=return_status_group or None,
                stage_filter=return_stage_filter or None,
            )
            job_return_url = job_list_action_location(
                return_folder="",
                return_operation=return_filters[0] or "",
                return_phase=return_filters[1] or "",
                return_state=return_filters[2] or "",
                return_reason_code=return_filters[3] or "",
                return_jobs_page=return_jobs_page,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        job_panel_return_query = urlencode(
            {
                key: value
                for key, value in {
                    "return_operation": return_filters[0],
                    "return_phase": return_filters[1],
                    "return_state": return_filters[2],
                    "return_reason_code": return_filters[3],
                    "return_jobs_page": (
                        max(1, return_jobs_page)
                        if max(1, return_jobs_page) > 1
                        else None
                    ),
                }.items()
                if value not in (None, "")
            }
        )
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        external_subtitles = external_subtitles_for_job(service, job.source_rel)
        return TEMPLATES.TemplateResponse(
            request,
            "job.html",
            {
                "job": job,
                "job_return_url": job_return_url,
                "job_panel_return_query": job_panel_return_query,
                "return_operation": return_filters[0],
                "return_phase": return_filters[1],
                "return_state": return_filters[2],
                "return_reason_code": return_filters[3],
                "return_jobs_page": max(1, return_jobs_page),
                "events": [
                    event
                    for event in service.store.events(job_id)
                    if not event["message"].startswith(
                        ("transcription chunks:", "translation checkpoint saved")
                    )
                ],
                "csrf_token": request.session.get("csrf_token", ""),
                "prompt_categories": service.prompt_revision_choices(),
                "video_mime_type": guess_media_type(job.source_rel),
                "external_subtitle": (
                    {
                        "path": external_subtitles[0].name,
                        "formats": [
                            path.suffix.lower().lstrip(".")
                            for path in external_subtitles
                        ],
                    }
                    if external_subtitles
                    else None
                ),
                "subtitle_validation": current_subtitle_validation(
                    service,
                    job,
                    external_subtitles,
                ),
                "subtitle_validator": service.subtitle_validator_view(),
                "transcript_revisions": transcript_revision_history(
                    service,
                    job,
                ),
                "translation_generations": translation_generation_history(
                    service,
                    job.id,
                ),
                "subtitle_generations": subtitle_generation_view(
                    service.store.list_subtitle_generations(job.id)
                ),
                "artifact_names": {
                    "transcript": artifact_filename(
                        job.source_rel,
                        "transcript",
                    ),
                    "translation": artifact_filename(
                        job.source_rel,
                        "translation",
                    ),
                },
            },
        )

    @app.get("/jobs/{job_id}/panel", response_class=HTMLResponse)
    def job_panel(
        request: Request,
        job_id: str,
        return_operation: str = "",
        return_phase: str = "",
        return_state: str = "",
        return_reason_code: str = "",
        return_status_group: str = "",
        return_stage_filter: str = "",
        return_jobs_page: int = 1,
    ) -> Any:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            return_filters = normalize_job_list_filters(
                operation_filter=return_operation or None,
                phase_filter=return_phase or None,
                state_filter=return_state or None,
                reason_filter=return_reason_code or None,
                status_group=return_status_group or None,
                stage_filter=return_stage_filter or None,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        external_subtitles = external_subtitles_for_job(service, job.source_rel)
        return TEMPLATES.TemplateResponse(
            request,
            "_job_panel.html",
            {
                "job": job,
                "return_operation": return_filters[0],
                "return_phase": return_filters[1],
                "return_state": return_filters[2],
                "return_reason_code": return_filters[3],
                "return_jobs_page": max(1, return_jobs_page),
                "events": [
                    event
                    for event in service.store.events(job_id)
                    if not event["message"].startswith(
                        ("transcription chunks:", "translation checkpoint saved")
                    )
                ],
                "csrf_token": request.session.get("csrf_token", ""),
                "prompt_categories": service.prompt_revision_choices(),
                "video_mime_type": guess_media_type(job.source_rel),
                "external_subtitle": (
                    {
                        "path": external_subtitles[0].name,
                        "formats": [
                            path.suffix.lower().lstrip(".")
                            for path in external_subtitles
                        ],
                    }
                    if external_subtitles
                    else None
                ),
                "subtitle_validation": current_subtitle_validation(
                    service,
                    job,
                    external_subtitles,
                ),
                "subtitle_validator": service.subtitle_validator_view(),
                "transcript_revisions": transcript_revision_history(
                    service,
                    job,
                ),
                "translation_generations": translation_generation_history(
                    service,
                    job.id,
                ),
                "subtitle_generations": subtitle_generation_view(
                    service.store.list_subtitle_generations(job.id)
                ),
                "artifact_names": {
                    "transcript": artifact_filename(
                        job.source_rel,
                        "transcript",
                    ),
                    "translation": artifact_filename(
                        job.source_rel,
                        "translation",
                    ),
                },
            },
        )

    @app.post("/jobs/{job_id}/validate-external-subtitle")
    def validate_external_subtitle(
        request: Request,
        job_id: str,
        csrf_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        try:
            external_subtitles = service.library.external_subtitles(job.source_rel)
            if not external_subtitles:
                raise ValueError("외부 자막이 없습니다.")
            candidate_path = next(
                (
                    Path(value)
                    for value in (job.srt_path, job.ass_path)
                    if value and Path(value).is_file()
                ),
                None,
            )
            if candidate_path is None:
                raise ValueError("비교할 시스템 생성 자막이 없습니다.")
            metrics = compare_subtitles(
                parse_subtitle(external_subtitles[0]),
                parse_subtitle(candidate_path),
            )
            validation = service.store.save_subtitle_validation(
                job_id=job.id,
                source_rel=job.source_rel,
                external_path=str(external_subtitles[0]),
                external_hash=subtitle_asset_hash(external_subtitles),
                candidate_path=str(candidate_path),
                candidate_hash=sha256_file(candidate_path),
                metrics=metrics,
            )
            service.store.add_event(
                job.id,
                "info",
                "외부 자막 비교 검증 완료",
                event_code="subtitle.validation_completed",
                payload={"validation_id": validation["id"]},
            )
            service.record_subtitle_validation("local", "completed")
        except (OSError, UnicodeError, ValueError) as error:
            service.record_subtitle_validation("local", "failed")
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            f"/jobs/{job.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/validate-external-subtitle/llm")
    def validate_external_subtitle_with_llm(
        request: Request,
        job_id: str,
        csrf_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        external_subtitles = external_subtitles_for_job(service, job.source_rel)
        validation = current_subtitle_validation(
            service,
            job,
            external_subtitles,
        )
        if validation is None:
            raise HTTPException(
                status_code=400,
                detail="현재 자막 파일의 비교 검증을 먼저 실행하세요.",
            )
        try:
            _updated, cached = service.validate_subtitles_with_llm(
                validation["id"]
            )
            service.store.add_event(
                job.id,
                "info",
                "상용 LLM 자막 검증 저장 결과 재사용"
                if cached
                else "상용 LLM 자막 검증 완료",
                event_code=(
                    "subtitle.llm_validation_cache_hit"
                    if cached
                    else "subtitle.llm_validation_completed"
                ),
                payload={"validation_id": validation["id"]},
            )
        except (ExternalServiceError, ValueError) as error:
            raise HTTPException(
                status_code=400,
                detail=service.sanitize_external_error(str(error)),
            ) from error
        return RedirectResponse(
            f"/jobs/{job.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/retry")
    def retry_job(
        request: Request,
        job_id: str,
        csrf_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).retry(job_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/restart-translation")
    def restart_translation(
        request: Request,
        job_id: str,
        csrf_token: str = Form(""),
        return_folder: str | None = Form(None),
        prompt_category_id: str = Form(""),
        transcript_revision_id: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            if not prompt_category_id.strip():
                raise ValueError("번역 프롬프트 카테고리를 선택하세요.")
            orchestrator(request).restart_translation(
                job_id,
                prompt_category_id,
                transcript_revision_id=(
                    None
                    if transcript_revision_id.strip() in {"", "__current__"}
                    else transcript_revision_id.strip()
                ),
            )
        except (OSError, UnicodeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            job_action_location(job_id, return_folder),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/delete")
    def delete_job(
        request: Request,
        job_id: str,
        csrf_token: str = Form(""),
        return_folder: str | None = Form(None),
        return_operation: str = Form(""),
        return_phase: str = Form(""),
        return_state: str = Form(""),
        return_reason_code: str = Form(""),
        return_status_group: str = Form(""),
        return_stage_filter: str = Form(""),
        return_jobs_page: int = Form(1),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            return_location = job_list_action_location(
                return_folder=return_folder or "",
                return_operation=return_operation,
                return_phase=return_phase,
                return_state=return_state,
                return_reason_code=return_reason_code,
                return_status_group=return_status_group,
                return_stage_filter=return_stage_filter,
                return_jobs_page=return_jobs_page,
            )
            orchestrator(request).delete_job_record(job_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            return_location,
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/pause-translation")
    def pause_translation(
        request: Request,
        job_id: str,
        csrf_token: str = Form(""),
        return_folder: str | None = Form(None),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).pause_translation(job_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            job_action_location(
                job_id,
                return_folder,
                translation_pause_requested=1,
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/resume-translation")
    def resume_translation(
        request: Request,
        job_id: str,
        csrf_token: str = Form(""),
        return_folder: str | None = Form(None),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).resume_translation(job_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            job_action_location(job_id, return_folder),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/translate-selected", response_class=HTMLResponse)
    def translate_selected_jobs(
        request: Request,
        job_ids: list[str] | None = Form(None),
        csrf_token: str = Form(""),
        prompt_category_id: str = Form(""),
        return_folder: str = Form(""),
        return_operation: str = Form(""),
        return_phase: str = Form(""),
        return_state: str = Form(""),
        return_reason_code: str = Form(""),
        return_status_group: str = Form(""),
        return_jobs_page: int = Form(1),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        service = orchestrator(request)
        try:
            filters = normalize_job_list_filters(
                operation_filter=return_operation or None,
                phase_filter=return_phase or None,
                state_filter=return_state or None,
                reason_filter=return_reason_code or None,
                status_group=return_status_group or None,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        try:
            if not prompt_category_id.strip():
                raise ValueError("번역 프롬프트 카테고리를 선택하세요.")
            created = service.create_selected_translation_jobs(
                job_ids or [],
                prompt_category_id=prompt_category_id,
            )
        except (OSError, UnicodeError, ValueError) as error:
            context = {
                "request": request,
                **job_list_context(
                    service,
                    jobs_page=return_jobs_page,
                    operation_filter=filters[0],
                    phase_filter=filters[1],
                    state_filter=filters[2],
                    reason_filter=filters[3],
                ),
                **job_stage_filter_context(
                    service,
                    operation_filter=filters[0],
                    phase_filter=filters[1],
                    state_filter=filters[2],
                    reason_filter=filters[3],
                ),
                "status_groups": JOB_STATUS_GROUP_LABELS,
                "csrf_token": request.session.get("csrf_token", ""),
                "prompt_categories": service.prompt_revision_choices(),
                "notice": None,
                "error": str(error),
            }
            return TEMPLATES.TemplateResponse(
                request,
                "jobs.html",
                context,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return RedirectResponse(
            job_list_action_location(
                return_folder=return_folder,
                return_operation=return_operation,
                return_phase=return_phase,
                return_state=(
                    "" if return_state == "done" else return_state
                ),
                return_reason_code=return_reason_code,
                return_status_group=(
                    ""
                    if return_status_group == "completed"
                    else return_status_group
                ),
                return_jobs_page=(
                    1 if return_status_group == "completed" else return_jobs_page
                ),
                translations_queued=len(created),
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/pause-all-translations")
    def pause_all_translations(
        request: Request,
        csrf_token: str = Form(""),
        return_folder: str = Form(""),
        return_operation: str = Form(""),
        return_phase: str = Form(""),
        return_state: str = Form(""),
        return_reason_code: str = Form(""),
        return_status_group: str = Form(""),
        return_stage_filter: str = Form(""),
        return_jobs_page: int = Form(1),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            return_location = job_list_action_location(
                return_folder=return_folder,
                return_operation=return_operation,
                return_phase=return_phase,
                return_state=return_state,
                return_reason_code=return_reason_code,
                return_status_group=return_status_group,
                return_stage_filter=return_stage_filter,
                return_jobs_page=return_jobs_page,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        paused_count = orchestrator(request).pause_all_translations()
        separator = "&" if "?" in return_location else "?"
        return RedirectResponse(
            f"{return_location}{separator}translations_paused={paused_count}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/stop-all")
    def stop_all_jobs(
        request: Request,
        csrf_token: str = Form(""),
        return_folder: str = Form(""),
        return_operation: str = Form(""),
        return_phase: str = Form(""),
        return_state: str = Form(""),
        return_reason_code: str = Form(""),
        return_status_group: str = Form(""),
        return_stage_filter: str = Form(""),
        return_jobs_page: int = Form(1),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            return_location = job_list_action_location(
                return_folder=return_folder,
                return_operation=return_operation,
                return_phase=return_phase,
                return_state=return_state,
                return_reason_code=return_reason_code,
                return_status_group=return_status_group,
                return_stage_filter=return_stage_filter,
                return_jobs_page=return_jobs_page,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        stopped_count = orchestrator(request).stop_all_jobs()
        separator = "&" if "?" in return_location else "?"
        return RedirectResponse(
            f"{return_location}{separator}jobs_stopped={stopped_count}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/stop-selected")
    def stop_selected_jobs(
        request: Request,
        job_ids: list[str] | None = Form(None),
        csrf_token: str = Form(""),
        return_folder: str = Form(""),
        return_operation: str = Form(""),
        return_phase: str = Form(""),
        return_state: str = Form(""),
        return_reason_code: str = Form(""),
        return_status_group: str = Form(""),
        return_stage_filter: str = Form(""),
        return_jobs_page: int = Form(1),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        redirect_page = (
            1
            if any(
                (
                    return_operation,
                    return_phase,
                    return_state,
                    return_reason_code,
                    return_status_group,
                    return_stage_filter,
                )
            )
            else return_jobs_page
        )
        try:
            return_location = job_list_action_location(
                return_folder=return_folder,
                return_operation=return_operation,
                return_phase=return_phase,
                return_state=return_state,
                return_reason_code=return_reason_code,
                return_status_group=return_status_group,
                return_stage_filter=return_stage_filter,
                return_jobs_page=redirect_page,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        stopped_count = orchestrator(request).stop_jobs(job_ids or [])
        separator = "&" if "?" in return_location else "?"
        return RedirectResponse(
            f"{return_location}{separator}jobs_stopped={stopped_count}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/retry-all")
    def retry_all_jobs(
        request: Request,
        csrf_token: str = Form(""),
        return_folder: str = Form(""),
        return_operation: str = Form(""),
        return_phase: str = Form(""),
        return_state: str = Form(""),
        return_reason_code: str = Form(""),
        return_status_group: str = Form(""),
        return_stage_filter: str = Form(""),
        return_jobs_page: int = Form(1),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            return_location = job_list_action_location(
                return_folder=return_folder,
                return_operation=return_operation,
                return_phase=return_phase,
                return_state=return_state,
                return_reason_code=return_reason_code,
                return_status_group=return_status_group,
                return_stage_filter=return_stage_filter,
                return_jobs_page=(
                    1
                    if any(
                        (
                            return_operation,
                            return_phase,
                            return_state,
                            return_reason_code,
                            return_status_group,
                            return_stage_filter,
                        )
                    )
                    else return_jobs_page
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        retried_count = orchestrator(request).retry_all_jobs()
        separator = "&" if "?" in return_location else "?"
        return RedirectResponse(
            f"{return_location}{separator}jobs_retried={retried_count}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/retry-selected")
    def retry_selected_jobs(
        request: Request,
        job_ids: list[str] | None = Form(None),
        csrf_token: str = Form(""),
        return_folder: str = Form(""),
        return_operation: str = Form(""),
        return_phase: str = Form(""),
        return_state: str = Form(""),
        return_reason_code: str = Form(""),
        return_status_group: str = Form(""),
        return_stage_filter: str = Form(""),
        return_jobs_page: int = Form(1),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        redirect_page = (
            1
            if any(
                (
                    return_operation,
                    return_phase,
                    return_state,
                    return_reason_code,
                    return_status_group,
                    return_stage_filter,
                )
            )
            else return_jobs_page
        )
        try:
            return_location = job_list_action_location(
                return_folder=return_folder,
                return_operation=return_operation,
                return_phase=return_phase,
                return_state=return_state,
                return_reason_code=return_reason_code,
                return_status_group=return_status_group,
                return_stage_filter=return_stage_filter,
                return_jobs_page=redirect_page,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        retried_count = orchestrator(request).retry_jobs(job_ids or [])
        separator = "&" if "?" in return_location else "?"
        return RedirectResponse(
            f"{return_location}{separator}jobs_retried={retried_count}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/{job_id}/reprocess")
    def reprocess_job(
        request: Request,
        job_id: str,
        csrf_token: str = Form(""),
        operation: str = Form(...),
        prompt_category_id: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            if (
                operation in {"translate", "full"}
                and not prompt_category_id.strip()
            ):
                raise ValueError("번역 프롬프트 카테고리를 선택하세요.")
            created = orchestrator(request).reprocess(
                job_id,
                operation,
                prompt_category_id=(
                    prompt_category_id
                    if operation in {"translate", "full"}
                    else None
                ),
            )
        except (FileExistsError, OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            f"/jobs/{created.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get(
        "/jobs/{job_id}/translation-comparison",
        response_class=HTMLResponse,
        name="translation_generation_comparison",
    )
    def translation_generation_comparison_page(
        request: Request,
        job_id: str,
        base_generation_id: str = "",
        candidate_generation_id: str = "",
        comparison_filter: str = "changes",
        comparison_page: int = 1,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        generations = translation_generation_history(service, job.id)
        if len(generations) < 2:
            raise HTTPException(
                status_code=400,
                detail="비교할 번역 버전이 두 개 이상 필요합니다.",
            )
        if not base_generation_id:
            base_generation_id = str(generations[-2]["id"])
        if not candidate_generation_id:
            candidate_generation_id = str(generations[-1]["id"])
        generation_by_id = {
            str(generation["id"]): generation
            for generation in generations
        }
        base = generation_by_id.get(base_generation_id)
        candidate = generation_by_id.get(candidate_generation_id)
        if base is None or candidate is None:
            raise HTTPException(
                status_code=404,
                detail="번역 버전을 찾을 수 없습니다.",
            )
        if base_generation_id == candidate_generation_id:
            raise HTTPException(
                status_code=400,
                detail="서로 다른 번역 버전을 선택하세요.",
            )
        valid_filters = {key for key, _label in TRANSLATION_COMPARISON_FILTERS}
        if comparison_filter not in valid_filters:
            raise HTTPException(
                status_code=400,
                detail="번역 비교 필터가 올바르지 않습니다.",
            )

        try:
            comparison = compare_translation_items(
                service.store.translation_items(base_generation_id),
                service.store.translation_items(candidate_generation_id),
                base_source_texts=translation_source_texts(
                    service,
                    job,
                    base,
                ),
                candidate_source_texts=translation_source_texts(
                    service,
                    job,
                    candidate,
                ),
            )
        except ValueError as error:
            raise HTTPException(
                status_code=409,
                detail="저장된 번역 항목을 비교할 수 없습니다.",
            ) from error
        filter_predicates: dict[str, Callable[[Mapping[str, Any]], bool]] = {
            "changes": lambda row: bool(row["has_change"]),
            "all": lambda _row: True,
            "changed": lambda row: row["state"] == "changed",
            "added": lambda row: row["state"] == "added",
            "removed": lambda row: row["state"] == "removed",
            "source_changed": lambda row: bool(row["source_changed"]),
            "unchanged": lambda row: not bool(row["has_change"]),
        }
        filtered_rows = [
            row
            for row in comparison["rows"]
            if filter_predicates[comparison_filter](row)
        ]
        page_count = max(
            1,
            math.ceil(
                len(filtered_rows) / TRANSLATION_COMPARISON_PAGE_LIMIT
            ),
        )
        comparison_page = min(page_count, max(1, comparison_page))
        offset = (
            (comparison_page - 1) * TRANSLATION_COMPARISON_PAGE_LIMIT
        )

        def comparison_location(
            selected_filter: str,
            selected_page: int = 1,
        ) -> str:
            query = urlencode(
                {
                    "base_generation_id": base_generation_id,
                    "candidate_generation_id": candidate_generation_id,
                    "comparison_filter": selected_filter,
                    "comparison_page": selected_page,
                }
            )
            return (
                f"/jobs/{quote(job.id, safe='')}/translation-comparison?{query}"
            )

        filter_counts = {
            "changes": int(comparison["change_count"]),
            "all": int(comparison["total_count"]),
            "changed": int(comparison["changed_count"]),
            "added": int(comparison["added_count"]),
            "removed": int(comparison["removed_count"]),
            "source_changed": int(comparison["source_changed_count"]),
            "unchanged": int(comparison["total_count"])
            - int(comparison["change_count"]),
        }
        return TEMPLATES.TemplateResponse(
            request,
            "translation_comparison.html",
            {
                "job": job,
                "generations": generations,
                "base_generation": base,
                "candidate_generation": candidate,
                "comparison": comparison,
                "comparison_rows": filtered_rows[
                    offset : offset + TRANSLATION_COMPARISON_PAGE_LIMIT
                ],
                "comparison_filter": comparison_filter,
                "comparison_filters": [
                    {
                        "key": key,
                        "label": label,
                        "count": filter_counts[key],
                        "url": comparison_location(key),
                    }
                    for key, label in TRANSLATION_COMPARISON_FILTERS
                ],
                "comparison_page": comparison_page,
                "comparison_page_count": page_count,
                "comparison_previous_url": (
                    comparison_location(
                        comparison_filter,
                        comparison_page - 1,
                    )
                    if comparison_page > 1
                    else None
                ),
                "comparison_next_url": (
                    comparison_location(
                        comparison_filter,
                        comparison_page + 1,
                    )
                    if comparison_page < page_count
                    else None
                ),
            },
        )

    @app.get(
        "/jobs/{job_id}/translation-generations/{generation_id}",
        name="download_translation_generation",
    )
    def download_translation_generation(
        request: Request,
        job_id: str,
        generation_id: str,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        service = orchestrator(request)
        generation = service.store.get_translation_generation(generation_id)
        if generation is None or generation["job_id"] != job_id:
            raise HTTPException(status_code=404, detail="generation not found")
        job_root = (service.settings.jobs_dir / job_id).resolve()
        artifact = Path(str(generation["artifact_path"])).resolve()
        try:
            artifact.relative_to(job_root)
        except ValueError as error:
            raise HTTPException(
                status_code=404,
                detail="generation not found",
            ) from error
        if not artifact.is_file():
            raise HTTPException(status_code=404, detail="generation not found")
        return FileResponse(
            artifact,
            media_type="application/json",
            filename=(
                "translation-generation-"
                f"{generation['generation_number']}.json"
            ),
        )

    @app.get(
        "/jobs/{job_id}/subtitle-generations/"
        "{generation_id}.{subtitle_format}",
        name="download_subtitle_generation",
    )
    def download_subtitle_generation(
        request: Request,
        job_id: str,
        generation_id: str,
        subtitle_format: str,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        service = orchestrator(request)
        generation = service.store.get_subtitle_generation(generation_id)
        if generation is None or generation["job_id"] != job_id:
            raise HTTPException(status_code=404, detail="generation not found")
        details = {
            "srt": (
                generation["srt_artifact_path"],
                "application/x-subrip",
            ),
            "ass": (generation["ass_artifact_path"], "text/x-ssa"),
        }
        if subtitle_format not in details:
            raise HTTPException(status_code=404, detail="generation not found")
        path_value, media_type = details[subtitle_format]
        job_root = (service.settings.jobs_dir / job_id).resolve()
        artifact = Path(str(path_value)).resolve()
        try:
            artifact.relative_to(job_root)
        except ValueError as error:
            raise HTTPException(
                status_code=404,
                detail="generation not found",
            ) from error
        if not artifact.is_file():
            raise HTTPException(status_code=404, detail="generation not found")
        return FileResponse(
            artifact,
            media_type=media_type,
            filename=(
                f"subtitle-generation-{generation['generation_number']}."
                f"{subtitle_format}"
            ),
        )

    @app.post(
        "/jobs/{job_id}/subtitle-generations/{generation_id}/publish",
        name="publish_subtitle_generation",
    )
    def publish_subtitle_generation(
        request: Request,
        job_id: str,
        generation_id: str,
        csrf_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).publish_subtitle_generation(
                job_id,
                generation_id,
            )
        except (OSError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            f"/jobs/{job_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get(
        "/jobs/{job_id}/artifacts/{kind}/edit",
        response_class=HTMLResponse,
        name="edit_artifact",
    )
    def edit_artifact(
        request: Request,
        job_id: str,
        kind: str,
        saved: int = 0,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        artifact_path, filename, label = artifact_details(job, kind)
        if not artifact_path or not Path(artifact_path).is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        try:
            content = Path(artifact_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise HTTPException(
                status_code=500,
                detail="artifact could not be read",
            ) from error
        return TEMPLATES.TemplateResponse(
            request,
            "artifact_editor.html",
            {
                "job": job,
                "kind": kind,
                "filename": filename,
                "label": label,
                "content": content,
                "csrf_token": request.session.get("csrf_token", ""),
                "error": None,
                "saved": bool(saved),
            },
        )

    @app.post(
        "/jobs/{job_id}/artifacts/{kind}/edit",
        response_class=HTMLResponse,
    )
    def save_artifact(
        request: Request,
        job_id: str,
        kind: str,
        content: str = Form(...),
        csrf_token: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        _artifact_path, filename, label = artifact_details(job, kind)
        try:
            service.save_artifact(job_id, kind, content)
        except (OSError, UnicodeError, ValueError) as error:
            return TEMPLATES.TemplateResponse(
                request,
                "artifact_editor.html",
                {
                    "job": service.store.get(job_id) or job,
                    "kind": kind,
                    "filename": filename,
                    "label": label,
                    "content": content,
                    "csrf_token": request.session.get("csrf_token", ""),
                    "error": str(error),
                    "saved": False,
                },
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return RedirectResponse(
            f"/jobs/{job_id}/artifacts/{kind}/edit?saved=1",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.get("/jobs/{job_id}/artifacts/{kind}")
    def download_artifact(request: Request, job_id: str, kind: str) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        job = orchestrator(request).store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        artifact_path, filename, _label = artifact_details(job, kind)
        if not artifact_path or not Path(artifact_path).is_file():
            raise HTTPException(status_code=404, detail="artifact not found")
        return FileResponse(
            artifact_path,
            media_type="application/json",
            filename=filename,
        )

    @app.get("/api/jobs")
    def list_jobs(request: Request) -> list[dict[str, Any]]:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")
        jobs: list[dict[str, Any]] = []
        for job in orchestrator(request).store.list_jobs():
            payload = asdict(job)
            payload["created_at"] = format_kst_iso(job.created_at)
            payload["updated_at"] = format_kst_iso(job.updated_at)
            jobs.append(payload)
        return jobs

    return app


app = create_app()


def main(argv: Sequence[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(
        description="Run the stt-to-subtitle web orchestrator.",
    )
    parser.parse_args(argv)
    configure_kst_logging(
        os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
    uvicorn.run(
        "stt_to_subtitle.web_app:app",
        host=os.environ.get("WEB_HOST", "0.0.0.0"),
        port=int(os.environ.get("WEB_PORT", "8080")),
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get(
            "WEB_FORWARDED_ALLOW_IPS", "127.0.0.1"
        ),
    )


if __name__ == "__main__":
    main()
