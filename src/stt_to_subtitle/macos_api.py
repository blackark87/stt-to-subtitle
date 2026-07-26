"""macOS-native FastAPI service for MPS transcription."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time
from typing import Any, AsyncIterator, Mapping
from uuid import uuid4
import wave

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request
from fastapi import UploadFile, status
from fastapi.responses import JSONResponse

from .contracts import TRANSCRIPT_SCHEMA_VERSION, add_segment_ids
from .files import write_json_atomic
from .kotoba import (
    ChunkProgress,
    DEFAULT_CHUNK_LENGTH_SECONDS,
    DEFAULT_NOISE_FILTER_TRIGGER_LEVEL,
    MODEL_ID,
    MODEL_REVISION,
    SpeechPipeline,
    TranscriptionOptions,
    load_pipeline,
    normalize_segments,
    run_pipeline,
)
from .transcription_store import TranscriptionJob, TranscriptionStore
from .time_display import configure_kst_logging

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MacOSAPISettings:
    state_dir: Path
    api_token: str
    hf_token: str
    device: str = "mps"
    diarization_device: str = "cpu"
    batch_size: int = 1
    threads: int | None = None
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    progress_interval: float = 30.0
    chunk_progress_every: int = 10
    noise_filter_trigger_level: float = DEFAULT_NOISE_FILTER_TRIGGER_LEVEL

    @classmethod
    def from_env(cls) -> MacOSAPISettings:
        threads_value = os.environ.get("STT_THREADS", "").strip()
        return cls(
            state_dir=Path(
                os.environ.get("STT_STATE_DIR", "./var/macos-stt")
            ).expanduser(),
            api_token=os.environ.get("STT_API_TOKEN", ""),
            hf_token=os.environ.get("HF_TOKEN", ""),
            device=os.environ.get("STT_DEVICE", "mps").strip(),
            diarization_device=os.environ.get(
                "STT_DIARIZATION_DEVICE", "cpu"
            ).strip(),
            batch_size=int(os.environ.get("STT_BATCH_SIZE", "1")),
            threads=int(threads_value) if threads_value else None,
            max_upload_bytes=int(
                os.environ.get(
                    "STT_MAX_UPLOAD_BYTES",
                    str(2 * 1024 * 1024 * 1024),
                )
            ),
            progress_interval=float(
                os.environ.get("STT_PROGRESS_INTERVAL_SECONDS", "30")
            ),
            chunk_progress_every=int(
                os.environ.get("STT_CHUNK_PROGRESS_EVERY", "10")
            ),
            noise_filter_trigger_level=float(
                os.environ.get(
                    "STT_NOISE_FILTER_TRIGGER_LEVEL",
                    str(DEFAULT_NOISE_FILTER_TRIGGER_LEVEL),
                )
            ),
        )

    def validate(self) -> None:
        if not self.hf_token.strip():
            raise ValueError("HF_TOKEN is required")
        if self.batch_size < 1:
            raise ValueError("STT_BATCH_SIZE must be at least 1")
        if self.threads is not None and self.threads < 1:
            raise ValueError("STT_THREADS must be at least 1")
        if self.max_upload_bytes < 1:
            raise ValueError("STT_MAX_UPLOAD_BYTES must be at least 1")
        if self.progress_interval <= 0:
            raise ValueError("STT_PROGRESS_INTERVAL_SECONDS must be positive")
        if self.chunk_progress_every not in {10, 100}:
            raise ValueError("STT_CHUNK_PROGRESS_EVERY must be 10 or 100")
        if self.noise_filter_trigger_level <= 0:
            raise ValueError(
                "STT_NOISE_FILTER_TRIGGER_LEVEL must be positive"
            )


def _parse_options(raw_options: str, settings: MacOSAPISettings) -> dict[str, Any]:
    try:
        decoded = json.loads(raw_options)
    except json.JSONDecodeError as error:
        raise ValueError("options must be valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise ValueError("options must be a JSON object")

    allowed = {
        "chunk_length_seconds",
        "num_speakers",
        "min_speakers",
        "max_speakers",
        "add_punctuation",
        "noise_filter",
    }
    unknown = set(decoded) - allowed
    if unknown:
        raise ValueError(f"unsupported transcription options: {sorted(unknown)}")
    noise_filter = decoded.get("noise_filter", True)
    if not isinstance(noise_filter, bool):
        raise ValueError("noise_filter must be a JSON boolean")
    options = TranscriptionOptions(
        batch_size=settings.batch_size,
        chunk_length_seconds=int(
            decoded.get(
                "chunk_length_seconds",
                DEFAULT_CHUNK_LENGTH_SECONDS,
            )
        ),
        num_speakers=(
            int(decoded["num_speakers"])
            if decoded.get("num_speakers") is not None
            else None
        ),
        min_speakers=(
            int(decoded["min_speakers"])
            if decoded.get("min_speakers") is not None
            else None
        ),
        max_speakers=(
            int(decoded["max_speakers"])
            if decoded.get("max_speakers") is not None
            else None
        ),
        add_punctuation=bool(decoded.get("add_punctuation", False)),
        noise_filter=noise_filter,
        noise_filter_trigger_level=settings.noise_filter_trigger_level,
        threads=settings.threads,
    )
    options.validate()
    return asdict(options)


def _validate_wav(path: Path) -> None:
    try:
        with wave.open(str(path), "rb") as wav_file:
            if wav_file.getnchannels() != 1:
                raise ValueError("uploaded WAV must be mono")
            if wav_file.getframerate() != 16000:
                raise ValueError("uploaded WAV must use a 16000 Hz sample rate")
            if wav_file.getsampwidth() != 2:
                raise ValueError("uploaded WAV must use 16-bit samples")
            if wav_file.getcomptype() != "NONE":
                raise ValueError("uploaded WAV must use uncompressed PCM")
            if wav_file.getnframes() < 1:
                raise ValueError("uploaded WAV is empty")
    except (EOFError, wave.Error) as error:
        raise ValueError("uploaded file is not a valid PCM WAV") from error


class TranscriptionService:
    """Own the persistent queue and the single lazy model instance."""

    def __init__(self, settings: MacOSAPISettings) -> None:
        settings.validate()
        self.settings = settings
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.incoming_dir = self.settings.state_dir / "incoming"
        self.result_dir = self.settings.state_dir / "results"
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        self.store = TranscriptionStore(self.settings.state_dir / "jobs.sqlite3")
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._pipeline: SpeechPipeline | None = None
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="macos-stt-worker",
            daemon=True,
        )
        self._started_at = time.time()

    def start(self) -> None:
        interrupted = self.store.fail_interrupted_jobs()
        if interrupted:
            LOGGER.warning("marked %d interrupted transcription job(s) failed", interrupted)
        self._worker.start()
        for job_id in self.store.queued_ids():
            self._queue.put(job_id)

    def stop(self) -> None:
        self._queue.put(None)
        self._worker.join(timeout=5)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "uptime_seconds": round(time.time() - self._started_at, 3),
            "queued_jobs": self._queue.qsize(),
        }

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        detail: dict[str, Any] = {
            "device": self.settings.device,
            "diarization_device": self.settings.diarization_device,
            "hf_token_configured": bool(self.settings.hf_token.strip()),
        }
        if not detail["hf_token_configured"]:
            detail["status"] = "not_ready"
            detail["reason"] = "HF_TOKEN is not configured"
            return False, detail
        try:
            import torch
        except ImportError:
            detail["status"] = "not_ready"
            detail["reason"] = "PyTorch is not installed"
            return False, detail
        if self.settings.device == "mps" and not torch.backends.mps.is_available():
            detail["status"] = "not_ready"
            detail["reason"] = "PyTorch MPS is not available"
            return False, detail
        detail["status"] = "ready"
        return True, detail

    async def submit(
        self,
        upload: UploadFile,
        idempotency_key: str,
        options: Mapping[str, Any],
    ) -> TranscriptionJob:
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise ValueError("Idempotency-Key must contain 1 to 200 characters")

        existing = self.store.get_by_idempotency_key(key)
        if existing is not None:
            await upload.close()
            if existing.status == "failed":
                if not Path(existing.audio_path).is_file():
                    raise ValueError("saved upload for the failed job is unavailable")
                self.store.requeue(existing.id)
                self._queue.put(existing.id)
                requeued = self.store.get(existing.id)
                if requeued is None:
                    raise RuntimeError("requeued transcription job could not be read")
                return requeued
            return existing

        job_id = uuid4().hex
        audio_path = self.incoming_dir / f"{job_id}.wav"
        digest = hashlib.sha256()
        total_bytes = 0
        try:
            with audio_path.open("xb") as stream:
                while chunk := await upload.read(1024 * 1024):
                    total_bytes += len(chunk)
                    if total_bytes > self.settings.max_upload_bytes:
                        raise ValueError("uploaded WAV exceeds STT_MAX_UPLOAD_BYTES")
                    digest.update(chunk)
                    stream.write(chunk)
            _validate_wav(audio_path)
            job = self.store.create(
                job_id=job_id,
                idempotency_key=key,
                audio_path=audio_path,
                audio_sha256=digest.hexdigest(),
                options=options,
            )
        except BaseException:
            audio_path.unlink(missing_ok=True)
            raise
        finally:
            await upload.close()

        self._queue.put(job.id)
        return job

    def _get_pipeline(self) -> SpeechPipeline:
        if self._pipeline is None:
            LOGGER.info(
                "loading %s on %s with Pyannote on %s",
                MODEL_ID,
                self.settings.device,
                self.settings.diarization_device,
            )
            self._pipeline = load_pipeline(
                self.settings.hf_token,
                batch_size=self.settings.batch_size,
                device=self.settings.device,
                diarization_device=self.settings.diarization_device,
                threads=self.settings.threads,
            )
            LOGGER.info("transcription model loaded")
        return self._pipeline

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                if job_id is None:
                    return
                self._run_job(job_id)
            finally:
                self._queue.task_done()

    def _run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or job.status != "queued":
            return
        self.store.update(job_id, status="running")
        started = time.monotonic()
        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._log_heartbeat,
            args=(job.id, started, heartbeat_stop),
            name=f"stt-heartbeat-{job.id[:8]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            options = TranscriptionOptions(**job.options)
            raw_result = run_pipeline(
                self._get_pipeline(),
                Path(job.audio_path),
                options,
                progress_callback=lambda progress: self._record_chunk_progress(
                    job.id,
                    progress,
                ),
                progress_every=self.settings.chunk_progress_every,
            )
            segments = add_segment_ids(normalize_segments(raw_result))
            noise_filter = raw_result.get(
                "noise_filter",
                {
                    "enabled": options.noise_filter,
                    "trigger_level": options.noise_filter_trigger_level,
                    "removed_count": 0,
                    "removed_spans": [],
                },
            )
            payload = {
                "schema_version": TRANSCRIPT_SCHEMA_VERSION,
                "job_id": job.id,
                "audio_sha256": job.audio_sha256,
                "model": {
                    "id": MODEL_ID,
                    "revision": MODEL_REVISION,
                },
                "timing": {
                    "postprocessor": raw_result.get(
                        "timestamp_postprocessor",
                        "model-default",
                    ),
                },
                "runtime": {
                    "device": self.settings.device,
                    "diarization_device": self.settings.diarization_device,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                },
                "options": job.options,
                "noise_filter": noise_filter,
                "segments": segments,
            }
            result_path = self.result_dir / f"{job.id}.json"
            write_json_atomic(result_path, payload)
            self.store.update(
                job.id,
                status="completed",
                result_path=result_path,
            )
            LOGGER.info(
                "transcription job %s completed with %d segments",
                job.id,
                len(segments),
            )
            if (
                isinstance(noise_filter, Mapping)
                and int(noise_filter.get("removed_count", 0)) > 0
            ):
                LOGGER.info(
                    "transcription job %s noise filter removed %d "
                    "non-speech span(s)",
                    job.id,
                    int(noise_filter["removed_count"]),
                )
        except BaseException as error:
            message = str(error).replace(self.settings.hf_token, "[redacted]")
            self.store.update(
                job.id,
                status="failed",
                error=message[:2000] or error.__class__.__name__,
            )
            LOGGER.exception("transcription job %s failed", job.id)
        finally:
            heartbeat_stop.set()
            heartbeat.join()

    def _record_chunk_progress(
        self,
        job_id: str,
        progress: ChunkProgress,
    ) -> None:
        previous = self.store.get(job_id)
        self.store.update_chunk_progress(
            job_id,
            created=progress.created,
            completed=progress.completed,
        )
        previous_created = previous.chunks_created if previous else 0
        previous_completed = previous.chunks_completed if previous else 0
        report_every = self.settings.chunk_progress_every
        should_log = (
            (previous_created == 0 and progress.created > 0)
            or progress.completed // report_every
            > previous_completed // report_every
            or progress.final
        )
        if not should_log:
            return
        LOGGER.info(
            "transcription job %s chunks: created %d, completed %d, "
            "in progress %d (reporting every %d chunks)",
            job_id,
            progress.created,
            progress.completed,
            progress.in_progress,
            report_every,
        )

    def _log_heartbeat(
        self,
        job_id: str,
        started: float,
        stop_event: threading.Event,
    ) -> None:
        while not stop_event.wait(self.settings.progress_interval):
            job = self.store.get(job_id)
            progress = (
                ChunkProgress(
                    created=job.chunks_created,
                    completed=job.chunks_completed,
                )
                if job is not None
                else ChunkProgress(created=0, completed=0)
            )
            LOGGER.info(
                "transcription job %s is still running (elapsed %.0fs; "
                "chunks created %d, completed %d, in progress %d)",
                job_id,
                time.monotonic() - started,
                progress.created,
                progress.completed,
                progress.in_progress,
            )


def create_app(
    settings: MacOSAPISettings | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = TranscriptionService(settings or MacOSAPISettings.from_env())
        app.state.transcription_service = service
        service.start()
        try:
            yield
        finally:
            service.stop()

    app = FastAPI(
        title="stt-to-subtitle macOS transcription API",
        version="1.0.0",
        lifespan=lifespan,
    )

    def get_service(request: Request) -> TranscriptionService:
        return request.app.state.transcription_service

    def require_bearer(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> None:
        service = get_service(request)
        if not service.settings.api_token.strip():
            return
        prefix = "Bearer "
        provided = (
            authorization[len(prefix) :]
            if authorization and authorization.startswith(prefix)
            else ""
        )
        if not hmac.compare_digest(provided, service.settings.api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @app.get("/healthz")
    def healthz(
        service: TranscriptionService = Depends(get_service),
    ) -> dict[str, Any]:
        return service.health()

    @app.get("/readyz")
    def readyz(
        service: TranscriptionService = Depends(get_service),
    ) -> JSONResponse:
        ready, detail = service.readiness()
        return JSONResponse(
            detail,
            status_code=200 if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.post(
        "/v1/transcriptions",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_bearer)],
    )
    async def submit_transcription(
        request: Request,
        audio: UploadFile = File(...),
        options: str = Form("{}"),
        idempotency_key: str | None = Header(
            default=None,
            alias="Idempotency-Key",
        ),
        service: TranscriptionService = Depends(get_service),
    ) -> dict[str, Any]:
        ready, detail = service.readiness()
        if not ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=detail["reason"],
            )
        if idempotency_key is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Idempotency-Key header is required",
            )
        try:
            parsed_options = _parse_options(options, service.settings)
            job = await service.submit(audio, idempotency_key, parsed_options)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(error),
            ) from error
        return {
            **job.public_dict(
                report_every=service.settings.chunk_progress_every,
            ),
            "status_url": str(request.url_for("get_transcription", job_id=job.id)),
            "result_url": str(
                request.url_for("get_transcription_result", job_id=job.id)
            ),
        }

    @app.get(
        "/v1/transcriptions/{job_id}",
        dependencies=[Depends(require_bearer)],
        name="get_transcription",
    )
    def get_transcription(
        job_id: str,
        service: TranscriptionService = Depends(get_service),
    ) -> dict[str, Any]:
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job.public_dict(
            report_every=service.settings.chunk_progress_every,
        )

    @app.get(
        "/v1/transcriptions/{job_id}/result",
        dependencies=[Depends(require_bearer)],
        name="get_transcription_result",
    )
    def get_transcription_result(
        job_id: str,
        service: TranscriptionService = Depends(get_service),
    ) -> JSONResponse:
        job = service.store.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.status != "completed" or job.result_path is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"job is {job.status}",
            )
        try:
            payload = json.loads(Path(job.result_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="saved transcription result is unavailable",
            ) from error
        return JSONResponse(payload)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    configure_kst_logging(
        os.environ.get("LOG_LEVEL", "INFO").upper(),
    )
    uvicorn.run(
        "stt_to_subtitle.macos_api:app",
        host=os.environ.get("STT_HOST", "0.0.0.0"),
        port=int(os.environ.get("STT_PORT", "8100")),
        workers=1,
    )


if __name__ == "__main__":
    main()
