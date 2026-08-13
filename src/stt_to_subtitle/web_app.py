"""Container-friendly web UI for the subtitle pipeline."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
from typing import Any, AsyncIterator
from urllib.parse import quote, urlencode

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

from .artifacts import artifact_filename
from .contracts import validate_transcript, validate_translation_items
from .media_preview import (
    guess_media_type,
    iter_file_range,
    parse_byte_range,
    srt_to_webvtt,
)
from .web_config import (
    WebSettings,
    RemoteServerSettings,
    normalize_server_url,
)
from .orchestrator import SubtitleOrchestrator
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

LOGGER = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent
RECENT_JOB_LIMIT = 20
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
    "blocked": "확인 필요",
    "failed": "실패",
}
JOB_OPERATION_LABELS = {
    "extract": "오디오 추출 (기존 작업)",
    "transcribe": "전사",
    "translate": "번역",
    "full": "전체",
}
JOB_STAGE_LABELS = {
    "audio extraction": "오디오 추출",
    "transcription": "전사",
    "translation": "번역",
    "render": "자막 생성",
}
EVENT_LEVEL_LABELS = {
    "info": "정보",
    "warning": "주의",
    "error": "오류",
}
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
TEMPLATES.env.filters["job_stage"] = lambda value: JOB_STAGE_LABELS.get(
    str(value),
    str(value),
)
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
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.authentication_enabled = authentication_enabled

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
        return f"/?{urlencode(query)}" if query else "/"

    def job_action_location(
        job_id: str,
        return_folder: str | None,
        **values: object,
    ) -> str:
        if return_folder is None:
            return f"/jobs/{job_id}"
        return dashboard_location(return_folder, **values)

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
        folder: str = "",
        jobs_page: int = 1,
    ) -> dict[str, Any]:
        service = orchestrator(request)
        browser = service.library.browse(folder)
        latest_jobs = service.store.latest_jobs_by_source()
        completed_subtitles = service.store.latest_completed_subtitle_jobs()
        for media in browser["files"]:
            source_rel = str(media["path"])
            latest = latest_jobs.get(source_rel)
            linked_job = None
            if latest is not None and latest.status in {"blocked", "failed"}:
                media["subtitle_state"] = "attention"
                linked_job = latest
            elif latest is not None and latest.status not in {
                "audio_completed",
                "transcription_completed",
                "completed",
            }:
                media["subtitle_state"] = "running"
                linked_job = latest
            elif media["has_subtitle"]:
                media["subtitle_state"] = "completed"
                linked_job = completed_subtitles.get(source_rel)
            else:
                media["subtitle_state"] = "pending"
            media["job_id"] = linked_job.id if linked_job else None
            media["selectable"] = media["subtitle_state"] == "pending"

        jobs_page = max(1, jobs_page)
        job_count = service.store.count_jobs()
        jobs_offset = (jobs_page - 1) * RECENT_JOB_LIMIT
        open_jobs = service.store.list_open_jobs()
        running_count = sum(
            job.status
            in {"extracting", "transcription_running", "translation_running", "rendering"}
            for job in open_jobs
        )
        attention_count = sum(job.status in {"blocked", "failed"} for job in open_jobs)
        waiting_count = len(open_jobs) - running_count - attention_count
        completed_count = service.store.count_successful_jobs()
        return {
            "request": request,
            "recent_jobs": service.store.list_jobs(
                limit=RECENT_JOB_LIMIT,
                offset=jobs_offset,
            ),
            "stoppable_job_count": sum(job.can_stop for job in open_jobs),
            "retriable_job_count": sum(job.can_retry for job in open_jobs),
            "pausable_translation_count": sum(
                job.can_pause_translation for job in open_jobs
            ),
            "job_stats": {
                "running": running_count,
                "attention": attention_count,
                "waiting": waiting_count,
                "completed": completed_count,
            },
            "jobs_page": jobs_page,
            "jobs_has_previous": jobs_page > 1,
            "jobs_has_next": jobs_offset + RECENT_JOB_LIMIT < job_count,
            "job_count": job_count,
            "csrf_token": request.session.get("csrf_token", ""),
            "error": error,
            "notice": notice,
            "remote_servers": service.remote_servers_view(),
            "prompt_categories": service.active_prompt_categories(),
            **browser,
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
        skipped: int | None = None,
        folder: str = "",
        jobs_page: int = 1,
        completed_page: int | None = None,
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
            notice = f"중단·실패 작업 {jobs_retried}개를 재시도했습니다."
        try:
            context = dashboard_context(
                request,
                notice=notice,
                folder=folder,
                jobs_page=(
                    completed_page
                    if completed_page is not None and jobs_page == 1
                    else jobs_page
                ),
            )
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
    ) -> Any:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")
        service = orchestrator(request)
        if completed_page is not None and jobs_page == 1:
            jobs_page = completed_page
        jobs_page = max(1, jobs_page)
        job_count = service.store.count_jobs()
        jobs_offset = (jobs_page - 1) * RECENT_JOB_LIMIT
        open_jobs = service.store.list_open_jobs()
        return TEMPLATES.TemplateResponse(
            request,
            "_jobs_table.html",
            {
                "recent_jobs": service.store.list_jobs(
                    limit=RECENT_JOB_LIMIT,
                    offset=jobs_offset,
                ),
                "stoppable_job_count": sum(job.can_stop for job in open_jobs),
                "retriable_job_count": sum(job.can_retry for job in open_jobs),
                "pausable_translation_count": sum(
                    job.can_pause_translation for job in open_jobs
                ),
                "jobs_page": jobs_page,
                "jobs_has_previous": jobs_page > 1,
                "jobs_has_next": (
                    jobs_offset + RECENT_JOB_LIMIT < job_count
                ),
                "job_count": job_count,
                "prompt_categories": service.active_prompt_categories(),
                "current_folder": folder,
                "csrf_token": request.session.get("csrf_token", ""),
            },
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
        folder_rels: list[str] | None = Form(None),
        return_folder: str = Form(""),
        csrf_token: str = Form(""),
        force_overwrite: bool = Form(False),
        backend: str = Form("kotoba"),
        audio_stream: str = Form("0"),
        start_seconds: str = Form("0"),
        duration_seconds: str = Form(""),
        chunk_length_seconds: str = Form("60"),
        hybrid_kotoba_chunk_length_seconds: str = Form("15"),
        hybrid_whisperx_chunk_length_seconds: str = Form("30"),
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
        options = {
            "backend": backend,
            "audio_stream": audio_stream,
            "start_seconds": start_seconds,
            "duration_seconds": duration_seconds,
            "chunk_length_seconds": chunk_length_seconds,
            "num_speakers": num_speakers,
            "min_speakers": min_speakers,
            "max_speakers": max_speakers,
            "add_punctuation": add_punctuation,
            "noise_filter": noise_filter[-1] if noise_filter else True,
        }
        if backend.strip().lower() == "hybrid":
            options["hybrid_rescue"] = {
                "kotoba_chunk_length_seconds": (
                    hybrid_kotoba_chunk_length_seconds
                ),
                "whisperx_chunk_length_seconds": (
                    hybrid_whisperx_chunk_length_seconds
                ),
            }
        try:
            service = orchestrator(request)
            if (
                operation in {"translate", "full"}
                and not prompt_category_id.strip()
            ):
                raise ValueError("번역 프롬프트 카테고리를 선택하세요.")
            selected_sources, skipped = service.expand_job_sources(
                source_rels or [],
                folder_rels or [],
                force_overwrite=force_overwrite,
                operation=operation,
            )
            jobs = service.create_jobs(
                selected_sources,
                force_overwrite=force_overwrite,
                options=options,
                operation=operation,
                prompt_category_id=(
                    prompt_category_id
                    if operation in {"translate", "full"}
                    else None
                ),
            )
        except (FileExistsError, OSError, ValueError) as error:
            try:
                context = dashboard_context(
                    request,
                    error=str(error),
                    folder=return_folder,
                )
            except ValueError:
                context = dashboard_context(request, error=str(error))
            return TEMPLATES.TemplateResponse(
                request,
                "dashboard.html",
                context,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        query = {"queued": len(jobs)}
        if skipped:
            query["skipped"] = skipped
        if return_folder:
            query["folder"] = return_folder
        return RedirectResponse(
            f"/?{urlencode(query)}",
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

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return TEMPLATES.TemplateResponse(
            request,
            "job.html",
            {
                "job": job,
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
    def job_panel(request: Request, job_id: str) -> Any:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")
        service = orchestrator(request)
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return TEMPLATES.TemplateResponse(
            request,
            "_job_panel.html",
            {
                "job": job,
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
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).delete_job_record(job_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return RedirectResponse(
            dashboard_location(return_folder or ""),
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

    @app.post("/jobs/pause-all-translations")
    def pause_all_translations(
        request: Request,
        csrf_token: str = Form(""),
        return_folder: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        paused_count = orchestrator(request).pause_all_translations()
        return RedirectResponse(
            dashboard_location(
                return_folder,
                translations_paused=paused_count,
            ),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/stop-all")
    def stop_all_jobs(
        request: Request,
        csrf_token: str = Form(""),
        return_folder: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        stopped_count = orchestrator(request).stop_all_jobs()
        return RedirectResponse(
            dashboard_location(return_folder, jobs_stopped=stopped_count),
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @app.post("/jobs/retry-all")
    def retry_all_jobs(
        request: Request,
        csrf_token: str = Form(""),
        return_folder: str = Form(""),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        retried_count = orchestrator(request).retry_all_jobs()
        return RedirectResponse(
            dashboard_location(return_folder, jobs_retried=retried_count),
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
