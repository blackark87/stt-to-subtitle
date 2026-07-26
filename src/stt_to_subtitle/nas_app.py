"""NAS web UI with optional authentication for the subtitle pipeline."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
import hmac
import logging
import mimetypes
import os
from pathlib import Path
import secrets
from typing import Any, AsyncIterator
from urllib.parse import quote

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

from .media_preview import iter_file_range, parse_byte_range, srt_to_webvtt
from .nas_config import NASSettings
from .orchestrator import NASOrchestrator

LOGGER = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=PACKAGE_DIR / "templates")
TEMPLATES.env.filters["datetime"] = lambda value: datetime.fromtimestamp(
    float(value)
).strftime("%Y-%m-%d %H:%M:%S")
TEMPLATES.env.filters["filesize"] = lambda value: (
    f"{float(value) / 1024 / 1024:.1f} MiB"
)


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

    def dashboard_context(
        request: Request,
        *,
        error: str | None = None,
        notice: str | None = None,
    ) -> dict[str, Any]:
        service = orchestrator(request)
        return {
            "request": request,
            "files": service.library.list_files(),
            "jobs": service.store.list_jobs(),
            "csrf_token": request.session.get("csrf_token", ""),
            "error": error,
            "notice": notice,
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
    def dashboard(request: Request, queued: int | None = None) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        notice = (
            f"작업 {queued}개를 등록했습니다."
            if queued is not None and queued > 1
            else None
        )
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            dashboard_context(request, notice=notice),
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
            {"jobs": orchestrator(request).store.list_jobs()},
        )

    @app.post("/jobs", response_class=HTMLResponse)
    def create_job(
        request: Request,
        source_rels: list[str] | None = Form(None),
        csrf_token: str = Form(""),
        force_overwrite: bool = Form(False),
        audio_stream: str = Form("0"),
        start_seconds: str = Form("0"),
        duration_seconds: str = Form(""),
        chunk_length_seconds: str = Form("15"),
        num_speakers: str = Form(""),
        min_speakers: str = Form(""),
        max_speakers: str = Form(""),
        add_punctuation: bool = Form(False),
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
        }
        try:
            jobs = orchestrator(request).create_jobs(
                source_rels or [],
                force_overwrite=force_overwrite,
                options=options,
            )
        except (FileExistsError, OSError, ValueError) as error:
            return TEMPLATES.TemplateResponse(
                request,
                "dashboard.html",
                dashboard_context(request, error=str(error)),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        if len(jobs) > 1:
            return RedirectResponse(
                f"/?queued={len(jobs)}",
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
        media_type = (
            mimetypes.guess_type(source.name)[0]
            or "application/octet-stream"
        )
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
            source = service.library.resolve_file(job.source_rel)
            subtitle = source.with_name(f"{source.stem}.ko.srt")
            srt_text = subtitle.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError, ValueError) as error:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="subtitle not found",
            ) from error
        return Response(
            srt_to_webvtt(srt_text),
            media_type="text/vtt",
            headers={"Cache-Control": "private, no-cache"},
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

    @app.get("/jobs/{job_id}/artifacts/{kind}")
    def download_artifact(request: Request, job_id: str, kind: str) -> Any:
        if not is_authenticated(request):
            return login_redirect()
        job = orchestrator(request).store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        artifacts = {
            "transcript": (job.transcript_path, "transcript.json"),
            "translation": (job.translation_path, "translation.json"),
        }
        if kind not in artifacts:
            raise HTTPException(status_code=404, detail="artifact not found")
        artifact_path, filename = artifacts[kind]
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
        return [
            asdict(job)
            for job in orchestrator(request).store.list_jobs()
        ]

    return app


app = create_app()


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        "stt_to_subtitle.nas_app:app",
        host=os.environ.get("NAS_HOST", "0.0.0.0"),
        port=int(os.environ.get("NAS_PORT", "8080")),
        workers=1,
    )


if __name__ == "__main__":
    main()
