"""Container-friendly web UI for the subtitle pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
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
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .artifacts import artifact_filename
from .contracts import validate_transcript, validate_translation_items
from .gpu_monitoring import PrometheusGpuMonitor
from .job_store import RETRYABLE_STATUSES, RUNNING_STATUSES, SUCCESS_STATUSES
from .media_preview import (
    guess_media_type,
    iter_file_range,
    parse_byte_range,
    srt_to_webvtt,
)
from .web_config import (
    group_multipart_media,
    WebSettings,
    RemoteServerSettings,
    normalize_server_url,
)
from .orchestrator import (
    COMPARISON_AUDIO_SOURCE_JOB_ID_OPTION,
    COMPARISON_PARENT_ID_OPTION,
    SubtitleOrchestrator,
    TRANSCRIPTION_COMPARISON_BACKENDS,
)
from .service_clients import (
    ExternalServiceError,
    list_openai_compatible_models,
)
from .subtitle import render_webvtt
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
WAITING_STATUSES = {
    "queued",
    "audio_ready",
    "transcribed",
    "translation_paused",
    "translated",
}
JOB_STATUS_GROUPS = {
    "running": RUNNING_STATUSES,
    "blocked": {"blocked"},
    "failed": {"failed"},
    "waiting": WAITING_STATUSES,
    "completed": SUCCESS_STATUSES,
}
JOB_STATUS_GROUP_LABELS = {
    "running": "진행 중",
    "blocked": "중단",
    "failed": "실패",
    "waiting": "대기",
    "completed": "완료",
}
JOB_STAGE_FILTERS = {
    "extraction": {"queued", "extracting", "audio_completed"},
    "transcription": {
        "audio_ready",
        "transcription_running",
        "transcription_completed",
    },
    "transcription_waiting": {"audio_ready"},
    "transcription_running": {"transcription_running"},
    "transcription_completed": {"transcription_completed"},
    "translation": {
        "transcribed",
        "translation_running",
        "translation_paused",
        "translated",
    },
    "translation_waiting": {"transcribed"},
    "translation_running": {"translation_running"},
    "translation_completed": {"translated"},
    "completed": {"rendering", "completed"},
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
    "translation_paused": "번역 중단됨",
    "translated": "자막 생성 대기",
    "rendering": "자막 생성 중",
    "completed": "완료",
    "blocked": "중단",
    "failed": "실패",
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
    "translation_paused": "번역 중단됨",
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
    "extract": "오디오 추출 (기존 작업)",
    "transcribe": "전사",
    "translate": "번역",
    "full": "전체",
}
JOB_STAGE_LABELS = {
    "audio extraction": "추출",
    "transcription": "전사",
    "translation": "번역",
    "render": "완료",
}
STAGE_SEQUENCE = (
    "audio extraction",
    "transcription",
    "translation",
    "render",
)
OPERATION_STAGES = {
    "extract": ("audio extraction", "render"),
    "transcribe": STAGE_SEQUENCE,
    "translate": STAGE_SEQUENCE,
    "full": STAGE_SEQUENCE,
}
STATUS_ACTIVE_STAGE = {
    "extracting": ("audio extraction", "running"),
    "audio_ready": ("transcription", "waiting"),
    "transcription_running": ("transcription", "running"),
    "transcription_completed": ("translation", "waiting"),
    "transcribed": ("translation", "waiting"),
    "translation_running": ("translation", "running"),
    "translation_paused": ("translation", "paused"),
    "translated": ("render", "waiting"),
    "rendering": ("render", "running"),
}
STAGE_FINISHED_STATUSES = {
    "audio_completed",
    "completed",
}
STAGE_STATE_LABELS = {
    "done": "완료",
    "running": "진행 중",
    "waiting": "대기",
    "paused": "중단됨",
    "blocked": "중단",
    "failed": "실패",
    "pending": "대기",
}


def job_stage_view(job: Any) -> list[dict[str, Any]]:
    """작업이 거치는 모든 파이프라인 단계와 각 단계의 상태를 돌려준다."""
    stages = OPERATION_STAGES.get(str(job.operation), STAGE_SEQUENCE)
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
    status = str(job.status)
    if status in STAGE_FINISHED_STATUSES:
        active: str | None = None
        active_state = "done"
    elif status in {"blocked", "failed"}:
        blocked = str(job.blocked_stage or "")
        active = blocked if blocked in stages else stages[0]
        active_state = "blocked" if status == "blocked" else "failed"
    elif status == "queued":
        active = (
            "translation" if str(job.operation) == "translate" else stages[0]
        )
        active_state = "waiting"
    else:
        active, active_state = STATUS_ACTIVE_STAGE.get(
            status,
            (stages[0], "waiting"),
        )
        if active not in stages:
            active = stages[0]

    view: list[dict[str, Any]] = []
    reached_active = False
    for stage in stages:
        if active is None:
            state = "done"
        elif stage == active:
            state = active_state
            reached_active = True
        elif reached_active:
            state = "pending"
        else:
            state = "done"
        completed, total = chunk_counts.get(stage, (0, 0))
        percent = (
            round(completed * 100 / total)
            if total
            else (100 if state == "done" else 0)
        )
        if state != "done":
            percent = min(99, percent)
        view.append(
            {
                "key": stage,
                "label": JOB_STAGE_LABELS.get(stage, stage),
                "state": state,
                "state_label": STAGE_STATE_LABELS[state],
                "completed": completed,
                "total": total,
                "total_is_estimate": (
                    stage == "transcription"
                    and chunks_total_estimate > chunks_created
                ),
                "percent": percent,
            }
        )
    return view


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
            in {"running", "blocked", "failed", "paused", "waiting"}
        ),
        stages[-1] if stages else None,
    )
    return {
        "stages": stages,
        "percent": percent,
        "current": current,
        "complete": bool(stages) and all(
            stage["state"] == "done" for stage in stages
        ),
    }


def comparison_audio_stage(jobs: Sequence[Any]) -> dict[str, str]:
    """여러 비교 작업의 오디오 준비 상태를 하나의 단계로 집계한다."""
    audio_stages = [
        stage
        for job in jobs
        for stage in job_stage_view(job)
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


TEMPLATES = Jinja2Templates(directory=PACKAGE_DIR / "templates")
TEMPLATES.env.filters["datetime"] = format_kst_timestamp
TEMPLATES.env.filters["datetime_iso"] = format_kst_iso
TEMPLATES.env.filters["filesize"] = lambda value: (
    f"{float(value) / 1024 / 1024 / 1024:.2f} GiB"
)
TEMPLATES.env.filters["job_status"] = lambda value: JOB_STATUS_LABELS.get(
    str(value),
    str(value),
)
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
TEMPLATES.env.filters["filename"] = lambda value: Path(str(value)).name
TEMPLATES.env.filters["parent_path"] = lambda value: (
    "" if str(Path(str(value)).parent) == "." else str(Path(str(value)).parent)
)


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
        return_status_group: str,
        return_jobs_page: int,
        return_stage_filter: str = "",
        **values: object,
    ) -> str:
        if return_status_group and return_stage_filter:
            raise ValueError(
                "작업 상태와 단계 필터를 동시에 사용할 수 없습니다."
            )
        if (
            return_status_group
            and return_status_group not in JOB_STATUS_GROUPS
        ):
            raise ValueError("지원하지 않는 작업 상태 필터입니다.")
        if return_stage_filter and return_stage_filter not in JOB_STAGE_FILTERS:
            raise ValueError("지원하지 않는 작업 단계 필터입니다.")
        if return_stage_filter:
            query = {
                "stage_filter": return_stage_filter,
                "jobs_page": max(1, return_jobs_page),
                **values,
            }
            return f"/jobs?{urlencode(query)}"
        if return_status_group:
            query = {
                "status_group": return_status_group,
                "jobs_page": max(1, return_jobs_page),
                **values,
            }
            return f"/jobs?{urlencode(query)}"
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
                statuses=statuses,
                include_comparison_transcriptions=False,
            )
            for group, statuses in JOB_STATUS_GROUPS.items()
        }

    def validate_job_list_filters(
        *,
        status_group: str | None,
        stage_filter: str | None,
    ) -> None:
        if status_group is not None and status_group not in JOB_STATUS_GROUPS:
            raise ValueError("지원하지 않는 작업 상태 필터입니다.")
        if stage_filter is not None and stage_filter not in JOB_STAGE_FILTERS:
            raise ValueError("지원하지 않는 작업 단계 필터입니다.")
        if status_group is not None and stage_filter is not None:
            raise ValueError("작업 상태와 단계 필터를 동시에 사용할 수 없습니다.")

    def job_stage_filter_context(
        service: SubtitleOrchestrator,
        *,
        status_group: str | None,
        stage_filter: str | None,
    ) -> dict[str, Any]:
        validate_job_list_filters(
            status_group=status_group,
            stage_filter=stage_filter,
        )
        return {
            "job_stage_filters": JOB_STAGE_FILTER_NAV,
            "job_stage_counts": {
                stage["key"]: service.store.count_jobs(
                    statuses=JOB_STAGE_FILTERS[stage["key"]],
                    include_comparison_transcriptions=False,
                )
                for stage in JOB_STAGE_FILTER_NAV
            },
            "selected_status_group": status_group,
            "selected_stage_filter": stage_filter,
            "selected_stage_group": (
                stage_filter.split("_", 1)[0] if stage_filter else None
            ),
        }

    def job_list_context(
        service: SubtitleOrchestrator,
        *,
        jobs_page: int,
        status_group: str | None = None,
        stage_filter: str | None = None,
        folder: str = "",
        limit: int = RECENT_JOB_LIMIT,
        paginated: bool = True,
    ) -> dict[str, Any]:
        validate_job_list_filters(
            status_group=status_group,
            stage_filter=stage_filter,
        )
        if stage_filter is not None:
            statuses = JOB_STAGE_FILTERS[stage_filter]
        elif status_group is not None:
            statuses = JOB_STATUS_GROUPS[status_group]
        else:
            statuses = None
        jobs_page = max(1, jobs_page) if paginated else 1
        job_count = service.store.count_jobs(
            statuses=statuses,
            include_comparison_transcriptions=False,
        )
        jobs_offset = (jobs_page - 1) * limit
        all_visible_jobs = service.store.list_jobs(
            limit=None,
            include_comparison_transcriptions=False,
        )
        open_jobs = [
            job
            for job in all_visible_jobs
            if job.status not in SUCCESS_STATUSES
        ]
        recent_jobs = service.store.list_jobs(
            limit=limit,
            offset=jobs_offset,
            statuses=statuses,
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
                for job in all_visible_jobs
                if job.can_stop
                and (statuses is None or job.status in statuses)
            }
            if paginated
            else set()
        )
        retriable_job_ids = (
            {
                job.id
                for job in all_visible_jobs
                if job.can_retry
                and (statuses is None or job.status in statuses)
            }
            if paginated
            else set()
        )

        def page_location(page: int) -> str:
            if folder:
                return media_location(folder, jobs_page=page)
            query = {"jobs_page": page}
            if stage_filter is not None:
                query = {"stage_filter": stage_filter, **query}
            elif status_group is not None:
                query = {"status_group": status_group, **query}
            return "/jobs?" + urlencode(query)

        label = (
            JOB_STAGE_FILTER_LABELS[stage_filter]
            if stage_filter is not None
            else (
                JOB_STATUS_GROUP_LABELS[status_group]
                if status_group is not None
                else "전체"
            )
        )
        filtered = status_group is not None or stage_filter is not None
        return {
            "recent_jobs": recent_jobs,
            "translatable_job_ids": translatable_job_ids,
            "translatable_job_count": len(translatable_job_ids),
            "stoppable_job_ids": stoppable_job_ids,
            "stoppable_selection_count": len(stoppable_job_ids),
            "retriable_job_ids": retriable_job_ids,
            "retriable_selection_count": len(retriable_job_ids),
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
                f"{label} 단계의 작업이 없습니다."
                if filtered
                else "등록된 작업이 없습니다."
            ),
            "show_bulk_actions": paginated,
            "selected_status_group": status_group,
            "selected_stage_filter": stage_filter,
            "selected_stage_group": (
                stage_filter.split("_", 1)[0] if stage_filter else None
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

    def styled_webvtt(job: Any) -> str | None:
        if not job.transcript_path or not job.translation_path:
            return None
        transcript_path = Path(job.transcript_path)
        translation_path = Path(job.translation_path)
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
            **job_list_context(
                service,
                jobs_page=1,
                limit=DASHBOARD_JOB_LIMIT,
                paginated=False,
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
            "prompt_categories": service.active_prompt_categories(),
        }

    def media_context(
        request: Request,
        *,
        error: str | None = None,
        notice: str | None = None,
        folder: str = "",
        query: str = "",
    ) -> dict[str, Any]:
        service = orchestrator(request)
        normalized_query = query.strip()
        browser = (
            service.library.search_by_title(normalized_query, folder)
            if normalized_query
            else service.library.browse(folder)
        )
        latest_jobs = service.store.latest_jobs_by_source()
        completed_subtitles = service.store.latest_completed_subtitle_jobs()
        for media in browser["files"]:
            source_rel = str(media["path"])
            latest = latest_jobs.get(source_rel)
            linked_job = None
            if latest is not None and latest.status in {"blocked", "failed"}:
                media["subtitle_state"] = latest.status
                stage = JOB_STAGE_LABELS.get(
                    str(latest.blocked_stage),
                    str(latest.blocked_stage or ""),
                )
                media["processing_label"] = JOB_STATUS_LABELS[latest.status]
                if stage:
                    media["processing_label"] += f" · {stage}"
                linked_job = latest
            elif latest is not None and latest.status not in {
                "audio_completed",
                "transcription_completed",
                "completed",
            }:
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
            media["selectable"] = latest is None or latest.status in {
                "audio_completed",
                "transcription_completed",
                "completed",
            }
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
            "prompt_categories": service.active_prompt_categories(),
            "search_query": normalized_query,
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
        terminal_statuses = SUCCESS_STATUSES | RETRYABLE_STATUSES
        completed_count = sum(
            job.status in SUCCESS_STATUSES for job in comparison_jobs
        )
        attention_count = sum(
            job.status in RETRYABLE_STATUSES for job in comparison_jobs
        )
        blocked_count = sum(
            job.status == "blocked" for job in comparison_jobs
        )
        failed_count = sum(
            job.status == "failed" for job in comparison_jobs
        )
        active_count = sum(
            job.status in RUNNING_STATUSES for job in comparison_jobs
        )
        terminal_count = sum(
            job.status in terminal_statuses for job in comparison_jobs
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
            "prompt_categories": service.active_prompt_categories(),
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
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        if folder:
            return RedirectResponse(
                media_location(folder),
                status_code=status.HTTP_303_SEE_OTHER,
            )
        notice = None
        if queued is not None and queued > 0:
            notice = f"작업 {queued}개를 등록했습니다."
            if skipped:
                notice += f" 기존 작업·자막 {skipped}개는 제외했습니다."
        elif translation_pause_requested is not None:
            notice = "번역 중단 요청을 반영했습니다."
        elif translations_paused is not None:
            notice = f"번역 작업 {translations_paused}개에 중단을 요청했습니다."
        elif jobs_stopped is not None:
            notice = f"진행 중인 작업 {jobs_stopped}개에 중단을 요청했습니다."
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
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        notice = None
        if queued is not None and queued > 0:
            notice = f"작업 {queued}개를 등록했습니다."
            if skipped:
                notice += f" 기존 작업·자막 {skipped}개는 제외했습니다."
        elif translation_pause_requested is not None:
            notice = "번역 중단 요청을 반영했습니다."
        elif translations_paused is not None:
            notice = f"번역 작업 {translations_paused}개에 중단을 요청했습니다."
        elif jobs_stopped is not None:
            notice = f"진행 중인 작업 {jobs_stopped}개에 중단을 요청했습니다."
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
            context = {
                "request": request,
                **job_list_context(
                    orchestrator(request),
                    jobs_page=jobs_page,
                    status_group=status_group or None,
                    stage_filter=stage_filter or None,
                ),
                **job_stage_filter_context(
                    orchestrator(request),
                    status_group=status_group or None,
                    stage_filter=stage_filter or None,
                ),
                "status_groups": JOB_STATUS_GROUP_LABELS,
                "csrf_token": request.session.get("csrf_token", ""),
                "prompt_categories": orchestrator(
                    request
                ).active_prompt_categories(),
                "notice": (
                    f"선택한 전사 작업 {translations_queued}개를 번역으로 "
                    "전환했습니다."
                    if translations_queued is not None
                    else (
                        f"번역 작업 {translations_paused}개에 중단을 "
                        "요청했습니다."
                        if translations_paused is not None
                        else (
                            f"진행 중인 작업 {jobs_stopped}개에 중단을 "
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
        status_group: str = "",
        stage_filter: str = "",
    ) -> Any:
        if not is_authenticated(request):
            raise HTTPException(
                status_code=401,
                detail="authentication required",
            )
        try:
            context = job_stage_filter_context(
                orchestrator(request),
                status_group=status_group or None,
                stage_filter=stage_filter or None,
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
            "prompt_categories": service.all_prompt_categories(),
        }

    @app.get("/settings", response_class=HTMLResponse)
    def server_settings_page(
        request: Request,
        saved: bool = False,
        prompt_saved: bool = False,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        return TEMPLATES.TemplateResponse(
            request,
            "settings.html",
            settings_context(
                request,
                notice=(
                    "서버 설정을 저장했습니다."
                    if saved
                    else "번역 프롬프트 설정을 저장했습니다."
                    if prompt_saved
                    else None
                ),
            ),
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
            models = list_openai_compatible_models(base_url, token)
        except (ValueError, ExternalServiceError) as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
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

    @app.get("/jobs-fragment", response_class=HTMLResponse)
    def jobs_fragment(
        request: Request,
        jobs_page: int = 1,
        completed_page: int | None = None,
        folder: str = "",
        status_group: str | None = None,
        stage_filter: str | None = None,
        compact: bool = False,
    ) -> Any:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")
        if completed_page is not None and jobs_page == 1:
            jobs_page = completed_page
        service = orchestrator(request)
        try:
            context = job_list_context(
                service,
                jobs_page=1 if compact else jobs_page,
                status_group=status_group or None,
                stage_filter=stage_filter or None,
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
                "prompt_categories": service.active_prompt_categories(),
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
            {"job_stats": job_stats(orchestrator(request))},
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

    @app.post("/jobs", response_class=HTMLResponse)
    def create_job(
        request: Request,
        source_rels: list[str] | None = Form(None),
        source_groups: list[str] | None = Form(None),
        folder_rels: list[str] | None = Form(None),
        return_folder: str = Form(""),
        return_query: str = Form(""),
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
        if job is None or not job.srt_path:
            raise HTTPException(status_code=404, detail="subtitle not found")
        try:
            webvtt = styled_webvtt(job)
            if webvtt is None:
                source = service.library.resolve_file(job.source_rel)
                subtitle = source.with_name(f"{source.stem}.ko.srt")
                webvtt = srt_to_webvtt(
                    subtitle.read_text(encoding="utf-8-sig")
                )
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

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(
        request: Request,
        job_id: str,
        return_status_group: str = "",
        return_stage_filter: str = "",
        return_jobs_page: int = 1,
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        try:
            job_return_url = job_list_action_location(
                return_folder="",
                return_status_group=return_status_group,
                return_stage_filter=return_stage_filter,
                return_jobs_page=return_jobs_page,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return TEMPLATES.TemplateResponse(
            request,
            "job.html",
            {
                "job": job,
                "job_return_url": job_return_url,
                "return_status_group": return_status_group,
                "return_stage_filter": return_stage_filter,
                "return_jobs_page": max(1, return_jobs_page),
                "events": [
                    event
                    for event in service.store.events(job_id)
                    if not event["message"].startswith(
                        ("transcription chunks:", "translation checkpoint saved")
                    )
                ],
                "csrf_token": request.session.get("csrf_token", ""),
                "prompt_categories": service.active_prompt_categories(),
                "video_mime_type": guess_media_type(job.source_rel),
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
        return_status_group: str = "",
        return_stage_filter: str = "",
        return_jobs_page: int = 1,
    ) -> Any:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")
        try:
            job_list_action_location(
                return_folder="",
                return_status_group=return_status_group,
                return_stage_filter=return_stage_filter,
                return_jobs_page=return_jobs_page,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return TEMPLATES.TemplateResponse(
            request,
            "_job_panel.html",
            {
                "job": job,
                "return_status_group": return_status_group,
                "return_stage_filter": return_stage_filter,
                "return_jobs_page": max(1, return_jobs_page),
                "events": [
                    event
                    for event in service.store.events(job_id)
                    if not event["message"].startswith(
                        ("transcription chunks:", "translation checkpoint saved")
                    )
                ],
                "csrf_token": request.session.get("csrf_token", ""),
                "prompt_categories": service.active_prompt_categories(),
                "video_mime_type": guess_media_type(job.source_rel),
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
        return_status_group: str = Form(""),
        return_jobs_page: int = Form(1),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        if (
            return_status_group
            and return_status_group not in JOB_STATUS_GROUPS
        ):
            raise HTTPException(
                status_code=400,
                detail="지원하지 않는 작업 상태 필터입니다.",
            )
        service = orchestrator(request)
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
                    status_group=(
                        return_status_group
                        if return_status_group in JOB_STATUS_GROUPS
                        else None
                    ),
                ),
                **job_stage_filter_context(
                    service,
                    status_group=(
                        return_status_group
                        if return_status_group in JOB_STATUS_GROUPS
                        else None
                    ),
                    stage_filter=None,
                ),
                "status_groups": JOB_STATUS_GROUP_LABELS,
                "csrf_token": request.session.get("csrf_token", ""),
                "prompt_categories": service.active_prompt_categories(),
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
        return_status_group: str = Form(""),
        return_stage_filter: str = Form(""),
        return_jobs_page: int = Form(1),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        redirect_page = (
            1
            if return_status_group or return_stage_filter
            else return_jobs_page
        )
        try:
            return_location = job_list_action_location(
                return_folder=return_folder,
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
                return_status_group=return_status_group,
                return_stage_filter=return_stage_filter,
                return_jobs_page=(
                    1
                    if return_status_group or return_stage_filter
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
        return_status_group: str = Form(""),
        return_stage_filter: str = Form(""),
        return_jobs_page: int = Form(1),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        redirect_page = (
            1
            if return_status_group or return_stage_filter
            else return_jobs_page
        )
        try:
            return_location = job_list_action_location(
                return_folder=return_folder,
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


def main() -> None:
    import uvicorn

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
