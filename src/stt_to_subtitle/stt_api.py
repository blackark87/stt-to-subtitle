"""Native FastAPI transcription service for MPS, CUDA, or CPU."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
import gc
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import platform
import queue
import subprocess
import tempfile
import threading
import time
from typing import Any, AsyncIterator, Mapping, Sequence
from uuid import uuid4
import wave

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request
from fastapi import UploadFile, status
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .contracts import TRANSCRIPT_SCHEMA_VERSION, add_segment_ids
from .files import write_json_atomic
from .hybrid_stt import (
    HYBRID_POLICY_VERSION,
    HybridRescueOptions,
    debounce_word_speakers,
    detect_hybrid_issues,
    fuse_hybrid_segments,
    map_fallback_speakers,
    mark_rescued_words,
    merge_issue_windows,
)
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
from .stt_quality import (
    NORMALIZATION_VERSION,
    overlap_duplicate_metrics,
    short_span_diagnostics,
)
from .stt_trace import TRACE_SCHEMA_VERSION
from .whisperx_worker import (
    DEFAULT_SUBTITLE_SEGMENTATION,
    DEFAULT_WHISPERX_COMPUTE_TYPE,
    DEFAULT_WHISPERX_LANGUAGE,
    DEFAULT_WHISPERX_MODEL,
    WHISPERX_MAX_BATCH_SIZE,
    WHISPERX_MAX_CHUNK_LENGTH_SECONDS,
    WHISPERX_MIN_BATCH_SIZE,
    WhisperXSegmentationOptions,
    rebuild_whisperx_segments,
)
from .whisperjav_worker import WhisperJAVOptions

LOGGER = logging.getLogger(__name__)
STT_BACKENDS = {"hybrid", "kotoba", "whisperjav", "whisperx"}


class TranscriptionChangeHook:
    """Bridge transcription-store changes from worker threads to SSE clients."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._versions: dict[str, int] = {}
        self._changed: dict[str, asyncio.Event] = {}

    def version(self, job_id: str) -> int:
        return self._versions.get(job_id, 0)

    def publish(self, job_id: str) -> None:
        try:
            self._loop.call_soon_threadsafe(self._mark_changed, job_id)
        except RuntimeError:
            return

    def _mark_changed(self, job_id: str) -> None:
        self._versions[job_id] = self.version(job_id) + 1
        self._changed.setdefault(job_id, asyncio.Event()).set()

    async def wait(
        self,
        job_id: str,
        version: int,
        timeout: float = 15.0,
    ) -> int:
        while self.version(job_id) == version:
            changed = self._changed.setdefault(job_id, asyncio.Event())
            changed.clear()
            if self.version(job_id) != version:
                break
            try:
                await asyncio.wait_for(changed.wait(), timeout=timeout)
            except TimeoutError:
                break
        return self.version(job_id)


def _env_boolean(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


@dataclass(frozen=True)
class STTAPISettings:
    state_dir: Path
    api_token: str
    hf_token: str
    device: str = "mps"
    diarization_device: str = "cpu"
    batch_size: int = 1
    whisperx_batch_size: int = 8
    threads: int | None = None
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    progress_interval: float = 30.0
    chunk_progress_every: int = 10
    noise_filter_trigger_level: float = DEFAULT_NOISE_FILTER_TRIGGER_LEVEL
    whisperx_python: Path = Path(".venv-whisperx/bin/python")
    whisperjav_python: Path = Path(".venv-whisperjav/bin/python")
    whisperx_model: str = DEFAULT_WHISPERX_MODEL
    whisperx_language: str = DEFAULT_WHISPERX_LANGUAGE
    whisperx_compute_type: str = DEFAULT_WHISPERX_COMPUTE_TYPE
    whisperx_cache_dir: Path = Path("./var/cuda-cache/whisperx")
    debug_artifacts: bool = False
    debug_artifacts_dir: Path | None = None

    @classmethod
    def from_env(cls) -> STTAPISettings:
        threads_value = os.environ.get("STT_THREADS", "").strip()
        return cls(
            state_dir=Path(
                os.environ.get("STT_STATE_DIR", "./var/stt")
            ).expanduser(),
            api_token=os.environ.get("STT_API_TOKEN", ""),
            hf_token=os.environ.get("HF_TOKEN", ""),
            device=os.environ.get("STT_DEVICE", "mps").strip(),
            diarization_device=os.environ.get(
                "STT_DIARIZATION_DEVICE", "cpu"
            ).strip(),
            batch_size=int(os.environ.get("STT_BATCH_SIZE", "1")),
            whisperx_batch_size=int(
                os.environ.get("WHISPERX_BATCH_SIZE", "8")
            ),
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
            whisperjav_python=Path(
                os.environ.get(
                    "WHISPERJAV_PYTHON",
                    ".venv-whisperjav/bin/python",
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
            debug_artifacts=_env_boolean("STT_DEBUG_ARTIFACTS"),
            debug_artifacts_dir=(
                Path(os.environ["STT_DEBUG_ARTIFACTS_DIR"]).expanduser()
                if os.environ.get("STT_DEBUG_ARTIFACTS_DIR", "").strip()
                else None
            ),
        )

    @property
    def artifacts_dir(self) -> Path:
        return self.debug_artifacts_dir or self.state_dir / "artifacts"

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
        if self.whisperx_batch_size < 1:
            raise ValueError("WHISPERX_BATCH_SIZE must be at least 1")
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


def _whisperx_unavailable_reason(settings: STTAPISettings) -> str | None:
    if settings.device == "mps" or settings.diarization_device == "mps":
        return "WhisperX backend supports only cpu or CUDA devices"
    if not settings.whisperx_python.is_file():
        return (
            "WhisperX Python was not found: "
            f"{settings.whisperx_python}"
        )
    return None


def _whisperjav_unavailable_reason(
    settings: STTAPISettings,
) -> str | None:
    if settings.device == "mps" or settings.diarization_device == "mps":
        return "WhisperJAV backend supports only cpu or CUDA devices"
    if not settings.whisperjav_python.is_file():
        return (
            "WhisperJAV Python was not found: "
            f"{settings.whisperjav_python}"
        )
    whisperx_reason = _whisperx_unavailable_reason(settings)
    if whisperx_reason is not None:
        return (
            "WhisperJAV speaker assignment is unavailable: "
            f"{whisperx_reason}"
        )
    return None


def _venv_nvidia_library_paths(python: Path) -> list[str]:
    return sorted(
        str(path)
        for path in (python.parent.parent / "lib").glob(
            "python*/site-packages/nvidia/*/lib"
        )
        if path.is_dir()
    )


def _resolve_batch_size(
    decoded: Mapping[str, Any],
    backend: str,
    settings: STTAPISettings,
) -> int:
    """Resolve the effective batch size for a transcription request.

    Client overrides are only honoured for the WhisperX-backed paths; the
    Kotoba pipeline caches its batch size at load time and WhisperJAV uses a
    different batching concept entirely.
    """

    if decoded.get("batch_size") is not None:
        if backend not in {"whisperx", "hybrid"}:
            raise ValueError(
                f"{backend} batch_size is fixed at pipeline load; "
                "set STT_BATCH_SIZE instead"
            )
        raw_value = decoded["batch_size"]
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise ValueError("batch_size must be an integer")
        if not (
            WHISPERX_MIN_BATCH_SIZE <= raw_value <= WHISPERX_MAX_BATCH_SIZE
        ):
            raise ValueError(
                "batch_size must be between "
                f"{WHISPERX_MIN_BATCH_SIZE} and {WHISPERX_MAX_BATCH_SIZE}"
            )
        return raw_value
    if backend in {"whisperx", "hybrid"}:
        return settings.whisperx_batch_size
    return settings.batch_size


def _parse_options(raw_options: str, settings: STTAPISettings) -> dict[str, Any]:
    try:
        decoded = json.loads(raw_options)
    except json.JSONDecodeError as error:
        raise ValueError("options must be valid JSON") from error
    if not isinstance(decoded, Mapping):
        raise ValueError("options must be a JSON object")

    allowed = {
        "backend",
        "batch_size",
        "chunk_length_seconds",
        "num_speakers",
        "min_speakers",
        "max_speakers",
        "add_punctuation",
        "noise_filter",
        "short_span_policy",
        "subtitle_segmentation",
        "repetition_policy",
        "repetition_min_count",
        "hybrid_rescue",
        "whisperjav",
    }
    unknown = set(decoded) - allowed
    if unknown:
        raise ValueError(f"unsupported transcription options: {sorted(unknown)}")
    backend = str(decoded.get("backend", "kotoba")).strip().lower()
    if backend not in STT_BACKENDS:
        raise ValueError(
            "backend must be 'kotoba', 'whisperx', 'hybrid', or "
            "'whisperjav'"
        )
    noise_filter = decoded.get("noise_filter", True)
    if not isinstance(noise_filter, bool):
        raise ValueError("noise_filter must be a JSON boolean")
    if backend in {"hybrid", "whisperjav", "whisperx"} and not noise_filter:
        if backend == "whisperx":
            raise ValueError(
                "WhisperX backend requires noise_filter=true for VAD"
            )
        raise ValueError(
            f"{backend} backend requires noise_filter=true for VAD"
        )
    batch_size = _resolve_batch_size(decoded, backend, settings)
    options = TranscriptionOptions(
        batch_size=batch_size,
        chunk_length_seconds=int(
            decoded.get(
                "chunk_length_seconds",
                (
                    WHISPERX_MAX_CHUNK_LENGTH_SECONDS
                    if backend == "whisperx"
                    else DEFAULT_CHUNK_LENGTH_SECONDS
                ),
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
        short_span_policy=str(decoded.get("short_span_policy", "observe")),
        threads=settings.threads,
    )
    options.validate()
    if (
        backend == "whisperx"
        and options.chunk_length_seconds
        > WHISPERX_MAX_CHUNK_LENGTH_SECONDS
    ):
        raise ValueError(
            "WhisperX chunk_length_seconds must be at most "
            f"{WHISPERX_MAX_CHUNK_LENGTH_SECONDS}"
        )
    parsed = {"backend": backend, **asdict(options)}
    if backend in {"hybrid", "whisperx"}:
        if backend == "whisperx" and "hybrid_rescue" in decoded:
            raise ValueError("hybrid_rescue requires backend='hybrid'")
        segmentation = WhisperXSegmentationOptions.from_options(
            decoded,
            defaults=(
                DEFAULT_SUBTITLE_SEGMENTATION
                if backend == "hybrid"
                else None
            ),
        )
        repetition_policy = str(decoded.get("repetition_policy", "flag"))
        if repetition_policy not in {"flag", "reject"}:
            raise ValueError("repetition_policy must be 'flag' or 'reject'")
        if backend == "hybrid" and repetition_policy != "flag":
            raise ValueError(
                "hybrid backend requires repetition_policy='flag' so failed "
                "windows can be rescued"
            )
        repetition_min_count = int(decoded.get("repetition_min_count", 8))
        if repetition_min_count < 2:
            raise ValueError("repetition_min_count must be at least 2")
        parsed.update(
            {
                "subtitle_segmentation": asdict(segmentation),
                "repetition_policy": repetition_policy,
                "repetition_min_count": repetition_min_count,
            }
        )
        if backend == "hybrid":
            hybrid = HybridRescueOptions.from_options(decoded)
            parsed["hybrid_rescue"] = asdict(hybrid)
            parsed["chunk_length_seconds"] = (
                hybrid.kotoba_chunk_length_seconds
            )
    elif backend == "whisperjav":
        forbidden = {
            "repetition_policy",
            "repetition_min_count",
            "hybrid_rescue",
        } & set(decoded)
        if forbidden:
            raise ValueError(
                "unsupported WhisperJAV quality options: "
                f"{sorted(forbidden)}"
            )
        parsed["subtitle_segmentation"] = asdict(
            WhisperXSegmentationOptions.from_options(
                decoded,
                defaults=DEFAULT_SUBTITLE_SEGMENTATION,
            )
        )
        parsed["whisperjav"] = asdict(
            WhisperJAVOptions.from_options(decoded)
        )
    elif any(
        key in decoded
        for key in (
            "subtitle_segmentation",
            "repetition_policy",
            "repetition_min_count",
            "hybrid_rescue",
            "whisperjav",
        )
    ):
        raise ValueError(
            "WhisperX quality options require backend='whisperx' or 'hybrid'"
        )
    return parsed


def write_wav_slice(
    source: Path,
    destination: Path,
    start_seconds: float,
    end_seconds: float,
) -> float:
    """Copy one time span of a PCM WAV file and return its real duration."""
    with wave.open(str(source), "rb") as reader:
        frame_rate = reader.getframerate()
        total_frames = reader.getnframes()
        first = max(0, min(total_frames, int(start_seconds * frame_rate)))
        last = max(first, min(total_frames, int(end_seconds * frame_rate)))
        if last <= first:
            return 0.0
        reader.setpos(first)
        frames = reader.readframes(last - first)
        with wave.open(str(destination), "wb") as writer:
            writer.setnchannels(reader.getnchannels())
            writer.setsampwidth(reader.getsampwidth())
            writer.setframerate(frame_rate)
            writer.writeframes(frames)
    return (last - first) / float(frame_rate)


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


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wav_file:
        return round(wav_file.getnframes() / wav_file.getframerate(), 3)


def _git_commit() -> str:
    return os.environ.get("GIT_COMMIT", "unknown").strip() or "unknown"


def _runtime_trace(
    settings: STTAPISettings,
    *,
    backend: str,
    elapsed_seconds: float | None = None,
    warm_start: bool,
) -> dict[str, Any]:
    return {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "device": settings.device,
        "diarization_device": settings.diarization_device,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "git_commit": _git_commit(),
        "warm_start": warm_start,
        "elapsed_seconds": elapsed_seconds,
    }


class TranscriptionService:
    """Own the persistent queue and the single lazy model instance."""

    def __init__(self, settings: STTAPISettings) -> None:
        settings.validate()
        self.settings = settings
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.incoming_dir = self.settings.state_dir / "incoming"
        self.result_dir = self.settings.state_dir / "results"
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        if self.settings.debug_artifacts:
            self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.store = TranscriptionStore(self.settings.state_dir / "jobs.sqlite3")
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._pipeline: SpeechPipeline | None = None
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="stt-api-worker",
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
                "hybrid": {"status": "ready"},
                "kotoba": {"status": "ready"},
                "whisperjav": {"status": "ready"},
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
            for backend in ("hybrid", "whisperx"):
                detail["backends"][backend] = {
                    "status": "unavailable",
                    "reason": whisperx_reason,
                }
        whisperjav_reason = _whisperjav_unavailable_reason(self.settings)
        if whisperjav_reason is not None:
            detail["backends"]["whisperjav"] = {
                "status": "unavailable",
                "reason": whisperjav_reason,
            }
        detail["status"] = "ready"
        return True, detail

    def backend_unavailable_reason(self, backend: str) -> str | None:
        if backend == "kotoba":
            return None
        if backend in {"hybrid", "whisperx"}:
            return _whisperx_unavailable_reason(self.settings)
        if backend == "whisperjav":
            return _whisperjav_unavailable_reason(self.settings)
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
                self.store.requeue(existing.id, options=options)
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

    def _run_kotoba_rescue_windows(
        self,
        job: TranscriptionJob,
        windows: Sequence[Mapping[str, Any]],
        primary_segments: Sequence[Mapping[str, Any]],
        options: TranscriptionOptions,
        *,
        artifact_dir: Path | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Decode only the rescue windows and restore absolute timestamps.

        Each window is diarized on its own, so speaker labels are local to
        the window and are reconciled at the transcript-normalization
        boundary.
        """
        pipeline = self._get_pipeline()
        source = Path(job.audio_path)
        segments: list[dict[str, Any]] = []
        removed_spans: list[dict[str, Any]] = []
        encoding_warning: Mapping[str, Any] | None = None
        timestamp_postprocessor = "model-default"
        decoded_seconds = 0.0
        executed = 0
        created_base = 0
        completed_base = 0
        with tempfile.TemporaryDirectory(prefix="hybrid-rescue-") as scratch:
            work_dir = Path(scratch)
            for index, window in enumerate(windows):
                start = float(window["start"])
                slice_path = work_dir / f"window-{index:03d}.wav"
                duration = write_wav_slice(
                    source,
                    slice_path,
                    start,
                    float(window["end"]),
                )
                if duration <= 0.0:
                    slice_path.unlink(missing_ok=True)
                    continue

                def report(
                    progress: ChunkProgress,
                    *,
                    created_base: int = created_base,
                    completed_base: int = completed_base,
                ) -> None:
                    self._record_chunk_progress(
                        job.id,
                        ChunkProgress(
                            created=created_base + progress.created,
                            completed=completed_base + progress.completed,
                            final=False,
                        ),
                    )

                window_result = run_pipeline(
                    pipeline,
                    slice_path,
                    options,
                    progress_callback=report,
                    progress_every=self.settings.chunk_progress_every,
                    debug_artifact_dir=(
                        artifact_dir / "kotoba" / f"window-{index:03d}"
                        if artifact_dir is not None
                        else None
                    ),
                )
                slice_path.unlink(missing_ok=True)
                executed += 1
                decoded_seconds += duration
                window_segments = normalize_segments(
                    window_result,
                    offset_seconds=start,
                )
                speaker_mapping = map_fallback_speakers(
                    primary_segments,
                    window_segments,
                )
                window_id = str(
                    window.get("window_id", f"rescue-window-{index + 1:06d}")
                )
                speaker_namespace = window_id.upper().replace("-", "_")
                for segment in window_segments:
                    local_speaker = str(
                        segment.get("speaker", "UNKNOWN")
                    )
                    mapped_speaker = speaker_mapping.get(
                        local_speaker,
                        f"KOTOBA_{local_speaker}",
                    )
                    if mapped_speaker == f"KOTOBA_{local_speaker}":
                        mapped_speaker = (
                            f"KOTOBA_{speaker_namespace}_{local_speaker}"
                        )
                    segment["speaker"] = mapped_speaker
                segments.extend(window_segments)
                chunks = window_result.get("chunks", [])
                if isinstance(chunks, list):
                    created_base += len(chunks)
                    completed_base += len(chunks)
                window_noise = window_result.get("noise_filter")
                if isinstance(window_noise, Mapping):
                    for span in window_noise.get("removed_spans", []) or []:
                        if not isinstance(span, Mapping):
                            continue
                        shifted = dict(span)
                        shifted["start"] = round(
                            float(span.get("start", 0.0)) + start, 3
                        )
                        shifted["end"] = round(
                            float(span.get("end", 0.0)) + start, 3
                        )
                        removed_spans.append(shifted)
                window_encoding = window_result.get("encoding_warning")
                if isinstance(window_encoding, Mapping) and window_encoding:
                    encoding_warning = window_encoding
                timestamp_postprocessor = str(
                    window_result.get(
                        "timestamp_postprocessor",
                        timestamp_postprocessor,
                    )
                )
        segments.sort(key=lambda segment: (segment["start"], segment["end"]))
        LOGGER.info(
            "hybrid job %s rescued %d window(s) covering %.1fs of audio",
            job.id,
            executed,
            decoded_seconds,
        )
        result: dict[str, Any] = {
            "timestamp_postprocessor": timestamp_postprocessor,
            "noise_filter": {
                "enabled": options.noise_filter,
                "provider": "kotoba-noise-filter-v1",
                "execution_state": (
                    "run_removed" if removed_spans else "run_no_removal"
                ),
                "scope": "windows",
                "window_count": executed,
                "decoded_seconds": round(decoded_seconds, 3),
                "removed_count": len(removed_spans),
                "removed_spans": removed_spans,
            },
        }
        if encoding_warning is not None:
            result["encoding_warning"] = dict(encoding_warning)
        return segments, result

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
        *,
        release_kotoba: bool = True,
        options: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        reason = self.backend_unavailable_reason("whisperx")
        if reason is not None:
            raise RuntimeError(reason)
        if release_kotoba:
            self._release_pipeline()
        worker_result = self.result_dir / f".{job.id}.whisperx.json"
        worker_result.unlink(missing_ok=True)
        worker_options = job.options if options is None else options
        LOGGER.info(
            "starting WhisperX worker for %s with batch_size=%s threads=%s",
            job.id,
            worker_options.get("batch_size"),
            worker_options.get("threads"),
        )
        environment = os.environ.copy()
        environment.update(
            {
                "HF_TOKEN": self.settings.hf_token,
                "STT_DEVICE": self.settings.device,
                "STT_DIARIZATION_DEVICE": self.settings.diarization_device,
                "WHISPERX_MODEL": self.settings.whisperx_model,
                "WHISPERX_LANGUAGE": self.settings.whisperx_language,
                "WHISPERX_COMPUTE_TYPE": self.settings.whisperx_compute_type,
                "WHISPERX_CACHE_DIR": str(self.settings.whisperx_cache_dir),
                "PYTHONIOENCODING": "utf-8",
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
            json.dumps(worker_options, sort_keys=True),
        ]
        if self.settings.debug_artifacts:
            command.extend(
                [
                    "--debug-dir",
                    str(self.settings.artifacts_dir / job.id / "whisperx"),
                ]
            )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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

    def _run_whisperjav_worker(
        self,
        job: TranscriptionJob,
    ) -> Mapping[str, Any]:
        reason = self.backend_unavailable_reason("whisperjav")
        if reason is not None:
            raise RuntimeError(reason)
        self._release_pipeline()
        ensemble_result = self.result_dir / f".{job.id}.whisperjav.json"
        speaker_result = self.result_dir / f".{job.id}.speakers.json"
        ensemble_result.unlink(missing_ok=True)
        speaker_result.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "HF_TOKEN": self.settings.hf_token,
                "STT_DEVICE": self.settings.device,
                "STT_DIARIZATION_DEVICE": self.settings.diarization_device,
                "WHISPERX_CACHE_DIR": str(self.settings.whisperx_cache_dir),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        whisperjav_environment = environment.copy()
        nvidia_library_paths = _venv_nvidia_library_paths(
            self.settings.whisperjav_python
        )
        if nvidia_library_paths:
            existing_library_path = environment.get("LD_LIBRARY_PATH", "")
            whisperjav_environment["LD_LIBRARY_PATH"] = ":".join(
                [
                    *nvidia_library_paths,
                    *(
                        [existing_library_path]
                        if existing_library_path
                        else []
                    ),
                ]
            )
        whisperjav_command = [
            str(self.settings.whisperjav_python),
            "-m",
            "stt_to_subtitle.whisperjav_worker",
            "--audio",
            job.audio_path,
            "--output",
            str(ensemble_result),
            "--options",
            json.dumps(job.options, sort_keys=True),
        ]
        speaker_command = [
            str(self.settings.whisperx_python),
            "-m",
            "stt_to_subtitle.speaker_worker",
            "--audio",
            job.audio_path,
            "--input",
            str(ensemble_result),
            "--output",
            str(speaker_result),
            "--options",
            json.dumps(job.options, sort_keys=True),
        ]
        if self.settings.debug_artifacts:
            artifact_dir = self.settings.artifacts_dir / job.id
            whisperjav_command.extend(
                ["--debug-dir", str(artifact_dir / "whisperjav")]
            )
            speaker_command.extend(
                ["--debug-dir", str(artifact_dir / "pyannote")]
            )
        try:
            for label, command, worker_environment in (
                ("WhisperJAV", whisperjav_command, whisperjav_environment),
                (
                    "WhisperJAV speaker assignment",
                    speaker_command,
                    environment,
                ),
            ):
                completed = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=worker_environment,
                )
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout).strip()
                    raise RuntimeError(
                        f"{label} worker failed"
                        + (f": {detail[-2000:]}" if detail else "")
                    )
            try:
                payload = json.loads(speaker_result.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    "WhisperJAV worker returned an invalid result"
                ) from error
            if not isinstance(payload, Mapping):
                raise RuntimeError("WhisperJAV worker result must be an object")
            return payload
        finally:
            ensemble_result.unlink(missing_ok=True)
            speaker_result.unlink(missing_ok=True)

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
            audio_duration = _wav_duration(Path(job.audio_path))
            warm_start = (
                backend in {"hybrid", "kotoba"}
                and self._pipeline is not None
            )
            stt_call_count = 2 if backend in {"hybrid", "whisperjav"} else 1
            artifact_dir = (
                self.settings.artifacts_dir / job.id
                if self.settings.debug_artifacts
                else None
            )
            request_trace = {
                "trace_schema_version": TRACE_SCHEMA_VERSION,
                "job_id": job.id,
                "request_id": job.id,
                "attempt": job.attempt,
                "parent_request_id": job.id if job.attempt > 1 else None,
                "input": {
                    "delivery_mode": "single_wav",
                    "sha256": job.audio_sha256,
                    "duration_sec": audio_duration,
                    "source_start_sec": 0.0,
                    "source_end_sec": audio_duration,
                },
                "provider": backend,
                "options": job.options,
                "option_semantics": {
                    "chunk_length_seconds": (
                        "not_applicable"
                        if backend == "whisperjav"
                        else "model_internal"
                    ),
                    "stt_call_count": stt_call_count,
                    "alignment_call_count": (
                        1 if backend == "whisperjav" else 0
                    ),
                    "diarization_call_count": 1,
                },
            }
            LOGGER.info(
                "stt_request job_id=%s request_id=%s attempt=%d "
                "delivery_mode=single_wav audio_sha256=%s "
                "duration_sec=%.3f provider=%s chunk_length_seconds=%s "
                "call_count=%d",
                job.id,
                job.id,
                job.attempt,
                job.audio_sha256,
                audio_duration,
                backend,
                job.options.get("chunk_length_seconds"),
                stt_call_count,
                extra=request_trace,
            )
            if artifact_dir is not None:
                write_json_atomic(
                    artifact_dir / "00_request.json",
                    request_trace,
                )
            option_names = {field.name for field in fields(TranscriptionOptions)}
            option_values = {
                key: value
                for key, value in job.options.items()
                if key in option_names
            }
            options = TranscriptionOptions(**option_values)
            words: list[dict[str, Any]] = []
            backend_quality: dict[str, Any] = {}
            if backend == "whisperjav":
                backend_result = self._run_whisperjav_worker(job)
                raw_segments = backend_result.get("segments")
                if not isinstance(raw_segments, list):
                    raise RuntimeError(
                        "WhisperJAV worker result has no segments list"
                    )
                segments = add_segment_ids(raw_segments)
                model = backend_result.get("model")
                timing = backend_result.get("timing")
                runtime = backend_result.get("runtime")
                noise_filter = backend_result.get("noise_filter")
                raw_words = backend_result.get("words", [])
                if isinstance(raw_words, list):
                    words = raw_words
                raw_quality = backend_result.get("quality", {})
                if isinstance(raw_quality, Mapping):
                    backend_quality = dict(raw_quality)
                if not isinstance(model, Mapping):
                    raise RuntimeError("WhisperJAV worker result has no model")
                if not isinstance(timing, Mapping):
                    raise RuntimeError("WhisperJAV worker result has no timing")
                if not isinstance(runtime, Mapping):
                    raise RuntimeError("WhisperJAV worker result has no runtime")
                if not isinstance(noise_filter, Mapping):
                    raise RuntimeError(
                        "WhisperJAV worker result has no noise_filter"
                    )
                runtime = {
                    **runtime,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            elif backend == "whisperx":
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
                raw_words = backend_result.get("words", [])
                if isinstance(raw_words, list):
                    words = raw_words
                raw_quality = backend_result.get("quality", {})
                if isinstance(raw_quality, Mapping):
                    backend_quality = dict(raw_quality)
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
            elif backend == "hybrid":
                hybrid_options = HybridRescueOptions.from_options(job.options)
                segmentation = WhisperXSegmentationOptions.from_options(
                    job.options
                )
                repetition_min_count = int(
                    job.options.get("repetition_min_count", 8)
                )
                kotoba_options = replace(
                    options,
                    chunk_length_seconds=(
                        hybrid_options.kotoba_chunk_length_seconds
                    ),
                )
                whisperx_options = {
                    **job.options,
                    "chunk_length_seconds": (
                        hybrid_options.whisperx_chunk_length_seconds
                    ),
                    "repetition_policy": "flag",
                }
                # Kotoba is loaded only after WhisperX finishes. Holding both
                # on a 10GB card makes CTranslate2 fail its batch allocations,
                # and the rescue pass cannot start before the primary result
                # exists anyway.
                primary_result = self._run_whisperx_worker(
                    job,
                    release_kotoba=True,
                    options=whisperx_options,
                )
                raw_primary_segments = primary_result.get("segments")
                if not isinstance(raw_primary_segments, list):
                    raise RuntimeError(
                        "WhisperX worker result has no segments list"
                    )
                raw_primary_words = primary_result.get("words", [])
                if not isinstance(raw_primary_words, list):
                    raw_primary_words = []
                words, speaker_debounce = debounce_word_speakers(
                    raw_primary_words,
                    maximum_flash_duration_sec=(
                        hybrid_options.speaker_debounce_sec
                    ),
                )
                rebuilt_primary = (
                    rebuild_whisperx_segments(words, segmentation)
                    if words
                    else raw_primary_segments
                )
                primary_segments = add_segment_ids(rebuilt_primary)
                for segment in primary_segments:
                    source_id = f"whisperx-{segment['id']}"
                    segment["id"] = source_id
                    segment["span_id"] = source_id
                    segment["parent_span_ids"] = list(
                        dict.fromkeys(
                            [*segment.get("parent_span_ids", []), source_id]
                        )
                    )
                    segment.setdefault(
                        "provider", "whisperx-word-segmentation-v1"
                    )
                primary_issues = detect_hybrid_issues(
                    primary_segments,
                    words,
                    options=hybrid_options,
                    repetition_min_count=repetition_min_count,
                )
                rescue_windows = merge_issue_windows(
                    primary_issues,
                    padding_seconds=hybrid_options.window_padding_sec,
                    audio_duration=audio_duration,
                )

                # Without a structurally failed window the rescue pass has
                # nothing to replace, so decoding the whole file a second time
                # buys nothing. Fusing an empty fallback returns the primary
                # segments unchanged.
                kotoba_skipped = not rescue_windows
                kotoba_started = time.monotonic()
                fallback_result: Mapping[str, Any] = {}
                fallback_segments: list[dict[str, Any]] = []
                fallback_issues: list[dict[str, Any]] = []
                if kotoba_skipped:
                    LOGGER.info(
                        "hybrid job %s has no rescue window; skipping the "
                        "Kotoba pass",
                        job.id,
                    )
                elif hybrid_options.rescue_scope == "windows":
                    (
                        window_segments,
                        fallback_result,
                    ) = self._run_kotoba_rescue_windows(
                        job,
                        rescue_windows,
                        primary_segments,
                        kotoba_options,
                        artifact_dir=artifact_dir,
                    )
                    fallback_segments = add_segment_ids(window_segments)
                else:
                    pipeline = self._get_pipeline()
                    fallback_result = run_pipeline(
                        pipeline,
                        Path(job.audio_path),
                        kotoba_options,
                        progress_callback=lambda progress: (
                            self._record_chunk_progress(job.id, progress)
                        ),
                        progress_every=self.settings.chunk_progress_every,
                        debug_artifact_dir=(
                            artifact_dir / "kotoba"
                            if artifact_dir is not None
                            else None
                        ),
                    )
                    fallback_segments = add_segment_ids(
                        normalize_segments(fallback_result)
                    )
                for segment in fallback_segments:
                    source_id = f"kotoba-{segment['id']}"
                    segment["id"] = source_id
                    segment["span_id"] = source_id
                    segment["parent_span_ids"] = list(
                        dict.fromkeys(
                            [
                                *segment.get("parent_span_ids", []),
                                source_id,
                            ]
                        )
                    )
                    segment.setdefault("provider", "kotoba")
                if fallback_segments:
                    fallback_issues = detect_hybrid_issues(
                        fallback_segments,
                        [],
                        options=hybrid_options,
                        repetition_min_count=repetition_min_count,
                    )
                kotoba_elapsed = (
                    0.0
                    if kotoba_skipped
                    else round(time.monotonic() - kotoba_started, 3)
                )
                fused_segments, hybrid_quality = fuse_hybrid_segments(
                    primary_segments,
                    fallback_segments,
                    rescue_windows,
                    fallback_issues=fallback_issues,
                    audio_duration=audio_duration,
                    primary_words=words,
                    fallback_speakers_preassigned=(
                        hybrid_options.rescue_scope == "windows"
                    ),
                )
                words = mark_rescued_words(
                    words,
                    hybrid_quality["decisions"],
                )
                segments = add_segment_ids(fused_segments)

                primary_model = primary_result.get("model")
                primary_timing = primary_result.get("timing")
                primary_runtime = primary_result.get("runtime")
                primary_noise_filter = primary_result.get("noise_filter")
                if not isinstance(primary_model, Mapping):
                    raise RuntimeError("WhisperX worker result has no model")
                if not isinstance(primary_timing, Mapping):
                    raise RuntimeError("WhisperX worker result has no timing")
                if not isinstance(primary_runtime, Mapping):
                    raise RuntimeError("WhisperX worker result has no runtime")
                if not isinstance(primary_noise_filter, Mapping):
                    raise RuntimeError(
                        "WhisperX worker result has no noise_filter"
                    )
                fallback_noise_filter = fallback_result.get(
                    "noise_filter",
                    {
                        "enabled": kotoba_options.noise_filter,
                        "provider": "kotoba-noise-filter-v1",
                        "execution_state": (
                            "skipped"
                            if kotoba_skipped
                            else "not_configured"
                        ),
                        "removed_count": None,
                        "removed_spans": [],
                    },
                )
                model = {
                    "id": "whisperx+kotoba",
                    "revision": (
                        f"{primary_model.get('revision', 'unknown')}+"
                        f"{MODEL_REVISION}"
                    ),
                    "primary": dict(primary_model),
                    "rescue": {
                        "id": MODEL_ID,
                        "revision": MODEL_REVISION,
                    },
                }
                timing = {
                    "postprocessor": HYBRID_POLICY_VERSION,
                    "primary": dict(primary_timing),
                    "rescue": {
                        "postprocessor": fallback_result.get(
                            "timestamp_postprocessor", "model-default"
                        )
                    },
                }
                runtime = {
                    "backend": "hybrid",
                    "device": self.settings.device,
                    "diarization_device": self.settings.diarization_device,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "simultaneous_model_residency": False,
                    "kotoba_skipped": kotoba_skipped,
                    "primary": dict(primary_runtime),
                    "rescue": {
                        "backend": "kotoba",
                        "skipped": kotoba_skipped,
                        "scope": hybrid_options.rescue_scope,
                        "elapsed_seconds": kotoba_elapsed,
                    },
                }
                noise_filter = {
                    "enabled": True,
                    "provider": HYBRID_POLICY_VERSION,
                    "execution_state": "composite",
                    "candidate_count": None,
                    "kept_count": None,
                    "removed_count": None,
                    "removed_duration_sum_sec": None,
                    "removed_duration_union_sec": None,
                    "removed_spans": [],
                    "primary": dict(primary_noise_filter),
                    "rescue": (
                        dict(fallback_noise_filter)
                        if isinstance(fallback_noise_filter, Mapping)
                        else {}
                    ),
                }
                primary_quality = primary_result.get("quality", {})
                if not isinstance(primary_quality, Mapping):
                    primary_quality = {}
                fallback_encoding = fallback_result.get("encoding_warning")
                backend_quality = {
                    "encoding_warning": dict(
                        primary_quality.get("encoding_warning", {})
                    )
                    if isinstance(
                        primary_quality.get("encoding_warning"), Mapping
                    )
                    else {},
                    "whisperx": dict(primary_quality),
                    "kotoba": {
                        "encoding_warning": (
                            dict(fallback_encoding)
                            if isinstance(fallback_encoding, Mapping)
                            else {}
                        ),
                        "issues": fallback_issues,
                    },
                    "hybrid": {
                        **hybrid_quality,
                        "options": asdict(hybrid_options),
                        "primary_issues": primary_issues,
                        "speaker_debounce": speaker_debounce,
                    },
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
                    debug_artifact_dir=(
                        artifact_dir / "kotoba"
                        if artifact_dir is not None
                        else None
                    ),
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
                        "provider": "kotoba-noise-filter-v1",
                        "execution_state": "not_configured",
                        "trigger_level": options.noise_filter_trigger_level,
                        "removed_count": None,
                        "removed_spans": [],
                    },
                )
                encoding_warning = raw_result.get("encoding_warning")
                if isinstance(encoding_warning, Mapping):
                    backend_quality["encoding_warning"] = dict(
                        encoding_warning
                    )
            else:
                raise RuntimeError(
                    f"unsupported transcription backend: {backend}"
                )
            duplicate_metrics = overlap_duplicate_metrics(segments)
            quality = {
                **backend_quality,
                "normalization_version": NORMALIZATION_VERSION,
                "short_span_policy": options.short_span_policy,
                "short_spans": short_span_diagnostics(segments),
                "overlap_duplicates": duplicate_metrics,
            }
            pipeline_trace = {
                "provider": backend,
                "model": str(model.get("id", "unknown")),
                "model_version": str(model.get("revision", "unknown")),
                "pipeline_version": str(
                    timing.get("postprocessor", "unknown")
                ),
                "git_commit": _git_commit(),
            }
            payload: dict[str, Any] = {
                "schema_version": TRANSCRIPT_SCHEMA_VERSION,
                "trace_schema_version": TRACE_SCHEMA_VERSION,
                "job_id": job.id,
                "request_id": job.id,
                "request": {
                    "attempt": job.attempt,
                    "parent_request_id": job.id if job.attempt > 1 else None,
                },
                "input": request_trace["input"],
                "pipeline": pipeline_trace,
                "audio_sha256": job.audio_sha256,
                "model": model,
                "timing": timing,
                "runtime": runtime,
                "options": job.options,
                "noise_filter": noise_filter,
                "quality": quality,
                "segments": segments,
            }
            if words:
                payload["words"] = words
            result_path = self.result_dir / f"{job.id}.json"
            write_json_atomic(result_path, payload)
            if artifact_dir is not None:
                write_json_atomic(
                    artifact_dir / "00_runtime.json",
                    _runtime_trace(
                        self.settings,
                        backend=backend,
                        elapsed_seconds=round(time.monotonic() - started, 3),
                        warm_start=warm_start,
                    ),
                )
                write_json_atomic(
                    artifact_dir / "metrics" / "metric_config.json",
                    {
                        "normalization_version": NORMALIZATION_VERSION,
                        "duplicate_metric_version": duplicate_metrics[
                            "duplicate_metric_version"
                        ],
                        "similarity_threshold": duplicate_metrics[
                            "similarity_threshold"
                        ],
                    },
                )
                write_json_atomic(
                    artifact_dir / "metrics" / "metric_result.json",
                    quality,
                )
                write_json_atomic(
                    artifact_dir / "metrics" / "warnings.json",
                    {
                        key: value
                        for key, value in quality.items()
                        if key in {"encoding_warning", "repetition"}
                    },
                )
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
            removed_count = (
                noise_filter.get("removed_count")
                if isinstance(noise_filter, Mapping)
                else None
            )
            if isinstance(removed_count, int) and removed_count > 0:
                LOGGER.info(
                    "transcription job %s noise filter removed %d "
                    "non-speech span(s)",
                    job.id,
                    removed_count,
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
    settings: STTAPISettings | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        service = TranscriptionService(settings or STTAPISettings.from_env())
        change_hook = TranscriptionChangeHook(asyncio.get_running_loop())
        service.store.set_change_hook(change_hook.publish)
        app.state.transcription_service = service
        app.state.transcription_change_hook = change_hook
        service.start()
        try:
            yield
        finally:
            service.store.set_change_hook(None)
            service.stop()

    app = FastAPI(
        title="stt-to-subtitle native transcription API",
        version=__version__,
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
            "events_url": str(
                request.url_for("get_transcription_events", job_id=job.id)
            ),
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
        "/v1/transcriptions/{job_id}/events",
        dependencies=[Depends(require_bearer)],
        name="get_transcription_events",
    )
    def get_transcription_events(
        request: Request,
        job_id: str,
        service: TranscriptionService = Depends(get_service),
    ) -> StreamingResponse:
        if service.store.get(job_id) is None:
            raise HTTPException(status_code=404, detail="job not found")
        change_hook: TranscriptionChangeHook = (
            request.app.state.transcription_change_hook
        )

        async def stream() -> AsyncIterator[str]:
            version = change_hook.version(job_id)
            job = service.store.get(job_id)
            if job is None:
                return
            payload = json.dumps(
                job.public_dict(
                    report_every=service.settings.chunk_progress_every,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"id: {version}\nevent: transcription\ndata: {payload}\n\n"
            while True:
                if job.status in {"completed", "failed"}:
                    return
                updated_version = await change_hook.wait(job_id, version)
                if updated_version == version:
                    yield ": keep-alive\n\n"
                    continue
                version = updated_version
                job = service.store.get(job_id)
                if job is None:
                    return
                payload = json.dumps(
                    job.public_dict(
                        report_every=service.settings.chunk_progress_every,
                    ),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                yield (
                    f"id: {version}\nevent: transcription\n"
                    f"data: {payload}\n\n"
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
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
        "stt_to_subtitle.stt_api:app",
        host=os.environ.get("STT_HOST", "0.0.0.0"),
        port=int(os.environ.get("STT_PORT", "8100")),
        workers=1,
    )


if __name__ == "__main__":
    main()
