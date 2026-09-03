"""API-only control plane for the static Nginx frontend."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import __version__
from .backend_jobs_api import router as jobs_router
from .backend_media_api import router as media_router
from .backend_settings_api import router as settings_router
from .gpu_monitoring import PrometheusGpuMonitor
from .orchestrator import SubtitleOrchestrator
from .time_display import configure_kst_logging
from .backend_config import BackendSettings


def create_backend_app(settings: BackendSettings | None = None) -> FastAPI:
    """Create the backend without importing the legacy Jinja2 application."""
    configured_settings = settings or BackendSettings.from_env()
    gpu_monitor = PrometheusGpuMonitor(
        configured_settings.gpu_prometheus_url,
        bearer_token=configured_settings.gpu_prometheus_token,
        timeout_seconds=configured_settings.gpu_metrics_timeout_seconds,
        cache_seconds=configured_settings.gpu_metrics_refresh_seconds,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = SubtitleOrchestrator(configured_settings)
        app.state.orchestrator = service
        service.start()
        try:
            yield
        finally:
            service.stop()

    app = FastAPI(
        title="stt-to-subtitle backend API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.gpu_monitor = gpu_monitor
    app.include_router(jobs_router)
    app.include_router(media_router)
    app.include_router(settings_router)

    def orchestrator(request: Request) -> SubtitleOrchestrator:
        return request.app.state.orchestrator

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request) -> Response:
        service = orchestrator(request)
        checks = {
            "media_root": service.library.root.is_dir(),
            "state_store": service.settings.state_dir.is_dir(),
            "work_directory": service.settings.jobs_dir.is_dir(),
            "transcription_audio_directory": (
                service.settings.transcription_audio_dir.is_dir()
            ),
        }
        ready = all(checks.values())
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "checks": checks},
            status_code=200 if ready else 503,
        )

    return app


def main(argv: Sequence[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(
        description="Run the stt-to-subtitle backend API.",
    )
    parser.parse_args(argv)
    configure_kst_logging(
        os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
    uvicorn.run(
        "stt_to_subtitle.backend_api:create_backend_app",
        host=os.environ.get("BACKEND_HOST", "0.0.0.0"),
        port=int(os.environ.get("BACKEND_PORT", "8080")),
        factory=True,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get(
            "BACKEND_FORWARDED_ALLOW_IPS",
            "127.0.0.1",
        ),
    )


if __name__ == "__main__":
    main()
