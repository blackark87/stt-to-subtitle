"""Native FastAPI transcription service for MPS, CUDA, or CPU."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
import gc
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
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
    validate_device,
)
from .transcription_store import TranscriptionJob, TranscriptionStore
from .time_display import configure_kst_logging
from .whisperx_worker import (
    DEFAULT_WHISPERX_COMPUTE_TYPE,
    DEFAULT_WHISPERX_LANGUAGE,
    DEFAULT_WHISPERX_MODEL,
)

LOGGER = logging.getLogger(__name__)
STT_BACKENDS = {"kotoba", "whisperx"}


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
    whisperx_python: Path = Path(".venv-whisperx/bin/python")
    whisperx_model: str = DEFAULT_WHISPERX_MODEL
    whisperx_language: str = DEFAULT_WHISPERX_LANGUAGE
    whisperx_compute_type: str = DEFAULT_WHISPERX_COMPUTE_TYPE
    whisperx_cache_dir: Path = Path("./var/cuda-cache/whisperx")

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
            whisperx_python=Path(
                os.environ.get(
                    "WHISPERX_PYTHON",
                    ".venv-whisperx/bin/python",
                )
            ).expanduser(),
            whisperx_model=os.environ.get(
                "WHISPERX_MODEL",
                DEFAULT_WHISPERX_MODEL,
            ).strip(),
            whisperx_language=os.environ.get(
                "WHISPERX_LANGUAGE",
                DEFAULT_WHISPERX_LANGUAGE,
            ).strip(),
            whisperx_compute_type=os.environ.get(
                "WHISPERX_COMPUTE_TYPE",
                DEFAULT_WHISPERX_COMPUTE_TYPE,
            ).strip(),
            whisperx_cache_dir=Path(
                os.environ.get(
                    "WHISPERX_CACHE_DIR",
                    "./var/cuda-cache/whisperx",
                )
            ).expanduser(),
        )

    def validate(self) -> None:
        if not self.hf_token.strip():
            raise ValueError("HF_TOKEN is required")
        validate_device(self.device, setting="STT_DEVICE")
        validate_device(
            self.diarization_device,
            setting="STT_DIARIZATION_DEVICE",
        )
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
        if not self.whisperx_model:
            raise ValueError("WHISPERX_MODEL must not be empty")
        if not self.whisperx_language:
            raise ValueError("WHISPERX_LANGUAGE must not be empty")
        if not self.whisperx_compute_type:
            raise ValueError("WHISPERX_COMPUTE_TYPE must not be empty")


def _device_unavailable_reason(torch: Any, device: str) -> str | None:
    if device == "cpu":
        return None
    if device == "mps":
        backends = getattr(torch, "backends", None)
        mps = getattr(backends, "mps", None)
        if mps is None or not mps.is_available():
            return "PyTorch MPS is not available"
        return None

    cuda = getattr(torch, "cuda", None)
    if cuda is None or not cuda.is_available():
        return "PyTorch CUDA is not available"
    if ":" not in device:
        return None
    index = int(device.split(":", maxsplit=1)[1])
    if index >= cuda.device_count():
        return (
            f"CUDA device {device} is not available; "
            f"found {cuda.device_count()} CUDA device(s)"
        )
    return None


def _whisperx_unavailable_reason(settings: MacOSAPISettings) -> str | None:
    if settings.device == "mps" or settings.diarization_device == "mps":
        return "WhisperX backend supports only cpu or CUDA devices"
    if not settings.whisperx_python.is_file():
        return (
            "WhisperX Python was not found: "
            f"{settings.whisperx_python}"
        )
    return None


def _parse_options(raw_options: str, settings: MacOSAPISettings) -> dict[str, Any]:
    try:
        decoded = json.loads(raw_options)
    except json.JSONDecodeError as error:
        raise ValueError("options must be valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise ValueError("options must be a JSON object")

    allowed = {
        "backend",
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
    backend = str(decoded.get("backend", "kotoba")).strip().lower()
    if backend not in STT_BACKENDS:
        raise ValueError(
            "backend must be either 'kotoba' or 'whisperx'"
        )
    noise_filter = decoded.get("noise_filter", True)
    if not isinstance(noise_filter, bool):
        raise ValueError("noise_filter must be a JSON boolean")
    if backend == "whisperx" and not noise_filter:
        raise ValueError("WhisperX backend requires noise_filter=true for VAD")
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
    return {"backend": backend, **asdict(options)}


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
            "loaded_backend": "kotoba" if self._pipeline is not None else None,
        }

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        detail: dict[str, Any] = {
            "device": self.settings.device,
            "diarization_device": self.settings.diarization_device,
            "hf_token_configured": bool(self.settings.hf_token.strip()),
            "backends": {
                "kotoba": {"status": "ready"},
                "whisperx": {"status": "ready"},
            },
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
        reason = _device_unavailable_reason(torch, self.settings.device)
        if reason is not None:
            detail["status"] = "not_ready"
            detail["reason"] = reason
            return False, detail
        reason = _device_unavailable_reason(
            torch,
            self.settings.diarization_device,
        )
        if reason is not None:
            detail["status"] = "not_ready"
            detail["reason"] = (
                f"{reason} for STT_DIARIZATION_DEVICE"
            )
            return False, detail
        whisperx_reason = _whisperx_unavailable_reason(self.settings)
        if whisperx_reason is not None:
            detail["backends"]["whisperx"] = {
                "status": "unavailable",
                "reason": whisperx_reason,
            }
        detail["status"] = "ready"
        return True, detail

    def backend_unavailable_reason(self, backend: str) -> str | None:
        if backend == "kotoba":
            return None
        if backend == "whisperx":
            return _whisperx_unavailable_reason(self.settings)
        return f"unsupported transcription backend: {backend}"

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

    def _release_pipeline(self) -> None:
        if self._pipeline is None:
            return
        LOGGER.info("unloading Kotoba transcription model before backend switch")
        self._pipeline = None
        gc.collect()
        try:
            import torch
        except ImportError:
            return
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            ipc_collect = getattr(torch.cuda, "ipc_collect", None)
            if callable(ipc_collect):
                ipc_collect()

    def _run_whisperx_worker(
        self,
        job: TranscriptionJob,
    ) -> Mapping[str, Any]:
        reason = self.backend_unavailable_reason("whisperx")
        if reason is not None:
            raise RuntimeError(reason)
        self._release_pipeline()
        worker_result = self.result_dir / f".{job.id}.whisperx.json"
        worker_result.unlink(missing_ok=True)
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[1])
        existing_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            source_root
            if not existing_pythonpath
            else f"{source_root}{os.pathsep}{existing_pythonpath}"
        )
        environment.update(
            {
                "HF_TOKEN": self.settings.hf_token,
                "STT_DEVICE": self.settings.device,
                "STT_DIARIZATION_DEVICE": self.settings.diarization_device,
                "WHISPERX_MODEL": self.settings.whisperx_model,
                "WHISPERX_LANGUAGE": self.settings.whisperx_language,
                "WHISPERX_COMPUTE_TYPE": self.settings.whisperx_compute_type,
                "WHISPERX_CACHE_DIR": str(self.settings.whisperx_cache_dir),
            }
        )
        command = [
            str(self.settings.whisperx_python),
            "-m",
            "stt_to_subtitle.whisperx_worker",
            "--audio",
            job.audio_path,
            "--output",
            str(worker_result),
            "--options",
            json.dumps(job.options, sort_keys=True),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise RuntimeError(
                    "WhisperX worker failed"
                    + (f": {detail[-2000:]}" if detail else "")
                )
            try:
                payload = json.loads(worker_result.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    "WhisperX worker returned an invalid result"
                ) from error
            if not isinstance(payload, Mapping):
                raise RuntimeError("WhisperX worker result must be an object")
            return payload
        finally:
            worker_result.unlink(missing_ok=True)

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
            backend = str(job.options.get("backend", "kotoba"))
            option_values = {
                key: value
                for key, value in job.options.items()
                if key != "backend"
            }
            options = TranscriptionOptions(**option_values)
            if backend == "whisperx":
                backend_result = self._run_whisperx_worker(job)
                raw_segments = backend_result.get("segments")
                if not isinstance(raw_segments, list):
                    raise RuntimeError(
                        "WhisperX worker result has no segments list"
                    )
                segments = add_segment_ids(raw_segments)
                model = backend_result.get("model")
                timing = backend_result.get("timing")
                runtime = backend_result.get("runtime")
                noise_filter = backend_result.get("noise_filter")
                if not isinstance(model, Mapping):
                    raise RuntimeError("WhisperX worker result has no model")
                if not isinstance(timing, Mapping):
                    raise RuntimeError("WhisperX worker result has no timing")
                if not isinstance(runtime, Mapping):
                    raise RuntimeError("WhisperX worker result has no runtime")
                if not isinstance(noise_filter, Mapping):
                    raise RuntimeError(
                        "WhisperX worker result has no noise_filter"
                    )
                runtime = {
                    **runtime,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            elif backend == "kotoba":
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
                model = {"id": MODEL_ID, "revision": MODEL_REVISION}
                timing = {
                    "postprocessor": raw_result.get(
                        "timestamp_postprocessor",
                        "model-default",
                    ),
                }
                runtime = {
                    "backend": "kotoba",
                    "device": self.settings.device,
                    "diarization_device": self.settings.diarization_device,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
                noise_filter = raw_result.get(
                    "noise_filter",
                    {
                        "enabled": options.noise_filter,
                        "trigger_level": options.noise_filter_trigger_level,
                        "removed_count": 0,
                        "removed_spans": [],
                    },
                )
            else:
                raise RuntimeError(
                    f"unsupported transcription backend: {backend}"
                )
            payload = {
                "schema_version": TRANSCRIPT_SCHEMA_VERSION,
                "job_id": job.id,
                "audio_sha256": job.audio_sha256,
                "model": model,
                "timing": timing,
                "runtime": runtime,
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
                "transcription job %s completed with %d segments using %s",
                job.id,
                len(segments),
                backend,
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
        title="stt-to-subtitle native transcription API",
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
            backend_reason = service.backend_unavailable_reason(
                str(parsed_options["backend"])
            )
            if backend_reason is not None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=backend_reason,
                )
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
