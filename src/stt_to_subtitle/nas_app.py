"""NAS web UI with optional authentication for the subtitle pipeline."""

from __future__ import annotations

from collections.abc import Mapping
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
from .nas_config import NASSettings
from .orchestrator import NASOrchestrator
from .subtitle import render_webvtt
from .time_display import (
    configure_kst_logging,
    format_kst_iso,
    format_kst_timestamp,
)

LOGGER = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=PACKAGE_DIR / "templates")
TEMPLATES.env.filters["datetime"] = format_kst_timestamp
TEMPLATES.env.filters["filesize"] = lambda value: (
    f"{float(value) / 1024 / 1024 / 1024:.2f} GiB"
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


def create_app(settings: NASSettings | None = None) -> FastAPI:
    configured_settings = settings or NASSettings.from_env()
    authentication_enabled = bool(configured_settings.admin_password.strip())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        orchestrator = NASOrchestrator(configured_settings)
        app.state.orchestrator = orchestrator
        orchestrator.start()
        try:
            yield
        finally:
            orchestrator.stop()

    app = FastAPI(
        title="stt-to-subtitle NAS orchestrator",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.authentication_enabled = authentication_enabled
    app.add_middleware(
        SessionMiddleware,
        secret_key=configured_settings.session_secret
        or secrets.token_urlsafe(48),
        session_cookie="stt_nas_session",
        same_site="strict",
        https_only=configured_settings.secure_cookie,
        max_age=12 * 60 * 60,
    )
    app.mount(
        "/static",
        StaticFiles(directory=PACKAGE_DIR / "static"),
        name="static",
    )

    def orchestrator(request: Request) -> NASOrchestrator:
        return request.app.state.orchestrator

    def is_authenticated(request: Request) -> bool:
        return (
            not authentication_enabled
            or request.session.get("authenticated") is True
        )

    def login_redirect() -> RedirectResponse:
        return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)

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
    ) -> dict[str, Any]:
        service = orchestrator(request)
        browser = service.library.browse(folder)
        return {
            "request": request,
            "jobs": service.store.list_jobs(),
            "csrf_token": request.session.get("csrf_token", ""),
            "error": error,
            "notice": notice,
            **browser,
        }

    @app.get("/healthz")
    def healthz(request: Request) -> dict[str, Any]:
        service = orchestrator(request)
        return {
            "status": "ok",
            "media_root_available": service.library.root.is_dir(),
            "authentication_enabled": authentication_enabled,
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
        folder: str = "",
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        notice = (
            f"작업 {queued}개를 등록했습니다."
            if queued is not None and queued > 1
            else None
        )
        try:
            context = dashboard_context(
                request,
                notice=notice,
                folder=folder,
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
    def jobs_fragment(request: Request) -> Any:
        if not is_authenticated(request):
            raise HTTPException(status_code=401, detail="authentication required")
        return TEMPLATES.TemplateResponse(
            request,
            "_jobs_table.html",
            {
                "jobs": orchestrator(request).store.list_jobs(),
                "csrf_token": request.session.get("csrf_token", ""),
            },
        )

    @app.post("/jobs", response_class=HTMLResponse)
    def create_job(
        request: Request,
        source_rels: list[str] | None = Form(None),
        return_folder: str = Form(""),
        csrf_token: str = Form(""),
        force_overwrite: bool = Form(False),
        audio_stream: str = Form("0"),
        start_seconds: str = Form("0"),
        duration_seconds: str = Form(""),
        chunk_length_seconds: str = Form("60"),
        num_speakers: str = Form(""),
        min_speakers: str = Form(""),
        max_speakers: str = Form(""),
        add_punctuation: bool = Form(False),
        noise_filter: list[bool] | None = Form(None),
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        options = {
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
        try:
            jobs = orchestrator(request).create_jobs(
                source_rels or [],
                force_overwrite=force_overwrite,
                options=options,
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
        if len(jobs) > 1:
            query = {"queued": len(jobs)}
            if return_folder:
                query["folder"] = return_folder
            return RedirectResponse(
                f"/?{urlencode(query)}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        return RedirectResponse(
            f"/jobs/{jobs[0].id}",
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
                "events": service.store.events(job_id),
                "csrf_token": request.session.get("csrf_token", ""),
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
                "events": service.store.events(job_id),
                "csrf_token": request.session.get("csrf_token", ""),
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
    ) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        validate_csrf(request, csrf_token)
        try:
            orchestrator(request).restart_translation(job_id)
        except (OSError, UnicodeError, ValueError) as error:
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


def main() -> None:
    import uvicorn

    configure_kst_logging(
        os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
    uvicorn.run(
        "stt_to_subtitle.nas_app:app",
        host=os.environ.get("NAS_HOST", "0.0.0.0"),
        port=int(os.environ.get("NAS_PORT", "8080")),
        workers=1,
    )


if __name__ == "__main__":
    main()
