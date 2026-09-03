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
import signal
import subprocess
import threading
import time
from typing import Any, AsyncIterator, Callable, Mapping, Sequence
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
    build_recall_union_segments,
    debounce_word_speakers,
    detect_hybrid_issues,
    detect_owsm_coverage_issues,
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
from .transcription_progress import parse_stage_progress
from .time_display import configure_kst_logging
from .stt_quality import (
    NORMALIZATION_VERSION,
    overlap_duplicate_metrics,
    short_span_diagnostics,
)
from .stt_trace import TRACE_SCHEMA_VERSION
from .stt_options import (
    HYBRID_STABLE_SUBTITLE_SEGMENTATION,
    OWSMAuditOptions,
)
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
STT_BACKENDS = {"hybrid", "whisperjav"}


class TranscriptionCancelled(RuntimeError):
    """Raised when a persisted remote cancellation reaches a safe boundary."""


class InvalidTranscriptionOutput(RuntimeError):
    """A backend returned data that violates the transcript contract."""


_RESOURCE_EXHAUSTION_MARKERS = (
    "cuda out of memory",
    "mps backend out of memory",
    "cublas_status_alloc_failed",
    "cudaerrorinvaliddevice",
    "parallel_for failed",
    "cannot allocate memory",
    "outofmemoryerror",
)


def _classify_transcription_failure(
    error: BaseException,
) -> tuple[str, bool, str]:
    """Return failure code, retryability, and the smallest affected scope."""
    normalized = (
        f"{error.__class__.__name__}: {error}"
    ).casefold()
    if isinstance(error, MemoryError) or any(
        marker in normalized for marker in _RESOURCE_EXHAUSTION_MARKERS
    ):
        return "resource_exhausted", False, "backend"
    return "transcription_processing_error", False, "job"


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
    runtime_id: str = "transcriber"
    runtime_name: str = "Transcriber"
    enabled_backends: frozenset[str] = frozenset(STT_BACKENDS)
    device: str = "mps"
    diarization_device: str = "cpu"
    batch_size: int = 1
    whisperx_batch_size: int = 8
    threads: int | None = None
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024
    progress_interval: float = 30.0
    chunk_progress_every: int = 10
    model_idle_timeout_seconds: float = 900.0
    noise_filter_trigger_level: float = DEFAULT_NOISE_FILTER_TRIGGER_LEVEL
    kotoba_python: Path = Path(".venv-kotoba/bin/python")
    whisperx_python: Path = Path(".venv-whisperx/bin/python")
    whisperjav_python: Path = Path(".venv-whisperjav/bin/python")
    owsm_python: Path = Path(".venv-owsm/bin/python")
    whisperx_model: str = DEFAULT_WHISPERX_MODEL
    whisperx_language: str = DEFAULT_WHISPERX_LANGUAGE
    whisperx_compute_type: str = DEFAULT_WHISPERX_COMPUTE_TYPE
    whisperx_cache_dir: Path = Path("./var/cuda-cache/whisperx")
    debug_artifacts: bool = False
    debug_artifacts_dir: Path | None = None
    work_dir: Path | None = None

    @classmethod
    def from_env(cls) -> STTAPISettings:
        threads_value = os.environ.get("STT_THREADS", "").strip()
        return cls(
            state_dir=Path(
                os.environ.get("STT_STATE_DIR", "./var/stt")
            ).expanduser(),
            api_token=os.environ.get("STT_API_TOKEN", ""),
            hf_token=os.environ.get("HF_TOKEN", ""),
            runtime_id=os.environ.get(
                "TRANSCRIBER_ID",
                os.environ.get("STT_RUNTIME_ID", "transcriber"),
            ).strip(),
            runtime_name=os.environ.get(
                "TRANSCRIBER_NAME",
                os.environ.get("STT_RUNTIME_NAME", "Transcriber"),
            ).strip(),
            enabled_backends=frozenset(
                backend.strip().lower()
                for backend in os.environ.get(
                    "STT_ENABLED_BACKENDS",
                    ",".join(sorted(STT_BACKENDS)),
                ).split(",")
                if backend.strip()
            ),
            work_dir=(
                Path(os.environ["STT_WORK_DIR"]).expanduser()
                if os.environ.get("STT_WORK_DIR", "").strip()
                else None
            ),
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
            model_idle_timeout_seconds=float(
                os.environ.get("STT_MODEL_IDLE_TIMEOUT_SECONDS", "900")
            ),
            noise_filter_trigger_level=float(
                os.environ.get(
                    "STT_NOISE_FILTER_TRIGGER_LEVEL",
                    str(DEFAULT_NOISE_FILTER_TRIGGER_LEVEL),
                )
            ),
            kotoba_python=Path(
                os.environ.get(
                    "KOTOBA_PYTHON",
                    ".venv-kotoba/bin/python",
                )
            ).expanduser(),
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
            owsm_python=Path(
                os.environ.get(
                    "OWSM_PYTHON",
                    ".venv-owsm/bin/python",
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

    @property
    def incoming_dir(self) -> Path:
        return self.work_dir or self.state_dir / "incoming"

    def validate(self) -> None:
        if not self.hf_token.strip():
            raise ValueError("HF_TOKEN is required")
        if not self.runtime_id or len(self.runtime_id) > 80:
            raise ValueError(
                "TRANSCRIBER_ID must be between 1 and 80 characters"
            )
        if not self.runtime_name or len(self.runtime_name) > 80:
            raise ValueError(
                "TRANSCRIBER_NAME must be between 1 and 80 characters"
            )
        if not self.enabled_backends:
            raise ValueError("STT_ENABLED_BACKENDS must not be empty")
        unknown_backends = self.enabled_backends - STT_BACKENDS
        if unknown_backends:
            raise ValueError(
                "STT_ENABLED_BACKENDS contains unsupported backends: "
                f"{sorted(unknown_backends)}"
            )
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


def _device_unavailable_reason_from_probe(
    probe: Mapping[str, Any],
    device: str,
) -> str | None:
    if device == "cpu":
        return None
    if device == "mps":
        if not bool(probe.get("mps_available")):
            return "PyTorch MPS is not available"
        return None
    if not bool(probe.get("cuda_available")):
        return "PyTorch CUDA is not available"
    if ":" not in device:
        return None
    index = int(device.split(":", maxsplit=1)[1])
    count = int(probe.get("cuda_device_count", 0))
    if index >= count:
        return (
            f"CUDA device {device} is not available; "
            f"found {count} CUDA device(s)"
        )
    return None


def _readiness_python(settings: STTAPISettings) -> Path:
    enabled = settings.enabled_backends
    candidates: list[Path] = []
    if "hybrid" in enabled:
        candidates.append(settings.whisperx_python)
    if "hybrid" in enabled:
        candidates.append(settings.kotoba_python)
    if "hybrid" in enabled:
        candidates.append(settings.owsm_python)
    if "whisperjav" in enabled:
        candidates.append(settings.whisperjav_python)
    candidates.extend(
        (
            settings.kotoba_python,
            settings.whisperx_python,
            settings.whisperjav_python,
            settings.owsm_python,
        )
    )
    return next((path for path in candidates if path.is_file()), candidates[0])


def _probe_worker_torch(settings: STTAPISettings) -> dict[str, Any]:
    python = _readiness_python(settings)
    if not python.is_file():
        raise RuntimeError(f"model Python was not found: {python}")
    environment = os.environ.copy()
    nvidia_paths = _venv_nvidia_library_paths(python)
    existing = environment.get("LD_LIBRARY_PATH", "").strip()
    if existing:
        nvidia_paths.append(existing)
    if nvidia_paths:
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(nvidia_paths)
    completed = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import json, torch; "
                "mps = getattr(getattr(torch, 'backends', None), 'mps', None); "
                "print(json.dumps({"
                "'cuda_available': bool(torch.cuda.is_available()), "
                "'cuda_device_count': int(torch.cuda.device_count()), "
                "'mps_available': bool(mps is not None and mps.is_available())"
                "}))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env=environment,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        message = detail[-1] if detail else "unknown import error"
        raise RuntimeError(f"model PyTorch probe failed: {message}")
    try:
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as error:
        raise RuntimeError("model PyTorch probe returned invalid output") from error
    if not isinstance(payload, dict):
        raise RuntimeError("model PyTorch probe returned invalid output")
    return payload


def _whisperx_unavailable_reason(settings: STTAPISettings) -> str | None:
    if settings.device == "mps" or settings.diarization_device == "mps":
        return "WhisperX backend supports only cpu or CUDA devices"
    if not settings.whisperx_python.is_file():
        return (
            "WhisperX Python was not found: "
            f"{settings.whisperx_python}"
        )
    return None


def _kotoba_unavailable_reason(settings: STTAPISettings) -> str | None:
    if not settings.kotoba_python.is_file():
        return f"Kotoba Python was not found: {settings.kotoba_python}"
    return None


def _owsm_unavailable_reason(settings: STTAPISettings) -> str | None:
    if settings.device == "mps":
        return "OWSM audit supports only cpu or CUDA devices"
    if not settings.owsm_python.is_file():
        return f"OWSM Python was not found: {settings.owsm_python}"
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

    Kotoba reloads its cached pipeline when this value changes. WhisperJAV
    uses a different batching concept and does not accept this option.
    """

    if decoded.get("batch_size") is not None:
        if backend == "whisperjav":
            raise ValueError(
                f"{backend} does not support the common batch_size option"
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
    if backend == "hybrid":
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
        "kotoba_batch_size",
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
        "owsm_audit",
        "whisperjav",
    }
    unknown = set(decoded) - allowed
    if unknown:
        raise ValueError(f"unsupported transcription options: {sorted(unknown)}")
    backend = str(decoded.get("backend", "hybrid")).strip().lower()
    if backend not in STT_BACKENDS:
        raise ValueError("backend must be 'hybrid' or 'whisperjav'")
    noise_filter = decoded.get("noise_filter", True)
    if not isinstance(noise_filter, bool):
        raise ValueError("noise_filter must be a JSON boolean")
    if backend in {"hybrid", "whisperjav"} and not noise_filter:
        raise ValueError(
            f"{backend} backend requires noise_filter=true for VAD"
        )
    batch_size = _resolve_batch_size(decoded, backend, settings)
    if decoded.get("kotoba_batch_size") is not None and backend != "hybrid":
        raise ValueError(
            "kotoba_batch_size requires a Hybrid backend"
        )
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
    if backend == "hybrid":
        hybrid = HybridRescueOptions.from_options(decoded)
        segmentation = WhisperXSegmentationOptions.from_options(
            decoded,
            defaults=(
                (
                    HYBRID_STABLE_SUBTITLE_SEGMENTATION
                    if hybrid is not None
                    and hybrid.stable_ts_regroup_enabled
                    else DEFAULT_SUBTITLE_SEGMENTATION
                )
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
            raw_kotoba_batch_size = decoded.get(
                "kotoba_batch_size",
                settings.batch_size,
            )
            if (
                isinstance(raw_kotoba_batch_size, bool)
                or not isinstance(raw_kotoba_batch_size, int)
            ):
                raise ValueError("kotoba_batch_size must be an integer")
            if not (
                WHISPERX_MIN_BATCH_SIZE
                <= raw_kotoba_batch_size
                <= WHISPERX_MAX_BATCH_SIZE
            ):
                raise ValueError(
                    "kotoba_batch_size must be between "
                    f"{WHISPERX_MIN_BATCH_SIZE} and "
                    f"{WHISPERX_MAX_BATCH_SIZE}"
                )
            if hybrid is None:
                raise RuntimeError("hybrid options were not initialized")
            parsed["hybrid_rescue"] = asdict(hybrid)
            parsed["owsm_audit"] = asdict(
                OWSMAuditOptions.from_options(decoded)
            )
            parsed["kotoba_batch_size"] = raw_kotoba_batch_size
            parsed["chunk_length_seconds"] = (
                hybrid.kotoba_chunk_length_seconds
            )
    elif backend == "whisperjav":
        forbidden = {
            "repetition_policy",
            "repetition_min_count",
            "hybrid_rescue",
            "owsm_audit",
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
            "owsm_audit",
            "whisperjav",
        )
    ):
        raise ValueError(
            "WhisperX quality options require backend='whisperx' or 'hybrid'"
        )
    return parsed


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
        self.incoming_dir = self.settings.incoming_dir
        self.result_dir = self.settings.state_dir / "results"
        self.incoming_dir.mkdir(parents=True, exist_ok=True)
        self.result_dir.mkdir(parents=True, exist_ok=True)
        if self.settings.debug_artifacts:
            self.settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.store = TranscriptionStore(self.settings.state_dir / "jobs.sqlite3")
        rebased_paths = self.store.rebase_audio_paths(
            previous_root=self.settings.state_dir / "incoming",
            current_root=self.incoming_dir,
        )
        if rebased_paths:
            LOGGER.info(
                "rebased saved upload paths for %d transcription job(s)",
                rebased_paths,
            )
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._pipeline: SpeechPipeline | None = None
        self._pipeline_batch_size: int | None = None
        # Held for the whole of a job so the idle reaper can never unload the
        # pipeline out from under a running transcription.
        self._pipeline_lock = threading.RLock()
        self._pipeline_idle_since = time.monotonic()
        self._stopping = threading.Event()
        self._process_lock = threading.Lock()
        self._active_processes: dict[str, subprocess.Popen[str]] = {}
        self._activity_lock = threading.Lock()
        self._active_job_id: str | None = None
        self._device_probe_lock = threading.Lock()
        self._device_probe_checked_at = 0.0
        self._device_probe: dict[str, Any] | None = None
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="stt-runtime-worker",
            daemon=True,
        )
        self._idle_reaper = threading.Thread(
            target=self._idle_reaper_loop,
            name="stt-runtime-idle-reaper",
            daemon=True,
        )
        self._started_at = time.time()

    def start(self) -> None:
        interrupted = self.store.fail_interrupted_jobs()
        if interrupted:
            LOGGER.warning("marked %d interrupted transcription job(s) failed", interrupted)
        self._worker.start()
        if self.settings.model_idle_timeout_seconds >= 0:
            self._idle_reaper.start()
        for job_id in self.store.queued_ids():
            self._queue.put(job_id)

    def stop(self) -> None:
        self._stopping.set()
        self._queue.put(None)
        self._worker.join(timeout=5)
        if self._idle_reaper.is_alive():
            self._idle_reaper.join(timeout=5)

    def cancel(self, job_id: str) -> TranscriptionJob | None:
        job = self.store.request_cancel(job_id)
        if job is None:
            return None
        if job.status == "cancel_requested":
            self._terminate_active_process(job_id)
        return self.store.get(job_id)

    def _raise_if_cancel_requested(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is not None and job.status in {"cancel_requested", "cancelled"}:
            raise TranscriptionCancelled("transcription cancelled by user")

    def _terminate_active_process(self, job_id: str) -> None:
        with self._process_lock:
            process = self._active_processes.get(job_id)
        if process is None or process.poll() is not None:
            return
        self._signal_process(process, signal.SIGTERM)

    @staticmethod
    def _signal_process(
        process: subprocess.Popen[str],
        requested_signal: signal.Signals,
    ) -> None:
        try:
            os.killpg(process.pid, requested_signal)
        except OSError:
            try:
                if requested_signal == signal.SIGKILL:
                    process.kill()
                else:
                    process.terminate()
            except OSError:
                return

    def _run_worker_process(
        self,
        job_id: str,
        command: Sequence[str],
        *,
        environment: Mapping[str, str],
        progress_path: Path | None = None,
        on_stage_progress: Callable[[str, int, int], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self._raise_if_cancel_requested(job_id)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(environment),
            start_new_session=True,
        )
        with self._process_lock:
            self._active_processes[job_id] = process
        last_stage_progress: tuple[str, int, int] | None = None

        def forward_stage_progress() -> None:
            nonlocal last_stage_progress
            if progress_path is None or on_stage_progress is None:
                return
            try:
                payload = json.loads(progress_path.read_text(encoding="utf-8"))
                if not isinstance(payload, Mapping):
                    return
                progress = parse_stage_progress(payload)
            except (OSError, ValueError):
                return
            if progress == last_stage_progress:
                return
            last_stage_progress = progress
            on_stage_progress(*progress)

        try:
            try:
                while True:
                    try:
                        stdout, stderr = process.communicate(timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        forward_stage_progress()
                        self._raise_if_cancel_requested(job_id)
            except TranscriptionCancelled:
                self._signal_process(process, signal.SIGTERM)
                try:
                    process.communicate(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self._signal_process(process, signal.SIGKILL)
                    process.communicate()
                raise
        finally:
            with self._process_lock:
                self._active_processes.pop(job_id, None)
        self._raise_if_cancel_requested(job_id)
        forward_stage_progress()
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout,
            stderr,
        )

    def health(self) -> dict[str, Any]:
        queue_snapshot = self.queue_snapshot()
        return {
            "status": "ok",
            "transcriber": {
                "id": self.settings.runtime_id,
                "name": self.settings.runtime_name,
                "version": __version__,
            },
            "uptime_seconds": round(time.time() - self._started_at, 3),
            "queued_jobs": queue_snapshot["queued"],
            "queue": queue_snapshot,
            "loaded_backend": "kotoba" if self._pipeline is not None else None,
            "loaded_kotoba_batch_size": self._pipeline_batch_size,
            "model_idle_timeout_seconds": (
                self.settings.model_idle_timeout_seconds
            ),
            "model_idle_seconds": (
                round(time.monotonic() - self._pipeline_idle_since, 1)
                if self._pipeline is not None
                else None
            ),
        }

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        detail: dict[str, Any] = {
            "transcriber": {
                "id": self.settings.runtime_id,
                "name": self.settings.runtime_name,
                "version": __version__,
            },
            "device": self.settings.device,
            "diarization_device": self.settings.diarization_device,
            "default_batch_sizes": {
                "kotoba": self.settings.batch_size,
                "whisperx": self.settings.whisperx_batch_size,
            },
            "hf_token_configured": bool(self.settings.hf_token.strip()),
            "queue": self.queue_snapshot(),
            "backends": {
                backend: {"status": "ready"}
                for backend in sorted(self.settings.enabled_backends)
            },
        }
        if not detail["hf_token_configured"]:
            detail["status"] = "not_ready"
            detail["reason"] = "HF_TOKEN is not configured"
            return False, detail

        try:
            import torch
        except ImportError:
            try:
                with self._device_probe_lock:
                    if (
                        self._device_probe is None
                        or time.monotonic() - self._device_probe_checked_at >= 60
                    ):
                        self._device_probe = _probe_worker_torch(self.settings)
                        self._device_probe_checked_at = time.monotonic()
                    device_probe = self._device_probe
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                detail["status"] = "not_ready"
                detail["reason"] = str(error)
                return False, detail
            reason_for = lambda device: _device_unavailable_reason_from_probe(
                device_probe,
                device,
            )
        else:
            reason_for = lambda device: _device_unavailable_reason(torch, device)
        reason = reason_for(self.settings.device)
        if reason is not None:
            detail["status"] = "not_ready"
            detail["reason"] = reason
            return False, detail
        reason = reason_for(self.settings.diarization_device)
        if reason is not None:
            detail["status"] = "not_ready"
            detail["reason"] = (
                f"{reason} for STT_DIARIZATION_DEVICE"
            )
            return False, detail
        for backend in self.settings.enabled_backends:
            backend_reason = self.backend_unavailable_reason(backend)
            if backend_reason is not None:
                detail["backends"][backend] = {
                    "status": "unavailable",
                    "reason": backend_reason,
                }
        unavailable = [
            value
            for value in detail["backends"].values()
            if value.get("status") == "unavailable"
        ]
        if len(unavailable) == len(self.settings.enabled_backends):
            detail["status"] = "not_ready"
            detail["reason"] = str(unavailable[0]["reason"])
            return False, detail
        detail["status"] = "ready"
        return True, detail

    def queue_snapshot(self) -> dict[str, Any]:
        counts = self.store.status_counts()
        with self._activity_lock:
            active_job_id = self._active_job_id
        return {
            "queued": counts["queued"],
            "running": counts["running"],
            "cancel_requested": counts["cancel_requested"],
            "active_job_id": active_job_id,
        }

    def backend_unavailable_reason(self, backend: str) -> str | None:
        if backend not in self.settings.enabled_backends:
            return (
                f"transcription backend {backend!r} is not enabled on "
                f"transcriber {self.settings.runtime_id!r}"
            )
        if backend == "hybrid":
            return (
                _whisperx_unavailable_reason(self.settings)
                or _kotoba_unavailable_reason(self.settings)
                or _owsm_unavailable_reason(self.settings)
            )
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
            if existing.status in {"cancelled", "failed"}:
                if not Path(existing.audio_path).is_file():
                    raise ValueError(
                        "saved upload for the retryable job is unavailable"
                    )
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

    def _get_pipeline(self, batch_size: int | None = None) -> SpeechPipeline:
        effective_batch_size = batch_size or self.settings.batch_size
        if (
            self._pipeline is not None
            and self._pipeline_batch_size != effective_batch_size
        ):
            self._release_pipeline(
                "to change batch size from "
                f"{self._pipeline_batch_size} to {effective_batch_size}"
            )
        if self._pipeline is None:
            LOGGER.info(
                "loading %s on %s with Pyannote on %s and batch_size=%s",
                MODEL_ID,
                self.settings.device,
                self.settings.diarization_device,
                effective_batch_size,
            )
            self._pipeline = load_pipeline(
                self.settings.hf_token,
                batch_size=effective_batch_size,
                device=self.settings.device,
                diarization_device=self.settings.diarization_device,
                threads=self.settings.threads,
            )
            self._pipeline_batch_size = effective_batch_size
            LOGGER.info("transcription model loaded")
        self._pipeline_idle_since = time.monotonic()
        return self._pipeline

    def _run_kotoba_worker(
        self,
        job: TranscriptionJob,
        options: TranscriptionOptions,
        *,
        windows: Sequence[Mapping[str, Any]] | None = None,
        artifact_dir: Path | None = None,
    ) -> Mapping[str, Any]:
        """Run Kotoba in an isolated process after other GPU workers exit."""
        reason = _kotoba_unavailable_reason(self.settings)
        if reason is not None:
            raise RuntimeError(reason)
        worker_result = self.result_dir / f".{job.id}.kotoba.json"
        progress_path = self.result_dir / f".{job.id}.kotoba.progress.json"
        worker_result.unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "HF_TOKEN": self.settings.hf_token,
                "STT_DEVICE": self.settings.device,
                "STT_DIARIZATION_DEVICE": self.settings.diarization_device,
                "PYTHONIOENCODING": "utf-8",
            }
        )
        nvidia_library_paths = _venv_nvidia_library_paths(
            self.settings.kotoba_python
        )
        if nvidia_library_paths:
            existing_library_path = environment.get("LD_LIBRARY_PATH", "")
            environment["LD_LIBRARY_PATH"] = ":".join(
                [
                    *nvidia_library_paths,
                    *([existing_library_path] if existing_library_path else []),
                ]
            )
        command = [
            str(self.settings.kotoba_python),
            "-m",
            "stt_to_subtitle.kotoba_worker",
            "--audio",
            job.audio_path,
            "--output",
            str(worker_result),
            "--options",
            json.dumps(asdict(options), sort_keys=True),
            "--windows",
            json.dumps(
                [dict(window) for window in windows]
                if windows is not None
                else None,
                sort_keys=True,
            ),
            "--progress",
            str(progress_path),
        ]
        if artifact_dir is not None:
            command.extend(["--debug-dir", str(artifact_dir)])
        try:
            completed = self._run_worker_process(
                job.id,
                command,
                environment=environment,
                progress_path=progress_path,
                on_stage_progress=lambda stage, index, total: (
                    self._record_stage_progress(
                        job.id,
                        stage=stage,
                        index=index,
                        total=total,
                    )
                ),
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise RuntimeError(
                    "Kotoba worker failed"
                    + (f": {detail[-2000:]}" if detail else "")
                )
            try:
                payload = json.loads(worker_result.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise InvalidTranscriptionOutput(
                    "Kotoba worker returned an invalid result"
                ) from error
            if not isinstance(payload, Mapping):
                raise InvalidTranscriptionOutput(
                    "Kotoba worker result must be an object"
                )
            return payload
        finally:
            worker_result.unlink(missing_ok=True)
            progress_path.unlink(missing_ok=True)

    def _run_owsm_audit_worker(
        self,
        job: TranscriptionJob,
        options: OWSMAuditOptions,
    ) -> Mapping[str, Any]:
        """Run the OWSM omission audit after WhisperX has exited."""
        reason = _owsm_unavailable_reason(self.settings)
        if reason is not None:
            raise RuntimeError(reason)
        worker_result = self.result_dir / f".{job.id}.owsm-audit.json"
        worker_result.unlink(missing_ok=True)
        environment = os.environ.copy()
        environment.update(
            {
                "HF_TOKEN": self.settings.hf_token,
                "PYTHONIOENCODING": "utf-8",
            }
        )
        nvidia_library_paths = _venv_nvidia_library_paths(
            self.settings.owsm_python
        )
        if nvidia_library_paths:
            existing_library_path = environment.get("LD_LIBRARY_PATH", "")
            environment["LD_LIBRARY_PATH"] = ":".join(
                [
                    *nvidia_library_paths,
                    *([existing_library_path] if existing_library_path else []),
                ]
            )
        command = [
            str(self.settings.owsm_python),
            "-m",
            "stt_to_subtitle.owsm_audit_worker",
            "--audio",
            job.audio_path,
            "--output",
            str(worker_result),
            "--device",
            self.settings.device,
            "--window-seconds",
            str(options.window_seconds),
            "--overlap-seconds",
            str(options.overlap_seconds),
        ]
        try:
            completed = self._run_worker_process(
                job.id,
                command,
                environment=environment,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise RuntimeError(
                    "OWSM audit worker failed"
                    + (f": {detail[-2000:]}" if detail else "")
                )
            try:
                payload = json.loads(worker_result.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise InvalidTranscriptionOutput(
                    "OWSM audit worker returned an invalid result"
                ) from error
            if not isinstance(payload, Mapping) or not isinstance(
                payload.get("windows"),
                list,
            ):
                raise InvalidTranscriptionOutput(
                    "OWSM audit worker result has no windows list"
                )
            return payload
        finally:
            worker_result.unlink(missing_ok=True)

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
        segments: list[dict[str, Any]] = []
        removed_spans: list[dict[str, Any]] = []
        encoding_warning: Mapping[str, Any] | None = None
        timestamp_postprocessor = "model-default"
        decoded_seconds = 0.0
        executed = 0
        created_base = 0
        completed_base = 0
        worker_payload = self._run_kotoba_worker(
            job,
            options,
            windows=windows,
            artifact_dir=(
                artifact_dir / "kotoba"
                if artifact_dir is not None
                else None
            ),
        )
        raw_windows = worker_payload.get("windows")
        if not isinstance(raw_windows, list):
            raise InvalidTranscriptionOutput(
                "Kotoba rescue worker result has no windows list"
            )
        for index, window_result in enumerate(raw_windows):
            if not isinstance(window_result, Mapping):
                raise InvalidTranscriptionOutput(
                    "Kotoba rescue worker returned an invalid window"
                )
            window = window_result.get("window")
            window_segments = window_result.get("segments")
            if not isinstance(window, Mapping) or not isinstance(
                window_segments,
                list,
            ):
                raise InvalidTranscriptionOutput(
                    "Kotoba rescue worker window is incomplete"
                )
            start = float(window["start"])
            duration = float(window_result.get("duration", 0.0))
            executed += 1
            decoded_seconds += duration
            normalized_window_segments = [
                dict(segment)
                for segment in window_segments
                if isinstance(segment, Mapping)
            ]
            speaker_mapping = map_fallback_speakers(
                primary_segments,
                normalized_window_segments,
            )
            window_id = str(
                window.get("window_id", f"rescue-window-{index + 1:06d}")
            )
            speaker_namespace = window_id.upper().replace("-", "_")
            for segment in normalized_window_segments:
                local_speaker = str(segment.get("speaker", "UNKNOWN"))
                mapped_speaker = speaker_mapping.get(
                    local_speaker,
                    f"KOTOBA_{local_speaker}",
                )
                if mapped_speaker == f"KOTOBA_{local_speaker}":
                    mapped_speaker = f"KOTOBA_{speaker_namespace}_{local_speaker}"
                segment["speaker"] = mapped_speaker
            segments.extend(normalized_window_segments)
            chunk_count = int(window_result.get("chunk_count", 0))
            created_base += chunk_count
            completed_base += chunk_count
            window_noise = window_result.get("noise_filter")
            if isinstance(window_noise, Mapping):
                for span in window_noise.get("removed_spans", []) or []:
                    if not isinstance(span, Mapping):
                        continue
                    shifted = dict(span)
                    shifted["start"] = round(
                        float(span.get("start", 0.0)) + start,
                        3,
                    )
                    shifted["end"] = round(
                        float(span.get("end", 0.0)) + start,
                        3,
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
        self._record_chunk_progress(
            job.id,
            ChunkProgress(
                created=created_base,
                completed=completed_base,
                final=False,
            ),
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

    def _release_pipeline(self, reason: str = "before backend switch") -> None:
        if self._pipeline is None:
            return
        LOGGER.info("unloading Kotoba transcription model %s", reason)
        self._pipeline = None
        self._pipeline_batch_size = None
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
        report_progress: bool = True,
    ) -> Mapping[str, Any]:
        # A hybrid-only runtime intentionally does not advertise WhisperX as
        # a public scheduling capability, but WhisperX remains its primary
        # internal component. Check the component executable directly here;
        # the public backend gate is enforced when the job is submitted.
        reason = _whisperx_unavailable_reason(self.settings)
        if reason is not None:
            raise RuntimeError(reason)
        if release_kotoba:
            self._release_pipeline()
        worker_result = self.result_dir / f".{job.id}.whisperx.json"
        progress_path = self.result_dir / f".{job.id}.whisperx.progress.json"
        worker_result.unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)
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
            "--progress",
            str(progress_path),
        ]
        if self.settings.debug_artifacts:
            command.extend(
                [
                    "--debug-dir",
                    str(self.settings.artifacts_dir / job.id / "whisperx"),
                ]
            )
        try:
            completed = self._run_worker_process(
                job.id,
                command,
                environment=environment,
                progress_path=progress_path if report_progress else None,
                on_stage_progress=(
                    lambda stage, index, total: self._record_stage_progress(
                        job.id,
                        stage=stage,
                        index=index,
                        total=total,
                    )
                )
                if report_progress
                else None,
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
                raise InvalidTranscriptionOutput(
                    "WhisperX worker returned an invalid result"
                ) from error
            if not isinstance(payload, Mapping):
                raise InvalidTranscriptionOutput(
                    "WhisperX worker result must be an object"
                )
            return payload
        finally:
            worker_result.unlink(missing_ok=True)
            progress_path.unlink(missing_ok=True)

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
        progress_path = self.result_dir / f".{job.id}.whisperjav.progress.json"
        ensemble_result.unlink(missing_ok=True)
        speaker_result.unlink(missing_ok=True)
        progress_path.unlink(missing_ok=True)
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
            "--progress",
            str(progress_path),
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
            for label, command, worker_environment, worker_progress in (
                (
                    "WhisperJAV",
                    whisperjav_command,
                    whisperjav_environment,
                    progress_path,
                ),
                (
                    "WhisperJAV speaker assignment",
                    speaker_command,
                    environment,
                    None,
                ),
            ):
                if worker_progress is None:
                    self._record_stage_progress(
                        job.id,
                        stage="speaker_diarization",
                        index=6,
                        total=7,
                    )
                completed = self._run_worker_process(
                    job.id,
                    command,
                    environment=worker_environment,
                    progress_path=worker_progress,
                    on_stage_progress=(
                        (
                            lambda stage, index, total: (
                                self._record_stage_progress(
                                    job.id,
                                    stage=stage,
                                    index=index,
                                    total=total,
                                )
                            )
                        )
                        if worker_progress is not None
                        else None
                    ),
                )
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout).strip()
                    raise RuntimeError(
                        f"{label} worker failed"
                        + (f": {detail[-2000:]}" if detail else "")
                    )
            self._record_stage_progress(
                job.id,
                stage="subtitle_normalization",
                index=7,
                total=7,
            )
            try:
                payload = json.loads(speaker_result.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise InvalidTranscriptionOutput(
                    "WhisperJAV worker returned an invalid result"
                ) from error
            if not isinstance(payload, Mapping):
                raise InvalidTranscriptionOutput(
                    "WhisperJAV worker result must be an object"
                )
            return payload
        finally:
            ensemble_result.unlink(missing_ok=True)
            speaker_result.unlink(missing_ok=True)
            progress_path.unlink(missing_ok=True)

    def _run_stable_ts_regroup_worker(
        self,
        job: TranscriptionJob,
        words: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        """Regroup WhisperX words without changing text or timestamps."""
        if not job.audio_path:
            raise RuntimeError("Hybrid job has no audio path")
        input_path = self.result_dir / f".{job.id}.stable-ts-input.json"
        output_path = self.result_dir / f".{job.id}.stable-ts-output.json"
        for path in (input_path, output_path):
            path.unlink(missing_ok=True)
        write_json_atomic(input_path, {"words": [dict(word) for word in words]})
        environment = os.environ.copy()
        environment.update(
            {
                "STT_DEVICE": self.settings.device,
                "PYTHONIOENCODING": "utf-8",
            }
        )
        nvidia_library_paths = _venv_nvidia_library_paths(
            self.settings.kotoba_python
        )
        if nvidia_library_paths:
            existing_library_path = environment.get("LD_LIBRARY_PATH", "")
            environment["LD_LIBRARY_PATH"] = ":".join(
                [
                    *nvidia_library_paths,
                    *([existing_library_path] if existing_library_path else []),
                ]
            )
        command = [
            str(self.settings.kotoba_python),
            "-m",
            "stt_to_subtitle.stable_ts_worker",
            "--audio",
            job.audio_path,
            "--input",
            str(input_path),
            "--output",
            str(output_path),
        ]
        try:
            completed = self._run_worker_process(
                job.id,
                command,
                environment=environment,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()
                raise RuntimeError(
                    "stable-ts regroup worker failed"
                    + (f": {detail[-2000:]}" if detail else "")
                )
            try:
                payload = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise InvalidTranscriptionOutput(
                    "stable-ts regroup worker returned an invalid result"
                ) from error
            if not isinstance(payload, Mapping):
                raise InvalidTranscriptionOutput(
                    "stable-ts regroup worker result must be an object"
                )
            return payload
        finally:
            input_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    def _worker_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                if job_id is None:
                    return
                with self._activity_lock:
                    self._active_job_id = job_id
                with self._pipeline_lock:
                    self._run_job(job_id)
            finally:
                with self._activity_lock:
                    self._active_job_id = None
                self._pipeline_idle_since = time.monotonic()
                self._queue.task_done()

    def _idle_reaper_loop(self) -> None:
        timeout = self.settings.model_idle_timeout_seconds
        interval = 5.0 if timeout <= 60 else 30.0
        while not self._stopping.wait(interval):
            try:
                self._release_idle_pipeline()
            except Exception:  # never let the reaper kill its own thread
                LOGGER.exception("idle model release failed")

    def _release_idle_pipeline(self) -> None:
        """Drop the resident Kotoba pipeline once nothing has needed it.

        WhisperX and WhisperJAV run as subprocesses and hand their VRAM back
        when they exit; only this in-process pipeline stays resident, so it is
        the one that has to be reaped.
        """
        timeout = self.settings.model_idle_timeout_seconds
        if timeout < 0 or self._pipeline is None or not self._queue.empty():
            return
        # A job holds the lock for its whole run; skip this tick rather than wait.
        if not self._pipeline_lock.acquire(blocking=False):
            return
        try:
            if self._pipeline is None or not self._queue.empty():
                return
            idle_for = time.monotonic() - self._pipeline_idle_since
            if idle_for < timeout:
                return
            self._release_pipeline(
                reason=f"after {idle_for:.0f}s idle (limit {timeout:.0f}s)"
            )
        finally:
            self._pipeline_lock.release()

    def _run_job(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or job.status != "queued":
            return
        if not self.store.update_if_status(
            job_id,
            {"queued"},
            status="running",
        ):
            return
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
            self._raise_if_cancel_requested(job_id)
            backend = str(job.options.get("backend", "hybrid"))
            audio_duration = _wav_duration(Path(job.audio_path))
            kotoba_batch_size = int(
                job.options.get("kotoba_batch_size", job.options["batch_size"])
            )
            warm_start = (
                backend == "hybrid"
                and self._pipeline is not None
                and self._pipeline_batch_size == kotoba_batch_size
            )
            stt_call_count = 3 if backend == "hybrid" else 2
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
                backend_label = "WhisperJAV"
                backend_result = self._run_whisperjav_worker(job)
                raw_segments = backend_result.get("segments")
                if not isinstance(raw_segments, list):
                    raise InvalidTranscriptionOutput(
                        f"{backend_label} worker result has no segments list"
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
                    raise InvalidTranscriptionOutput(
                        f"{backend_label} worker result has no model"
                    )
                if not isinstance(timing, Mapping):
                    raise InvalidTranscriptionOutput(
                        f"{backend_label} worker result has no timing"
                    )
                if not isinstance(runtime, Mapping):
                    raise InvalidTranscriptionOutput(
                        f"{backend_label} worker result has no runtime"
                    )
                if not isinstance(noise_filter, Mapping):
                    raise InvalidTranscriptionOutput(
                        f"{backend_label} worker result has no noise_filter"
                    )
                runtime = {
                    **runtime,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            elif backend == "whisperx":
                backend_result = self._run_whisperx_worker(job)
                raw_segments = backend_result.get("segments")
                if not isinstance(raw_segments, list):
                    raise InvalidTranscriptionOutput(
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
                    raise InvalidTranscriptionOutput(
                        "WhisperX worker result has no model"
                    )
                if not isinstance(timing, Mapping):
                    raise InvalidTranscriptionOutput(
                        "WhisperX worker result has no timing"
                    )
                if not isinstance(runtime, Mapping):
                    raise InvalidTranscriptionOutput(
                        "WhisperX worker result has no runtime"
                    )
                if not isinstance(noise_filter, Mapping):
                    raise InvalidTranscriptionOutput(
                        "WhisperX worker result has no noise_filter"
                    )
                runtime = {
                    **runtime,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            elif backend == "hybrid":
                owsm_enabled = True
                stage_total = 8
                hybrid_options = HybridRescueOptions.from_options(job.options)
                owsm_options = OWSMAuditOptions.from_options(job.options)
                segmentation = WhisperXSegmentationOptions.from_options(
                    job.options
                )
                repetition_min_count = int(
                    job.options.get("repetition_min_count", 8)
                )
                kotoba_options = replace(
                    options,
                    batch_size=kotoba_batch_size,
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
                    raise InvalidTranscriptionOutput(
                        "WhisperX worker result has no segments list"
                    )
                raw_primary_words = primary_result.get("words", [])
                if not isinstance(raw_primary_words, list):
                    raw_primary_words = []
                stable_ts_regroup = {
                    "enabled": hybrid_options.stable_ts_regroup_enabled,
                    "provider": "stable-ts-regroup-jav-v1",
                    "segment_count": 0,
                }
                if (
                    hybrid_options.stable_ts_regroup_enabled
                    and raw_primary_words
                ):
                    regrouped = self._run_stable_ts_regroup_worker(
                        job,
                        raw_primary_words,
                    )
                    regrouped_words = regrouped.get("words")
                    regrouped_segments = regrouped.get("segments")
                    if not isinstance(regrouped_words, list) or not isinstance(
                        regrouped_segments,
                        list,
                    ):
                        raise InvalidTranscriptionOutput(
                            "stable-ts regroup worker result is incomplete"
                        )
                    if len(regrouped_words) != len(raw_primary_words):
                        raise InvalidTranscriptionOutput(
                            "stable-ts regroup changed the WhisperX word count"
                        )
                    raw_primary_words = regrouped_words
                    stable_ts_regroup["segment_count"] = len(
                        regrouped_segments
                    )
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
                owsm_result: Mapping[str, Any] = {}
                owsm_issues: list[dict[str, Any]] = []
                if owsm_enabled:
                    if owsm_options is None:
                        raise RuntimeError("OWSM audit options were not initialized")
                    self._record_stage_progress(
                        job.id,
                        stage="owsm_audit",
                        index=4,
                        total=stage_total,
                    )
                    owsm_result = self._run_owsm_audit_worker(
                        job,
                        owsm_options,
                    )
                    raw_audit_windows = owsm_result.get("windows")
                    if not isinstance(raw_audit_windows, list):
                        raise InvalidTranscriptionOutput(
                            "OWSM audit result has no windows list"
                        )
                    owsm_issues = detect_owsm_coverage_issues(
                        [
                            dict(window)
                            for window in raw_audit_windows
                            if isinstance(window, Mapping)
                        ],
                        words,
                        primary_segments,
                        options=owsm_options,
                    )
                    primary_issues.extend(owsm_issues)
                self._record_stage_progress(
                    job.id,
                    stage="quality_analysis",
                    index=5 if owsm_enabled else 4,
                    total=stage_total,
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
                    self._record_stage_progress(
                        job.id,
                        stage="rescue_transcription",
                        index=6 if owsm_enabled else 5,
                        total=stage_total,
                    )
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
                    self._record_stage_progress(
                        job.id,
                        stage="rescue_transcription",
                        index=6 if owsm_enabled else 5,
                        total=stage_total,
                    )
                    worker_payload = self._run_kotoba_worker(
                        job,
                        kotoba_options,
                        artifact_dir=(
                            artifact_dir / "kotoba"
                            if artifact_dir is not None
                            else None
                        ),
                    )
                    raw_result = worker_payload.get("result")
                    raw_segments = worker_payload.get("segments")
                    if not isinstance(raw_result, Mapping) or not isinstance(
                        raw_segments,
                        list,
                    ):
                        raise InvalidTranscriptionOutput(
                            "Kotoba full worker result is incomplete"
                        )
                    fallback_result = raw_result
                    fallback_segments = add_segment_ids(
                        [
                            dict(segment)
                            for segment in raw_segments
                            if isinstance(segment, Mapping)
                        ]
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
                self._record_stage_progress(
                    job.id,
                    stage="transcription_merge",
                    index=7 if owsm_enabled else 6,
                    total=stage_total,
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
                fused_segments, hybrid_quality = build_recall_union_segments(
                    primary_segments,
                    fused_segments,
                    hybrid_quality,
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
                    raise InvalidTranscriptionOutput(
                        "WhisperX worker result has no model"
                    )
                if not isinstance(primary_timing, Mapping):
                    raise InvalidTranscriptionOutput(
                        "WhisperX worker result has no timing"
                    )
                if not isinstance(primary_runtime, Mapping):
                    raise InvalidTranscriptionOutput(
                        "WhisperX worker result has no runtime"
                    )
                if not isinstance(primary_noise_filter, Mapping):
                    raise InvalidTranscriptionOutput(
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
                    "id": "whisperx+owsm-audit+kotoba-recall-union",
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
                owsm_model = owsm_result.get("model")
                if owsm_enabled and isinstance(owsm_model, Mapping):
                    model["audit"] = dict(owsm_model)
                timing = {
                    "postprocessor": (
                        f"{HYBRID_POLICY_VERSION}+recall-union-v1"
                        + (
                            "+stable-ts-regroup-jav-v1"
                            if hybrid_options.stable_ts_regroup_enabled
                            else ""
                        )
                    ),
                    "primary": dict(primary_timing),
                    "rescue": {
                        "postprocessor": fallback_result.get(
                            "timestamp_postprocessor", "model-default"
                        )
                    },
                }
                runtime = {
                    "backend": backend,
                    "device": self.settings.device,
                    "diarization_device": self.settings.diarization_device,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "simultaneous_model_residency": False,
                    "kotoba_skipped": kotoba_skipped,
                    "primary": dict(primary_runtime),
                    "audit": (
                        {
                            **dict(owsm_result.get("runtime", {})),
                            "backend": "owsm",
                            "window_count": len(owsm_result.get("windows", [])),
                        }
                        if owsm_enabled
                        and isinstance(owsm_result.get("runtime"), Mapping)
                        else {"skipped": True}
                    ),
                    "rescue": {
                        "backend": "kotoba",
                        "skipped": kotoba_skipped,
                        "scope": hybrid_options.rescue_scope,
                        "merge_policy": "recall_union",
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
                        "stable_ts_regroup": stable_ts_regroup,
                    },
                }
                if owsm_enabled:
                    backend_quality["owsm_audit"] = {
                        "options": asdict(owsm_options),
                        "issues": owsm_issues,
                        "window_count": len(owsm_result.get("windows", [])),
                    }
                self._record_stage_progress(
                    job.id,
                    stage="subtitle_normalization",
                    index=8 if owsm_enabled else 7,
                    total=stage_total,
                )
            elif backend == "kotoba":
                self._record_stage_progress(
                    job.id,
                    stage="primary_transcription",
                    index=1,
                    total=2,
                )
                raw_result = run_pipeline(
                    self._get_pipeline(options.batch_size),
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
                self._record_stage_progress(
                    job.id,
                    stage="subtitle_normalization",
                    index=2,
                    total=2,
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
            self._raise_if_cancel_requested(job.id)
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
            if not self.store.update_if_status(
                job.id,
                {"running"},
                status="completed",
                result_path=result_path,
            ):
                result_path.unlink(missing_ok=True)
                self._raise_if_cancel_requested(job.id)
                raise RuntimeError("transcription status changed before completion")
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
        except TranscriptionCancelled:
            self.store.mark_cancelled(job.id)
            LOGGER.info("transcription job %s cancelled", job.id)
        except InvalidTranscriptionOutput as error:
            message = str(error).replace(self.settings.hf_token, "[redacted]")
            self.store.update_if_status(
                job.id,
                {"running"},
                status="failed",
                error=message[:2000] or error.__class__.__name__,
                failure_code="model_output_invalid",
                retryable=False,
                failure_scope="job",
            )
            LOGGER.exception("transcription job %s returned invalid output", job.id)
        except BaseException as error:
            message = str(error).replace(self.settings.hf_token, "[redacted]")
            if self.store.mark_cancelled(job.id):
                LOGGER.info("transcription job %s cancelled", job.id)
            else:
                failure_code, retryable, failure_scope = (
                    _classify_transcription_failure(error)
                )
                self.store.update_if_status(
                    job.id,
                    {"running"},
                    status="failed",
                    error=message[:2000] or error.__class__.__name__,
                    failure_code=failure_code,
                    retryable=retryable,
                    failure_scope=failure_scope,
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
        self._raise_if_cancel_requested(job_id)
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

    def _record_stage_progress(
        self,
        job_id: str,
        *,
        stage: str,
        index: int,
        total: int,
    ) -> None:
        self._raise_if_cancel_requested(job_id)
        if self.store.update_stage_progress(
            job_id,
            stage=stage,
            index=index,
            total=total,
        ):
            LOGGER.info(
                "transcription job %s stage %s (%d/%d)",
                job_id,
                stage,
                index,
                total,
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

    @app.post(
        "/v1/transcriptions/{job_id}/cancel",
        dependencies=[Depends(require_bearer)],
        name="cancel_transcription",
    )
    def cancel_transcription(
        job_id: str,
        service: TranscriptionService = Depends(get_service),
    ) -> dict[str, Any]:
        job = service.cancel(job_id)
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
                if job.status in {"cancelled", "completed", "failed"}:
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
        "stt_to_subtitle.runtime_api:app",
        host=os.environ.get("STT_HOST", "0.0.0.0"),
        port=int(os.environ.get("STT_PORT", "8100")),
        workers=1,
    )


if __name__ == "__main__":
    main()
