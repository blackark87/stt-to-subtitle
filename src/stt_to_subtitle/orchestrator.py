"""Stage-based web orchestration with independent bounded workers."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import asdict, replace
import hashlib
import json
import logging
import math
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4
import wave

from .artifact_retention import (
    audit_artifacts,
    cleanup_orphan_artifacts,
)
from .artifacts import artifact_filename, artifact_path
from .audio import AudioExtraction, extract_audio
from .contracts import (
    TRANSLATION_SCHEMA_VERSION,
    validate_transcript,
    validate_translation_items,
)
from .files import (
    copy_files_atomic,
    exclusive_file_lock,
    sha256_file,
    write_json_atomic,
)
from .stt_options import (
    DEFAULT_ANIME_MAX_GROUP_SECONDS,
    DEFAULT_CHUNK_LENGTH_SECONDS,
    DEFAULT_QWEN_MAX_GROUP_SECONDS,
    DEFAULT_SUBTITLE_SEGMENTATION,
    HybridRescueOptions,
    TranscriptionOptions,
    WHISPERX_MAX_BATCH_SIZE,
    WHISPERX_MAX_CHUNK_LENGTH_SECONDS,
    WHISPERX_MIN_BATCH_SIZE,
    WhisperJAVOptions,
    WhisperXSegmentationOptions,
)
from .path_display import PathDisplayRule
from .backend_config import (
    MediaLibrary,
    BackendSettings,
    RemoteServerSettings,
    SubtitleValidatorSettings,
    normalize_server_url,
    probe_media_duration,
)
from .job_store import (
    JobStore,
    PipelineJob,
    PromptCategory,
    RuntimeEndpoint,
    RETRYABLE_STATUSES,
    SUCCESS_STATUSES,
    WorkerLeaseLost,
)
from .job_state import JobReason, JobState
from .service_clients import (
    ExternalServiceError,
    OpenAICompatibleClient,
    OperationStopped,
    RemoteTranscriptionFailed,
    RequestConcurrencyLimiter,
    STTAPIClient,
    SubtitleValidationClient,
    TranslationPaused,
)
from .subtitle import write_styled_subtitles_atomic
from .subtitle_validation import build_subtitle_validator_payload
from .translation_prompt import (
    KOREAN_JAV_SYSTEM_PROMPT,
    KOREAN_TRANSLATION_REVIEW_PROMPT,
)

LOGGER = logging.getLogger(__name__)
MAX_EDITABLE_JSON_BYTES = 20 * 1024 * 1024
DEFAULT_ARTIFACT_CLEANUP_AGE_DAYS = 7
WAITING_STAGE_BY_STATUS = {
    "queued": "audio extraction",
    "audio_ready": "transcription",
    "transcribed": "translation",
    "translated": "render",
}
RUNNING_STATUSES = {
    "extracting",
    "transcription_running",
    "translation_running",
    "rendering",
}
RUNNING_STAGE_BY_STATUS = {
    "extracting": "extraction",
    "transcription_running": "transcription",
    "translation_running": "translation",
    "rendering": "render",
}
EVENT_PHASE_BY_STAGE = {
    "audio extraction": "extraction",
    "extraction": "extraction",
    "transcription": "transcription",
    "translation": "translation",
    "render": "render",
}
JOB_LEASE_SECONDS = 60.0
JOB_LEASE_HEARTBEAT_SECONDS = 15.0
JOB_SHUTDOWN_GRACE_SECONDS = 5.0
RUNTIME_REPROBE_INTERVAL_SECONDS = 30.0
BUILTIN_RUNTIME_ID = "builtin"
MAX_RUNTIME_ENDPOINTS = 32
LEGACY_BUILTIN_RUNTIME_URLS = {
    "http://stt:8100",
    "http://stt-backend:8100",
}


def _canonical_payload_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _transcription_model_revision(
    payload: Mapping[str, Any],
    *,
    fallback: str,
) -> str:
    explicit = payload.get("model_revision")
    if explicit is not None and not isinstance(explicit, Mapping):
        value = str(explicit).strip()
        if value:
            return value
    model = payload.get("model")
    if isinstance(model, Mapping):
        for key in ("revision", "id"):
            value = str(model.get(key, "")).strip()
            if value:
                return value
    elif model is not None:
        value = str(model).strip()
        if value:
            return value
    backend = str(payload.get("backend", "")).strip()
    return backend or fallback


USER_STOP_MESSAGE = "사용자 요청으로 전체 작업이 중단되었습니다."
USER_SELECTED_STOP_MESSAGE = "사용자 요청으로 작업이 중단되었습니다."
TRANSLATION_PROMPT_OPTION = "translation_prompt"
TRANSLATION_REVIEW_ROUNDS = 2
SUBTITLE_RENDERER_VERSION = "1"
SUPPORTED_OPERATIONS = {"extract", "transcribe", "translate", "full"}
TRANSLATION_OPERATIONS = {"translate", "full"}
TRANSCRIPTION_COMPARISON_BACKENDS = (
    "whisperjav",
    "hybrid",
    "whisperx",
    "kotoba",
)
TRANSCRIPTION_COMPARISON_SCHEMA_VERSION = 2
MAX_TRANSCRIPTION_COMPARISON_SOURCES = 20
COMPARISON_PARENT_ID_OPTION = "comparison_parent_id"
COMPARISON_AUDIO_SOURCE_JOB_ID_OPTION = "comparison_audio_source_job_id"


def wav_duration_seconds(path: Path) -> float | None:
    """Read the exact duration represented by an extracted PCM WAV header."""
    try:
        with wave.open(str(path), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            if frame_rate <= 0:
                return None
            return round(wav_file.getnframes() / frame_rate, 3)
    except (EOFError, OSError, wave.Error, ZeroDivisionError):
        return None


def estimate_transcription_chunks(
    duration_seconds: float | None,
    options: Mapping[str, Any],
) -> int:
    """Estimate model chunks from extracted audio duration and backend options."""
    if duration_seconds is None or duration_seconds <= 0:
        return 0
    raw_chunk_length: object = options.get(
        "chunk_length_seconds",
        DEFAULT_CHUNK_LENGTH_SECONDS,
    )
    if str(options.get("backend", "kotoba")) == "hybrid":
        hybrid_options = options.get("hybrid_rescue")
        if isinstance(hybrid_options, Mapping):
            raw_chunk_length = hybrid_options.get(
                "kotoba_chunk_length_seconds",
                raw_chunk_length,
            )
    elif str(options.get("backend", "kotoba")) == "whisperjav":
        whisperjav_options = options.get("whisperjav")
        if isinstance(whisperjav_options, Mapping):
            try:
                pass1 = float(
                    whisperjav_options.get(
                        "anime_max_group_duration_seconds",
                        DEFAULT_ANIME_MAX_GROUP_SECONDS,
                    )
                )
                pass2 = float(
                    whisperjav_options.get(
                        "qwen_max_group_duration_seconds",
                        DEFAULT_QWEN_MAX_GROUP_SECONDS,
                    )
                )
            except (TypeError, ValueError):
                return 0
            if pass1 <= 0 or pass2 <= 0:
                return 0
            return max(
                2,
                math.ceil(duration_seconds / pass1)
                + math.ceil(duration_seconds / pass2),
            )
    try:
        chunk_length = float(raw_chunk_length)
    except (TypeError, ValueError):
        return 0
    if chunk_length <= 0:
        return 0
    return max(1, math.ceil(duration_seconds / chunk_length))


def _audio_extraction_signature(
    options: Mapping[str, Any],
) -> tuple[int, float, float | None]:
    """Return the normalized fields that determine the extracted WAV."""
    raw_duration = options.get("duration_seconds")
    duration = (
        float(raw_duration) if raw_duration not in (None, "", 0, 0.0) else None
    )
    return (
        int(options.get("audio_stream", 0)),
        float(options.get("start_seconds", 0.0)),
        duration,
    )


SUPPORTED_STT_BACKENDS = {"kotoba", "whisperx", "hybrid", "whisperjav"}


def _operation_is_completed(
    latest: PipelineJob | None,
    operation: str,
    *,
    has_subtitle: bool,
) -> bool:
    """Return whether a source already reached the requested terminal stage."""
    if has_subtitle:
        return True
    if latest is None:
        return False
    if operation == "extract":
        return latest.status in SUCCESS_STATUSES
    if operation == "transcribe":
        return latest.status in {"transcription_completed", "completed"}
    return latest.status == "completed"


class SubtitleOrchestrator:
    """Advance persisted jobs while keeping each remote resource independent."""

    def __init__(self, settings: BackendSettings) -> None:
        settings.validate()
        self.settings = settings
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.settings.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.library = MediaLibrary(
            settings.media_root,
            settings.maximum_listed_files,
            duration_probe=probe_media_duration,
        )
        self.store = JobStore(settings.state_dir / "jobs.sqlite3")
        self._path_display_rules = tuple(
            self.store.list_path_display_rules()
        )
        rebased_paths = self.store.rebase_artifact_paths(
            previous_root=settings.state_dir / "jobs",
            current_root=settings.jobs_dir,
        )
        if rebased_paths:
            LOGGER.info(
                "rebased artifact paths for %d record(s)",
                rebased_paths,
            )
        saved_servers = self.store.get_remote_server_settings()
        if (
            saved_servers is not None
            and str(saved_servers["stt_base_url"])
            in LEGACY_BUILTIN_RUNTIME_URLS
            and settings.stt_base_url.strip()
            and str(saved_servers["stt_base_url"])
            != settings.stt_base_url.strip().rstrip("/")
        ):
            saved_servers = {
                **saved_servers,
                "stt_base_url": settings.stt_base_url.strip().rstrip("/"),
            }
            self.store.save_remote_server_settings(**saved_servers)
        initial_servers = (
            RemoteServerSettings(**saved_servers)
            if saved_servers is not None
            else settings.remote_servers()
        )
        saved_validator = self.store.get_subtitle_validator_settings()
        self._subtitle_validator = (
            SubtitleValidatorSettings(**saved_validator)
            if saved_validator is not None
            else SubtitleValidatorSettings()
        )
        self._translation_request_limiter = RequestConcurrencyLimiter(
            initial_servers.translation_workers
        )
        self._remote_runtime: tuple[
            STTAPIClient | None,
            OpenAICompatibleClient | None,
            RemoteServerSettings,
        ] = (None, None, initial_servers)
        self._stt_gate_lock = threading.RLock()
        self._runtime_lock = threading.RLock()
        self._runtime_clients: dict[str, STTAPIClient] = {}
        self._runtime_health: dict[str, dict[str, Any]] = {}
        self._runtime_dispatch_cursor = 0
        self._translation_circuit_lock = threading.RLock()
        self._subtitle_publication_lock = threading.RLock()
        self._stage_futures_lock = threading.RLock()
        self._stage_futures: set[Future[Any]] = set()
        self._worker_id = f"backend-{uuid4().hex}"
        saved_translation_state = self.store.get_dependency_state(
            "translation_lm"
        )
        saved_translation_state_name = (
            str(saved_translation_state["state"])
            if saved_translation_state is not None
            else None
        )
        self._translation_circuit_state = (
            "lost"
            if saved_translation_state_name == "lost"
            else "ready"
            if initial_servers.translation_is_complete
            else "offline"
        )
        if saved_translation_state_name != self._translation_circuit_state:
            self.store.save_dependency_state(
                "translation_lm",
                state=self._translation_circuit_state,
            )
        saved_stt_gate = self.store.get_dependency_state("stt")
        self._stt_gate_state = (
            str(saved_stt_gate["state"])
            if saved_stt_gate is not None
            else "ready"
            if initial_servers.stt_is_complete
            else "unknown"
        )
        self._stt_gate_message = (
            str(saved_stt_gate.get("last_error") or "연결 확인이 필요합니다.")
            if saved_stt_gate is not None
            and self._stt_gate_state in {"lost", "unknown"}
            else "사용 가능"
        )
        if initial_servers.stt_is_complete:
            self._runtime_health[BUILTIN_RUNTIME_ID] = {
                "status": self._stt_gate_state,
                "message": self._stt_gate_message,
                "readiness": None,
                "checked_at": None,
            }
        if initial_servers.stt_is_complete:
            try:
                self._set_remote_servers(initial_servers, persist=False)
            except ValueError as error:
                LOGGER.warning("remote server settings are invalid: %s", error)
        self._load_external_runtimes()
        self._stop_event = threading.Event()
        self._scheduler = threading.Thread(
            target=self._scheduler_loop,
            name="pipeline-job-scheduler",
            daemon=True,
        )
        self._audio_executor = ThreadPoolExecutor(
            max_workers=settings.audio_workers,
            thread_name_prefix="pipeline-audio",
        )
        # Rendering only writes subtitle files, so it never contends with
        # ffmpeg extraction. Keeping it on its own single-slot executor lets
        # the next job's extraction start while the previous one renders.
        self._render_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="pipeline-render",
        )
        self._stt_executor = ThreadPoolExecutor(
            max_workers=MAX_RUNTIME_ENDPOINTS,
            thread_name_prefix="pipeline-stt",
        )
        self._runtime_probe_executor = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="runtime-probe",
        )
        self._translation_executor = ThreadPoolExecutor(
            # translation_workers belongs to parallel batches within one
            # file. Keep files serial so one file owns those workers until
            # its translation is complete.
            max_workers=1,
            thread_name_prefix="pipeline-translation",
        )

    @property
    def stt_client(self) -> STTAPIClient | None:
        return self._remote_runtime[0]

    @property
    def lm_client(self) -> OpenAICompatibleClient | None:
        return self._remote_runtime[1]

    @property
    def remote_servers(self) -> RemoteServerSettings:
        return self._remote_runtime[2]

    @property
    def remote_servers_configured(self) -> bool:
        return (
            self.transcription_server_configured
            and self.lm_client is not None
        )

    @property
    def transcription_server_configured(self) -> bool:
        with self._runtime_lock:
            return bool(self._runtime_clients)

    @property
    def translation_server_configured(self) -> bool:
        return self.lm_client is not None

    def remote_servers_view(self) -> dict[str, Any]:
        servers = self.remote_servers
        with self._stt_gate_lock:
            stt_gate_state = self._stt_gate_state
            stt_gate_message = self._stt_gate_message
        return {
            "stt_base_url": servers.stt_base_url,
            "stt_token_configured": bool(servers.stt_token),
            "lm_base_url": servers.lm_base_url,
            "lm_token_configured": bool(servers.lm_token),
            "lm_model": servers.lm_model,
            "translation_workers": servers.translation_workers,
            "configured": self.remote_servers_configured,
            "transcription_configured": self.transcription_server_configured,
            "translation_configured": self.translation_server_configured,
            "stt_gate_state": stt_gate_state,
            "stt_gate_message": stt_gate_message,
        }

    @staticmethod
    def _validate_runtime_values(
        *,
        name: str,
        base_url: str,
        capacity: int,
    ) -> tuple[str, str, int]:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 80:
            raise ValueError("Runtime 이름은 1~80자여야 합니다.")
        if not 1 <= capacity <= 8:
            raise ValueError("Runtime 동시 작업 수는 1~8이어야 합니다.")
        normalized_url = normalize_server_url(base_url, "RUNTIME_BASE_URL")
        return normalized_name, normalized_url, capacity

    def _load_external_runtimes(self) -> None:
        with self._runtime_lock:
            for endpoint in self.store.list_runtime_endpoints():
                if endpoint.enabled:
                    self._runtime_clients[endpoint.id] = STTAPIClient(
                        endpoint.base_url,
                        endpoint.token,
                        request_observer=self.record_external_request,
                    )
                    self._runtime_health.setdefault(
                        endpoint.id,
                        {
                            "status": "unknown",
                            "message": None,
                            "readiness": None,
                            "checked_at": None,
                        },
                    )
                else:
                    self._runtime_health[endpoint.id] = {
                        "status": "disabled",
                        "message": None,
                        "readiness": None,
                        "checked_at": None,
                    }

    def _runtime_definitions(self) -> list[dict[str, Any]]:
        definitions: list[dict[str, Any]] = []
        servers = self.remote_servers
        if servers.stt_is_complete:
            definitions.append(
                {
                    "id": BUILTIN_RUNTIME_ID,
                    "name": "기본 Runtime",
                    "base_url": servers.stt_base_url,
                    "token": servers.stt_token,
                    "enabled": True,
                    "capacity": 1,
                    "builtin": True,
                }
            )
        definitions.extend(
            {
                "id": endpoint.id,
                "name": endpoint.name,
                "base_url": endpoint.base_url,
                "token": endpoint.token,
                "enabled": endpoint.enabled,
                "capacity": endpoint.capacity,
                "builtin": False,
            }
            for endpoint in self.store.list_runtime_endpoints()
        )
        return definitions

    def _runtime_definition(self, runtime_id: str) -> dict[str, Any]:
        for definition in self._runtime_definitions():
            if definition["id"] == runtime_id:
                return definition
        raise ValueError("Runtime을 찾을 수 없습니다.")

    def _runtime_client(self, runtime_id: str) -> STTAPIClient | None:
        with self._runtime_lock:
            return self._runtime_clients.get(runtime_id)

    def _stt_client_for_job(self, job: PipelineJob) -> STTAPIClient | None:
        return self._runtime_client(job.stt_runtime_id or BUILTIN_RUNTIME_ID)

    def _refresh_stt_gate_from_pool(
        self,
        *,
        reason_code: str | None = None,
        message: str | None = None,
    ) -> None:
        definitions = [
            definition
            for definition in self._runtime_definitions()
            if definition["enabled"]
        ]
        with self._runtime_lock:
            statuses = [
                str(self._runtime_health.get(definition["id"], {}).get(
                    "status", "unknown"
                ))
                for definition in definitions
            ]
        if any(status == "ready" for status in statuses):
            self._set_stt_gate("ready", "사용 가능")
        elif any(status == "checking" for status in statuses):
            self._set_stt_gate("checking", "연결 확인 중", persist=False)
        elif any(status == "unavailable" for status in statuses):
            self._set_stt_gate(
                "lost",
                message or "사용 가능한 Runtime이 없습니다.",
                reason_code=reason_code or JobReason.STT_UNAVAILABLE.value,
            )
        elif definitions:
            self._set_stt_gate("unknown", "연결 확인이 필요합니다.")
        else:
            self._set_stt_gate("offline", "Runtime이 등록되지 않았습니다.")

    def _set_runtime_health(
        self,
        runtime_id: str,
        status: str,
        *,
        message: str | None = None,
        readiness: Mapping[str, Any] | None = None,
        reason_code: str | None = None,
    ) -> None:
        with self._runtime_lock:
            self._runtime_health[runtime_id] = {
                "status": status,
                "message": message,
                "readiness": dict(readiness) if readiness is not None else None,
                "checked_at": time.time(),
            }
        self._refresh_stt_gate_from_pool(
            reason_code=reason_code,
            message=message,
        )

    def runtime_endpoints_view(self) -> list[dict[str, Any]]:
        counts = self.store.transcription_runtime_counts()
        with self._runtime_lock:
            health = {
                runtime_id: dict(value)
                for runtime_id, value in self._runtime_health.items()
            }
        views: list[dict[str, Any]] = []
        for definition in self._runtime_definitions():
            runtime_id = str(definition["id"])
            current = health.get(runtime_id, {"status": "unknown"})
            readiness = current.get("readiness")
            running = counts.get(runtime_id, 0)
            capacity = int(definition["capacity"])
            views.append(
                {
                    "id": runtime_id,
                    "name": definition["name"],
                    "base_url": definition["base_url"],
                    "token_configured": bool(definition["token"]),
                    "enabled": bool(definition["enabled"]),
                    "capacity": capacity,
                    "builtin": bool(definition["builtin"]),
                    "status": current.get("status", "unknown"),
                    "message": current.get("message"),
                    "checked_at": current.get("checked_at"),
                    "running_jobs": running,
                    "available_slots": max(0, capacity - running),
                    "identity": (
                        readiness.get("runtime")
                        if isinstance(readiness, Mapping)
                        else None
                    ),
                    "queue": (
                        readiness.get("queue")
                        if isinstance(readiness, Mapping)
                        else None
                    ),
                    "backends": (
                        readiness.get("backends")
                        if isinstance(readiness, Mapping)
                        else None
                    ),
                }
            )
        return views

    def create_runtime_endpoint(
        self,
        *,
        name: str,
        base_url: str,
        token: str,
        enabled: bool,
        capacity: int,
    ) -> dict[str, Any]:
        name, base_url, capacity = self._validate_runtime_values(
            name=name,
            base_url=base_url,
            capacity=capacity,
        )
        with self._runtime_lock:
            if (
                len(self.store.list_runtime_endpoints())
                >= MAX_RUNTIME_ENDPOINTS - 1
            ):
                raise ValueError("등록 가능한 외부 Runtime 수를 초과했습니다.")
            if base_url == self.remote_servers.stt_base_url:
                raise ValueError("기본 Runtime과 같은 주소는 등록할 수 없습니다.")
            endpoint = self.store.create_runtime_endpoint(
                name=name,
                base_url=base_url,
                token=token,
                enabled=enabled,
                capacity=capacity,
            )
            self._install_runtime_endpoint(endpoint)
        if enabled:
            self.probe_runtime_endpoint(endpoint.id)
        return self._runtime_view(endpoint.id)

    def _install_runtime_endpoint(self, endpoint: RuntimeEndpoint) -> None:
        with self._runtime_lock:
            if endpoint.enabled:
                self._runtime_clients[endpoint.id] = STTAPIClient(
                    endpoint.base_url,
                    endpoint.token,
                    request_observer=self.record_external_request,
                )
                self._runtime_health[endpoint.id] = {
                    "status": "unknown",
                    "message": None,
                    "readiness": None,
                    "checked_at": None,
                }
            else:
                self._runtime_clients.pop(endpoint.id, None)
                self._runtime_health[endpoint.id] = {
                    "status": "disabled",
                    "message": None,
                    "readiness": None,
                    "checked_at": time.time(),
                }
        self._refresh_stt_gate_from_pool()

    def update_runtime_endpoint(
        self,
        runtime_id: str,
        *,
        name: str,
        base_url: str,
        token: str | None,
        clear_token: bool,
        enabled: bool,
        capacity: int,
    ) -> dict[str, Any]:
        if runtime_id == BUILTIN_RUNTIME_ID:
            raise ValueError("기본 Runtime은 서버 설정에서 변경하세요.")
        name, base_url, capacity = self._validate_runtime_values(
            name=name,
            base_url=base_url,
            capacity=capacity,
        )
        with self._runtime_lock:
            current = self.store.get_runtime_endpoint(runtime_id)
            if current is None:
                raise ValueError("Runtime을 찾을 수 없습니다.")
            if self.store.runtime_has_active_transcriptions(runtime_id) and (
                not enabled or base_url != current.base_url
            ):
                raise ValueError(
                    "진행 중인 작업이 있는 Runtime 설정은 변경할 수 없습니다."
                )
            if base_url == self.remote_servers.stt_base_url:
                raise ValueError("기본 Runtime과 같은 주소는 등록할 수 없습니다.")
            updated = self.store.update_runtime_endpoint(
                runtime_id,
                name=name,
                base_url=base_url,
                token=(
                    ""
                    if clear_token
                    else current.token
                    if token is None
                    else token
                ),
                enabled=enabled,
                capacity=capacity,
            )
            self._install_runtime_endpoint(updated)
        if enabled:
            self.probe_runtime_endpoint(runtime_id)
        return self._runtime_view(runtime_id)

    def delete_runtime_endpoint(self, runtime_id: str) -> None:
        if runtime_id == BUILTIN_RUNTIME_ID:
            raise ValueError("기본 Runtime은 삭제할 수 없습니다.")
        with self._runtime_lock:
            if self.store.runtime_has_active_transcriptions(runtime_id):
                raise ValueError(
                    "진행 중인 작업이 있는 Runtime은 삭제할 수 없습니다."
                )
            self.store.delete_runtime_endpoint(runtime_id)
            self._runtime_clients.pop(runtime_id, None)
            self._runtime_health.pop(runtime_id, None)
        self._refresh_stt_gate_from_pool()

    def _runtime_view(self, runtime_id: str) -> dict[str, Any]:
        return next(
            view
            for view in self.runtime_endpoints_view()
            if view["id"] == runtime_id
        )

    def probe_runtime_endpoint(self, runtime_id: str) -> dict[str, Any]:
        definition = self._runtime_definition(runtime_id)
        if not definition["enabled"]:
            raise ValueError("비활성화된 Runtime은 확인할 수 없습니다.")
        self._set_runtime_health(runtime_id, "checking")
        client = self._runtime_client(runtime_id)
        if client is None:
            raise ValueError("Runtime 연결 설정이 없습니다.")
        probe_client = (
            client
            if runtime_id == BUILTIN_RUNTIME_ID
            else STTAPIClient(
                str(definition["base_url"]),
                str(definition["token"]),
                attempts=1,
                connect_timeout=3.0,
                read_timeout=5.0,
                request_observer=self.record_external_request,
            )
        )
        try:
            readiness = probe_client.check_readiness()
        except ExternalServiceError as error:
            self._record_dependency_readiness("stt", "unavailable")
            self._set_runtime_health(
                runtime_id,
                "unavailable",
                message=self._sanitize_error(str(error)),
            )
        else:
            self._record_dependency_readiness("stt", "ready")
            self._record_stt_queue_snapshot(readiness, runtime_id=runtime_id)
            identity = readiness.get("runtime")
            observed_id = (
                str(identity.get("id", "")).strip()
                if isinstance(identity, Mapping)
                else ""
            )
            with self._runtime_lock:
                duplicate_id = next(
                    (
                        other_id
                        for other_id, current in self._runtime_health.items()
                        if other_id != runtime_id
                        and current.get("status") == "ready"
                        and isinstance(current.get("readiness"), Mapping)
                        and isinstance(
                            current["readiness"].get("runtime"), Mapping
                        )
                        and str(
                            current["readiness"]["runtime"].get("id", "")
                        ).strip()
                        == observed_id
                        and observed_id
                    ),
                    None,
                )
            if duplicate_id is not None:
                self._set_runtime_health(
                    runtime_id,
                    "unavailable",
                    message="같은 Runtime ID가 이미 등록되어 있습니다.",
                    readiness=readiness,
                )
            else:
                self._set_runtime_health(
                    runtime_id,
                    "ready",
                    readiness=readiness,
                )
        return self._runtime_view(runtime_id)

    def subtitle_validator_view(self) -> dict[str, Any]:
        settings = self._subtitle_validator
        return {
            "provider": settings.provider,
            "base_url": settings.base_url,
            "token_configured": bool(settings.token),
            "model": settings.model,
            "region": settings.region,
            "configured": settings.is_complete,
        }

    def update_subtitle_validator(
        self,
        settings: SubtitleValidatorSettings,
    ) -> SubtitleValidatorSettings:
        normalized = settings.normalized()
        self.store.save_subtitle_validator_settings(
            provider=normalized.provider,
            base_url=normalized.base_url,
            token=normalized.token,
            model=normalized.model,
            region=normalized.region,
        )
        self._subtitle_validator = normalized
        return normalized

    def validate_subtitles_with_llm(
        self,
        validation_id: str,
    ) -> tuple[dict[str, Any], bool]:
        validation = self.store.get_subtitle_validation_by_id(validation_id)
        if validation is None:
            raise ValueError("자막 비교 결과를 찾을 수 없습니다.")
        settings = self._subtitle_validator.normalized()
        payload = build_subtitle_validator_payload(validation["metrics"])
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        input_hash = hashlib.sha256(
            settings.provider.encode("utf-8")
            + b"\0"
            + settings.region.encode("utf-8")
            + b"\0"
            + settings.base_url.encode("utf-8")
            + b"\0"
            + settings.model.encode("utf-8")
            + b"\0"
            + encoded
        ).hexdigest()
        if (
            validation["llm"] is not None
            and validation["validator_provider"] == settings.provider
            and validation["validator_model"] == settings.model
            and validation["validator_input_hash"] == input_hash
        ):
            self.record_subtitle_validation("llm", "cache_hit")
            return validation, True
        try:
            result = SubtitleValidationClient(
                settings.base_url,
                settings.token,
                settings.model,
                provider=settings.provider,
                region=settings.region,
                request_observer=self.record_external_request,
            ).validate(payload)
            updated = self.store.save_subtitle_llm_validation(
                validation_id,
                result=result,
                provider=settings.provider,
                model=settings.model,
                input_hash=input_hash,
            )
        except Exception:
            self.record_subtitle_validation("llm", "failed")
            raise
        self.record_subtitle_validation("llm", "completed")
        return updated, False

    @property
    def translation_circuit_state(self) -> str:
        with self._translation_circuit_lock:
            return self._translation_circuit_state

    @property
    def stt_gate_state(self) -> str:
        with self._stt_gate_lock:
            return self._stt_gate_state

    def _record_measurement(
        self,
        metric: str,
        value: float,
        *,
        labels: Mapping[str, str | int | bool] | None = None,
    ) -> None:
        try:
            self.store.record_operational_measurement(
                metric,
                value,
                labels=labels,
            )
        except Exception:
            LOGGER.exception(
                "operational measurement write failed: %s",
                metric,
            )

    def _record_lease_fencing_rejection(
        self,
        stage: str,
        detection: str,
    ) -> None:
        self._record_measurement(
            "lease.fencing_rejections",
            1,
            labels={"stage": stage, "detection": detection},
        )

    def record_subtitle_validation(self, mode: str, outcome: str) -> None:
        if mode not in {"local", "llm"}:
            raise ValueError("invalid subtitle validation mode")
        if outcome not in {"completed", "cache_hit", "failed"}:
            raise ValueError("invalid subtitle validation outcome")
        self._record_measurement(
            "subtitle.validation.runs",
            1,
            labels={"mode": mode, "outcome": outcome},
        )

    def _record_dependency_readiness(
        self,
        dependency: str,
        outcome: str,
    ) -> None:
        self._record_measurement(
            "dependency.readiness_checks",
            1,
            labels={"dependency": dependency, "outcome": outcome},
        )

    def record_external_request(self, observation: Mapping[str, Any]) -> None:
        labels: dict[str, str | int | bool] = {
            "service": str(observation.get("service", "external")),
            "operation": str(observation.get("operation", "request")),
            "outcome": str(observation.get("outcome", "unknown")),
            "attempt": int(observation.get("attempt", 1)),
        }
        if observation.get("status_code") is not None:
            labels["status_code"] = int(observation["status_code"])
        if observation.get("error_type") is not None:
            labels["error_type"] = str(observation["error_type"])
        self._record_measurement(
            "external.request.duration_seconds",
            max(0.0, float(observation.get("elapsed_seconds", 0.0))),
            labels=labels,
        )

    def _record_stt_queue_snapshot(
        self,
        readiness: Mapping[str, Any],
        *,
        runtime_id: str = BUILTIN_RUNTIME_ID,
    ) -> None:
        queue = readiness.get("queue")
        if not isinstance(queue, Mapping):
            return
        for state in ("queued", "running", "cancel_requested"):
            value = queue.get(state)
            if isinstance(value, int) and value >= 0:
                self._record_measurement(
                    "remote_stt.queue.jobs",
                    value,
                    labels={
                        "state": state,
                        **(
                            {"runtime_id": runtime_id}
                            if runtime_id != BUILTIN_RUNTIME_ID
                            else {}
                        ),
                    },
                )

    def _record_artifact_audit(self, audit: Mapping[str, object]) -> None:
        for state, key in (
            ("total", "total_files"),
            ("referenced", "referenced_files"),
            ("missing", "missing_count"),
            ("orphan", "orphan_count"),
            ("cleanup_eligible", "cleanup_eligible_count"),
            ("skipped_symlink", "skipped_symlinks"),
        ):
            value = audit.get(key)
            if isinstance(value, int) and value >= 0:
                self._record_measurement(
                    "artifact.audit.files",
                    value,
                    labels={"state": state},
                )
        for state, key in (
            ("total", "total_bytes"),
            ("orphan", "orphan_bytes"),
            ("cleanup_eligible", "cleanup_eligible_bytes"),
        ):
            value = audit.get(key)
            if isinstance(value, int) and value >= 0:
                self._record_measurement(
                    "artifact.audit.bytes",
                    value,
                    labels={"state": state},
                )

    def _set_stt_gate(
        self,
        state: str,
        message: str,
        *,
        reason_code: str | None = None,
        persist: bool = True,
    ) -> None:
        with self._stt_gate_lock:
            previous_state = self._stt_gate_state
            self._stt_gate_state = state
            self._stt_gate_message = message
        if persist:
            self.store.save_dependency_state(
                "stt",
                state=state,
                reason_code=reason_code,
                error=message if state in {"lost", "unknown"} else None,
            )
        if previous_state != state:
            self._record_measurement(
                "dependency.gate.transitions",
                1,
                labels={
                    "dependency": "stt",
                    "from_state": previous_state,
                    "to_state": state,
                    "reason": reason_code or "none",
                },
            )

    def _set_translation_circuit(
        self,
        state: str,
        *,
        reason_code: str | None = None,
        error: str | None = None,
    ) -> None:
        with self._translation_circuit_lock:
            previous_state = self._translation_circuit_state
            self._translation_circuit_state = state
        self.store.save_dependency_state(
            "translation_lm",
            state=state,
            reason_code=reason_code,
            error=error if state == "lost" else None,
        )
        if previous_state != state:
            self._record_measurement(
                "dependency.circuit.transitions",
                1,
                labels={
                    "dependency": "translation_lm",
                    "from_state": previous_state,
                    "to_state": state,
                    "reason": reason_code or "none",
                },
            )

    def activate_transcription_stt(self) -> int:
        """Open the STT pool after explicit readiness checks."""
        enabled = [
            definition
            for definition in self._runtime_definitions()
            if definition["enabled"]
        ]
        if not enabled:
            raise ValueError("전사 서버 설정을 먼저 저장하세요.")
        views = [
            self.probe_runtime_endpoint(str(definition["id"]))
            for definition in enabled
        ]
        if not any(view["status"] == "ready" for view in views):
            message = next(
                (
                    str(view["message"])
                    for view in views
                    if view.get("message")
                ),
                "사용 가능한 Runtime이 없습니다.",
            )
            raise ExternalServiceError(message)

        retried = 0
        for job_id in self.store.ids_with_status("blocked"):
            job = self.store.get(job_id)
            if (
                job is None
                or job.blocked_stage != "transcription"
                or job.state == JobState.STOPPED
            ):
                continue
            try:
                self.retry(job.id)
            except ValueError:
                continue
            retried += 1
        return retried

    def active_prompt_categories(self) -> list[PromptCategory]:
        return self.store.list_prompt_categories()

    def all_prompt_categories(self) -> list[PromptCategory]:
        return self.store.list_prompt_categories(include_archived=True)

    @property
    def path_display_rules(self) -> tuple[PathDisplayRule, ...]:
        return self._path_display_rules

    def artifact_audit(
        self,
        *,
        minimum_age_days: int = DEFAULT_ARTIFACT_CLEANUP_AGE_DAYS,
    ) -> dict[str, object]:
        audit = audit_artifacts(
            self.settings.jobs_dir,
            self.store.artifact_references(),
            minimum_age_days=minimum_age_days,
        )
        self._record_artifact_audit(audit.to_view())
        return audit.to_view()

    def cleanup_artifacts(
        self,
        *,
        minimum_age_days: int = DEFAULT_ARTIFACT_CLEANUP_AGE_DAYS,
        expected_token: str,
    ) -> dict[str, object]:
        audit = audit_artifacts(
            self.settings.jobs_dir,
            self.store.artifact_references(),
            minimum_age_days=minimum_age_days,
        )
        self._record_artifact_audit(audit.to_view())
        if not expected_token or audit.cleanup_token != expected_token:
            raise ValueError(
                "산출물 감사 결과가 변경되었습니다. 다시 감사를 실행하세요."
            )
        cleanup = cleanup_orphan_artifacts(audit)
        self._record_measurement(
            "artifact.cleanup.files",
            cleanup.removed_files,
            labels={"outcome": "removed"},
        )
        self._record_measurement(
            "artifact.cleanup.bytes",
            cleanup.removed_bytes,
            labels={"outcome": "removed"},
        )
        self._record_measurement(
            "artifact.cleanup.files",
            len(cleanup.failed_files),
            labels={"outcome": "failed"},
        )
        return {
            "removed_files": cleanup.removed_files,
            "removed_bytes": cleanup.removed_bytes,
            "failed_files": cleanup.failed_files,
        }

    def create_path_display_rule(
        self,
        *,
        source_pattern: str,
        display_pattern: str,
    ) -> PathDisplayRule:
        created = self.store.create_path_display_rule(
            source_pattern=source_pattern,
            display_pattern=display_pattern,
        )
        self._path_display_rules = tuple(
            self.store.list_path_display_rules()
        )
        return created

    def update_path_display_rule(
        self,
        rule_id: str,
        *,
        source_pattern: str,
        display_pattern: str,
    ) -> PathDisplayRule:
        updated = self.store.update_path_display_rule(
            rule_id,
            source_pattern=source_pattern,
            display_pattern=display_pattern,
        )
        self._path_display_rules = tuple(
            self.store.list_path_display_rules()
        )
        return updated

    def delete_path_display_rule(self, rule_id: str) -> None:
        self.store.delete_path_display_rule(rule_id)
        self._path_display_rules = tuple(
            self.store.list_path_display_rules()
        )

    def prompt_revision_choices(self) -> list[dict[str, Any]]:
        choices: list[dict[str, Any]] = []
        for category in self.active_prompt_categories():
            revisions = sorted(
                self.store.list_prompt_revisions(category.id),
                key=lambda revision: int(revision["revision_number"]),
                reverse=True,
            )
            for revision in revisions:
                is_active = revision["id"] == category.prompt_revision_id
                revision_number = int(revision["revision_number"])
                choices.append(
                    {
                        "id": (
                            category.id
                            if is_active
                            else f"{category.id}@{revision['id']}"
                        ),
                        "category_id": category.id,
                        "name": (
                            f"{category.name} · v{revision_number}"
                            + (" (현재)" if is_active else "")
                        ),
                        "revision_id": str(revision["id"]),
                        "revision_number": revision_number,
                        "is_active": is_active,
                    }
                )
        return choices

    def _prompt_snapshot(self, selection: str) -> dict[str, Any]:
        category_id, separator, revision_id = selection.strip().partition("@")
        category = self.store.get_prompt_category(category_id)
        if category is None or category.archived:
            raise ValueError("사용할 수 있는 번역 프롬프트를 선택하세요.")
        if separator:
            revision = self.store.get_prompt_revision(
                category.id,
                revision_id,
            )
            if revision is None:
                raise ValueError("사용할 수 있는 번역 프롬프트를 선택하세요.")
            selected_revision_id = str(revision["id"])
            selected_revision_number = int(revision["revision_number"])
            translation_prompt = str(revision["translation_prompt"])
            review_prompt = str(revision["review_prompt"])
        else:
            selected_revision_id = category.prompt_revision_id
            selected_revision_number = category.prompt_revision_number
            translation_prompt = category.translation_prompt
            review_prompt = category.review_prompt
        return {
            "category_id": category.id,
            "category_name": category.name,
            "revision_id": selected_revision_id,
            "revision_number": selected_revision_number,
            "translation_prompt": translation_prompt,
            "review_prompt": review_prompt,
            "review_rounds": TRANSLATION_REVIEW_ROUNDS,
        }

    @staticmethod
    def _legacy_prompt_snapshot() -> dict[str, Any]:
        return {
            "category_id": "jav",
            "category_name": "JAV (기존 작업)",
            "revision_id": None,
            "revision_number": None,
            "translation_prompt": KOREAN_JAV_SYSTEM_PROMPT,
            "review_prompt": KOREAN_TRANSLATION_REVIEW_PROMPT,
            "review_rounds": 0,
        }

    def update_remote_servers(
        self,
        settings: RemoteServerSettings,
    ) -> RemoteServerSettings:
        return self._set_remote_servers(settings, persist=True)

    def _set_remote_servers(
        self,
        settings: RemoteServerSettings,
        *,
        persist: bool,
    ) -> RemoteServerSettings:
        normalized = settings.normalized()
        previous = self.remote_servers
        translation_settings_changed = (
            normalized.lm_base_url,
            normalized.lm_token,
            normalized.lm_model,
        ) != (
            previous.lm_base_url,
            previous.lm_token,
            previous.lm_model,
        )
        if any(
            endpoint.base_url == normalized.stt_base_url
            for endpoint in self.store.list_runtime_endpoints()
        ):
            raise ValueError("외부 Runtime과 같은 주소를 기본값으로 설정할 수 없습니다.")
        stt_client = STTAPIClient(
            normalized.stt_base_url,
            normalized.stt_token,
            request_observer=self.record_external_request,
        )
        lm_client = (
            OpenAICompatibleClient(
                normalized.lm_base_url,
                normalized.lm_token,
                normalized.lm_model,
                max_segments=self.settings.translation_batch_segments,
                max_characters=self.settings.translation_batch_characters,
                request_limiter=self._translation_request_limiter,
                request_observer=self.record_external_request,
            )
            if normalized.translation_is_complete
            else None
        )
        if persist:
            self.store.save_remote_server_settings(
                stt_base_url=normalized.stt_base_url,
                stt_token=normalized.stt_token,
                lm_base_url=normalized.lm_base_url,
                lm_token=normalized.lm_token,
                lm_model=normalized.lm_model,
                translation_workers=normalized.translation_workers,
            )
        self._translation_request_limiter.set_limit(
            normalized.translation_workers
        )
        self._remote_runtime = (stt_client, lm_client, normalized)
        with self._runtime_lock:
            self._runtime_clients[BUILTIN_RUNTIME_ID] = stt_client
            if persist:
                self._runtime_health[BUILTIN_RUNTIME_ID] = {
                    "status": "unknown",
                    "message": None,
                    "readiness": None,
                    "checked_at": None,
                }
        if persist:
            self._refresh_stt_gate_from_pool()
        if persist and translation_settings_changed:
            self._set_translation_circuit("ready")
        return normalized

    def start(self) -> None:
        repaired_publications = self._reconcile_subtitle_publications()
        if repaired_publications:
            LOGGER.warning(
                "reconciled %d subtitle publication(s)",
                repaired_publications,
            )
        recovered = self._reconcile_interrupted_jobs()
        self._record_measurement(
            "recovery.pipeline_jobs",
            recovered,
        )
        if recovered:
            LOGGER.warning(
                "reconciled %d interrupted job(s) from persisted checkpoints",
                recovered,
            )
        translation_recovery = (
            self.store.reconcile_interrupted_translation_attempts()
        )
        self._record_measurement(
            "recovery.translation_generations",
            translation_recovery["generation_count"],
        )
        self._record_measurement(
            "recovery.translation_batches",
            translation_recovery["batch_count"],
        )
        if translation_recovery["generation_count"]:
            LOGGER.warning(
                "reconciled %d translation generation(s) and %d batch(es)",
                translation_recovery["generation_count"],
                translation_recovery["batch_count"],
            )
        for view in self.runtime_endpoints_view():
            if (
                not view["builtin"]
                and view["enabled"]
                and view["status"] == "unknown"
            ):
                self._runtime_probe_executor.submit(
                    self.probe_runtime_endpoint,
                    str(view["id"]),
                )
        self._scheduler.start()

    def _reconcile_interrupted_jobs(self) -> int:
        interrupted = self.store.recoverable_running_jobs(RUNNING_STATUSES)
        recovered = 0
        for job in interrupted:
            lease_token = self.store.claim_recovery_lease(
                job.id,
                job.status,
                lease_owner=self._worker_id,
                lease_seconds=JOB_LEASE_SECONDS,
            )
            if lease_token is None:
                continue
            claimed_job = self.store.get(job.id)
            if claimed_job is None:
                continue
            job = claimed_job
            recovered += 1
            stage = RUNNING_STAGE_BY_STATUS[job.status]
            if job.job_stop_requested:
                if (
                    job.status == "transcription_running"
                    and job.stt_job_id
                ):
                    self.store.add_event(
                        job.id,
                        "warning",
                        "service restart detected; confirming requested "
                        "remote transcription cancellation",
                        event_code="recovery.stop_confirmation",
                        from_state=job.state,
                        phase="transcription",
                        payload={
                            "remote_job_id": job.stt_job_id,
                            "runtime_id": (
                                job.stt_runtime_id or BUILTIN_RUNTIME_ID
                            ),
                        },
                    )
                    self._submit_stage(
                        self._stt_executor,
                        job.id,
                        "transcription",
                        self._cancel_interrupted_transcription,
                        lease_token,
                    )
                else:
                    self._mark_job_stopped(job, stage)
                    self.store.release_job_lease(
                        job.id,
                        lease_owner=self._worker_id,
                        lease_token=lease_token,
                    )
                continue

            if (
                job.status == "transcription_running"
                and job.stt_job_id
                and job.audio_path
                and Path(job.audio_path).is_file()
            ):
                self.store.add_event(
                    job.id,
                    "warning",
                    "service restart detected; reconnecting remote "
                    f"transcription {job.stt_job_id}",
                    event_code="transcription.reconnected",
                    from_state=job.state,
                    phase="transcription",
                    payload={
                        "remote_job_id": job.stt_job_id,
                        "runtime_id": job.stt_runtime_id or BUILTIN_RUNTIME_ID,
                    },
                )
                self._submit_stage(
                    self._stt_executor,
                    job.id,
                    "transcription",
                    self._transcribe,
                    lease_token,
                )
                continue

            if job.status == "extracting":
                target_status = "queued"
            elif job.status == "translation_running":
                target_status = self._restart_checkpoint_status(
                    job,
                    include_translation=False,
                )
            elif job.status == "rendering":
                target_status = self._restart_checkpoint_status(
                    job,
                    include_translation=True,
                )
            else:
                target_status = self._restart_checkpoint_status(
                    job,
                    include_translation=False,
                )

            fields: dict[str, Any] = {
                "status": target_status,
                "attempt": job.attempt + 1,
                "blocked_stage": None,
                "error": None,
                "job_stop_requested": 0,
            }
            if target_status != "translation_paused":
                fields["translation_pause_requested"] = 0
            if self.store.update_if_lease(
                job.id,
                lease_owner=self._worker_id,
                lease_token=lease_token,
                **fields,
            ):
                self.store.add_event(
                    job.id,
                    "warning",
                    "service restart detected; resumed from persisted "
                    f"checkpoint {target_status}",
                    event_code="recovery.checkpoint_resumed",
                    from_state=job.state,
                    attempt=job.attempt + 1,
                    payload={
                        "previous_status": job.status,
                        "target_status": target_status,
                    },
                )
                self.store.release_job_lease(
                    job.id,
                    lease_owner=self._worker_id,
                    lease_token=lease_token,
                )
        return recovered

    def _cancel_interrupted_transcription(self, job: PipelineJob) -> None:
        stt_client = self._stt_client_for_job(job)
        if stt_client is None or not job.stt_job_id:
            raise ExternalServiceError(
                "transcription server is not configured for cancellation"
            )
        stt_client.cancel_job_and_wait(job.stt_job_id)
        raise OperationStopped("remote transcription cancellation confirmed")

    def _restart_checkpoint_status(
        self,
        job: PipelineJob,
        *,
        include_translation: bool,
    ) -> str:
        segments: list[dict[str, Any]] | None = None
        if job.transcript_path and Path(job.transcript_path).is_file():
            try:
                transcript_payload = json.loads(
                    Path(job.transcript_path).read_text(encoding="utf-8")
                )
                segments = validate_transcript(transcript_payload)
            except (OSError, ValueError, json.JSONDecodeError):
                segments = None
        if segments is None:
            if job.audio_path and Path(job.audio_path).is_file():
                return "audio_ready"
            return "queued"
        if job.operation == "transcribe":
            return "transcription_completed"
        if (
            include_translation
            and job.translation_path
            and Path(job.translation_path).is_file()
        ):
            try:
                translation_payload = json.loads(
                    Path(job.translation_path).read_text(encoding="utf-8")
                )
                if not isinstance(translation_payload, Mapping):
                    raise ValueError(
                        "translation JSON document must be an object"
                    )
                validate_translation_items(
                    translation_payload.get("translations"),
                    [str(segment["id"]) for segment in segments],
                )
                return "translated"
            except (OSError, ValueError, json.JSONDecodeError):
                pass
        if job.translation_pause_requested:
            return "translation_paused"
        return "transcribed"

    def stop(
        self,
        *,
        grace_seconds: float = JOB_SHUTDOWN_GRACE_SECONDS,
    ) -> None:
        if grace_seconds < 0:
            raise ValueError("shutdown grace seconds cannot be negative")
        self._stop_event.set()
        if self._scheduler.is_alive():
            self._scheduler.join(timeout=5)
        for executor in (
            self._audio_executor,
            self._render_executor,
            self._stt_executor,
            self._runtime_probe_executor,
            self._translation_executor,
        ):
            executor.shutdown(wait=False, cancel_futures=True)
        with self._stage_futures_lock:
            active = set(self._stage_futures)
        if active:
            _done, pending = wait(active, timeout=grace_seconds)
            if pending:
                LOGGER.warning(
                    "shutdown grace expired with %d pipeline stage(s) active",
                    len(pending),
                )

    def create_job(
        self,
        source_rel: str,
        *,
        force_overwrite: bool,
        options: Mapping[str, Any],
        operation: str = "full",
        prompt_category_id: str | None = None,
    ) -> PipelineJob:
        return self.create_jobs(
            [source_rel],
            force_overwrite=force_overwrite,
            options=options,
            operation=operation,
            prompt_category_id=prompt_category_id,
        )[0]

    def expand_job_sources(
        self,
        source_rels: Sequence[str],
        folder_rels: Sequence[str],
        *,
        force_overwrite: bool,
        operation: str,
    ) -> tuple[list[str], int]:
        recursive = self.library.list_media_recursive(folder_rels)
        candidates = list(dict.fromkeys([*source_rels, *recursive]))
        if not candidates:
            raise ValueError("작업할 미디어 파일이나 폴더를 하나 이상 선택하세요.")
        if len(candidates) > self.settings.maximum_listed_files:
            raise ValueError("한 번에 등록할 수 있는 파일 수를 초과했습니다.")

        latest_jobs = self.store.latest_jobs_by_source()
        selected: list[str] = []
        skipped = 0
        for source_rel in candidates:
            source = self.library.resolve_file(source_rel)
            latest = latest_jobs.get(source_rel)
            if latest is not None and latest.status not in SUCCESS_STATUSES:
                skipped += 1
                continue
            # A comparison is an explicitly repeatable, transcription-only
            # operation. Completed jobs and rendered subtitles do not collide
            # with its per-job artifacts, so they must not filter the source.
            if operation == "compare":
                selected.append(source_rel)
                continue
            has_subtitle = any(
                path.exists()
                for path in (
                    source.with_name(f"{source.stem}.ko.srt"),
                    source.with_name(f"{source.stem}.ko.ass"),
                )
            )
            if (
                _operation_is_completed(
                    latest,
                    operation,
                    has_subtitle=has_subtitle,
                )
                and not force_overwrite
            ):
                skipped += 1
                continue
            selected.append(source_rel)
        if not selected:
            raise ValueError(
                "선택한 범위에 새로 등록할 수 있는 미디어가 없습니다."
            )
        return selected, skipped

    def create_jobs(
        self,
        source_rels: Sequence[str],
        *,
        force_overwrite: bool,
        options: Mapping[str, Any],
        operation: str = "full",
        prompt_category_id: str | None = None,
    ) -> list[PipelineJob]:
        if operation not in SUPPORTED_OPERATIONS:
            raise ValueError("unsupported job operation")
        if operation in {"transcribe", "full"} and not (
            self.transcription_server_configured
        ):
            raise ValueError("전사 서버 설정이 필요합니다.")
        if operation in TRANSLATION_OPERATIONS and not (
            self.translation_server_configured
        ):
            raise ValueError("번역 서버 설정이 필요합니다.")
        unique_source_rels = list(dict.fromkeys(source_rels))
        if not unique_source_rels:
            raise ValueError("작업할 미디어 파일을 하나 이상 선택하세요.")
        if len(unique_source_rels) > self.settings.maximum_listed_files:
            raise ValueError("한 번에 등록할 수 있는 파일 수를 초과했습니다.")

        normalized_options = self._normalize_options(options)
        if operation in TRANSLATION_OPERATIONS:
            if prompt_category_id:
                normalized_options[TRANSLATION_PROMPT_OPTION] = (
                    self._prompt_snapshot(prompt_category_id)
                )
            else:
                existing_snapshot = options.get(TRANSLATION_PROMPT_OPTION)
                normalized_options[TRANSLATION_PROMPT_OPTION] = (
                    dict(existing_snapshot)
                    if isinstance(existing_snapshot, Mapping)
                    else self._legacy_prompt_snapshot()
                )
        for source_rel in unique_source_rels:
            source = self.library.resolve_file(source_rel)
            existing_subtitles = [
                path
                for path in (
                    source.with_name(f"{source.stem}.ko.srt"),
                    source.with_name(f"{source.stem}.ko.ass"),
                )
                if path.exists()
            ]
            if (
                operation in TRANSLATION_OPERATIONS
                and existing_subtitles
                and not force_overwrite
            ):
                raise FileExistsError(
                    f"{source_rel}: 기존 한국어 자막 파일이 있습니다. "
                    "덮어쓰기를 명시적으로 선택하세요."
                )

        reusable_transcripts = (
            {
                source_rel: self._reusable_transcript(source_rel)
                for source_rel in unique_source_rels
            }
            if operation == "translate"
            else {}
        )
        latest_jobs = (
            self.store.latest_jobs_by_source()
            if operation in {"transcribe", "translate"}
            else {}
        )
        reusable_audio_jobs: dict[str, PipelineJob] = {}
        if operation == "transcribe":
            for source_rel in unique_source_rels:
                reusable_audio = self.store.latest_audio_job(source_rel)
                latest = latest_jobs.get(source_rel)
                if (
                    reusable_audio is not None
                    and latest is not None
                    and latest.id == reusable_audio.id
                ):
                    reusable_audio_jobs[source_rel] = reusable_audio

        jobs: list[PipelineJob] = []
        for source_rel in unique_source_rels:
            reusable_audio = reusable_audio_jobs.get(source_rel)
            if reusable_audio is not None:
                audio_available = bool(
                    reusable_audio.audio_path
                    and Path(reusable_audio.audio_path).is_file()
                )
                resumed_options = dict(normalized_options)
                if audio_available:
                    for key in (
                        "audio_stream",
                        "start_seconds",
                        "duration_seconds",
                    ):
                        if key in reusable_audio.options:
                            resumed_options[key] = reusable_audio.options[key]
                chunk_estimate = (
                    estimate_transcription_chunks(
                        wav_duration_seconds(Path(reusable_audio.audio_path)),
                        resumed_options,
                    )
                    if audio_available and reusable_audio.audio_path
                    else 0
                )
                self.store.update(
                    reusable_audio.id,
                    status="audio_ready" if audio_available else "queued",
                    force_overwrite=int(force_overwrite),
                    operation="transcribe",
                    options_json=json.dumps(resumed_options, sort_keys=True),
                    audio_path=(
                        reusable_audio.audio_path if audio_available else None
                    ),
                    audio_sha256=(
                        reusable_audio.audio_sha256 if audio_available else None
                    ),
                    audio_revision_id=(
                        reusable_audio.audio_revision_id
                        if audio_available
                        else None
                    ),
                    stt_job_id=None,
                    stt_runtime_id=None,
                    transcript_path=None,
                    transcript_revision_id=None,
                    translation_path=None,
                    srt_path=None,
                    ass_path=None,
                    blocked_stage=None,
                    error=None,
                    chunks_created=0,
                    chunks_completed=0,
                    chunks_total_estimate=chunk_estimate,
                    transcription_stage=None,
                    transcription_stage_index=0,
                    transcription_stage_total=0,
                    translation_chunks_total=0,
                    translation_chunks_completed=0,
                    translation_pause_requested=0,
                    job_stop_requested=0,
                )
                self.store.add_event(
                    reusable_audio.id,
                    "info",
                    "transcription requested; reusing extracted audio"
                    if audio_available
                    else "transcription requested; audio will be extracted "
                    "again",
                )
                resumed = self.store.get(reusable_audio.id)
                if resumed is None:
                    raise RuntimeError(
                        "resumed transcription job could not be read"
                    )
                jobs.append(resumed)
                continue
            reusable_transcript = reusable_transcripts.get(source_rel)
            if reusable_transcript is not None:
                reusable, transcript_payload = reusable_transcript
                latest = latest_jobs.get(source_rel)
                if (
                    reusable.status == "transcription_completed"
                    and latest is not None
                    and latest.id == reusable.id
                    and not reusable.options.get("comparison_id")
                ):
                    jobs.append(
                        self._continue_completed_transcription(
                            reusable,
                            prompt_snapshot=normalized_options[
                                TRANSLATION_PROMPT_OPTION
                            ],
                            force_overwrite=force_overwrite,
                            event_message=(
                                "translation requested; continuing completed "
                                "transcription in the same job"
                            ),
                        )
                    )
                    continue
                reusable_options = dict(reusable.options)
                reusable_options[TRANSLATION_PROMPT_OPTION] = (
                    normalized_options[TRANSLATION_PROMPT_OPTION]
                )
                created = self.store.create(
                    job_id=uuid4().hex,
                    source_rel=source_rel,
                    force_overwrite=force_overwrite,
                    options=reusable_options,
                    operation="translate",
                    audio_path=reusable.audio_path,
                    audio_sha256=reusable.audio_sha256,
                    audio_revision_id=reusable.audio_revision_id,
                )
                revision_id = uuid4().hex
                transcript_payload["revision"] = {
                    "id": revision_id,
                    "source_revision_id": reusable.transcript_revision_id,
                    "origin": "imported",
                }
                transcript_path = (
                    self.settings.jobs_dir
                    / created.id
                    / "transcript-revisions"
                    / revision_id
                    / artifact_filename(source_rel, "transcript")
                )
                write_json_atomic(transcript_path, transcript_payload)
                persisted = self.store.record_transcript_revision(
                    revision_id=revision_id,
                    job_id=created.id,
                    audio_revision_id=reusable.audio_revision_id,
                    remote_job_id=reusable.stt_job_id,
                    backend=str(reusable.options.get("backend", "imported")),
                    model_revision="imported",
                    options_hash=_canonical_payload_hash(reusable.options),
                    artifact_path=str(transcript_path),
                    content_hash=sha256_file(transcript_path),
                    origin="imported",
                    status="transcribed",
                    chunks_total=len(transcript_payload["segments"]),
                )
                if not persisted:
                    raise RuntimeError(
                        "imported transcript revision was not saved"
                    )
                self.store.add_event(
                    created.id,
                    "info",
                    "translation requested; reusing validated transcript",
                )
                refreshed = self.store.get(created.id)
                if refreshed is None:
                    raise RuntimeError("translation job could not be read")
                jobs.append(refreshed)
                continue
            jobs.append(
                self.store.create(
                    job_id=uuid4().hex,
                    source_rel=source_rel,
                    force_overwrite=force_overwrite,
                    options=normalized_options,
                    operation=operation,
                )
            )
        return jobs

    def create_transcription_comparison(
        self,
        source_rels: Sequence[str],
        *,
        options: Mapping[str, Any],
        reuse_audio_from: Sequence[PipelineJob] = (),
        parent_comparison_id: str | None = None,
    ) -> tuple[str, list[PipelineJob]]:
        """Queue one transcription-only job per engine and source."""
        if not self.transcription_server_configured:
            raise ValueError("전사 서버 설정이 필요합니다.")
        unique_source_rels = list(dict.fromkeys(source_rels))
        if not unique_source_rels:
            raise ValueError("비교할 미디어 파일을 하나 이상 선택하세요.")
        if len(unique_source_rels) > MAX_TRANSCRIPTION_COMPARISON_SOURCES:
            raise ValueError(
                "전사 비교는 한 번에 최대 "
                f"{MAX_TRANSCRIPTION_COMPARISON_SOURCES}개 파일까지 가능합니다."
            )
        for source_rel in unique_source_rels:
            self.library.resolve_file(source_rel)

        comparison_chunks = HybridRescueOptions.from_options(options)
        normalized_by_backend: dict[str, dict[str, Any]] = {}
        for backend in TRANSCRIPTION_COMPARISON_BACKENDS:
            backend_options = dict(options)
            backend_options["backend"] = backend
            if backend == "kotoba":
                backend_options["chunk_length_seconds"] = (
                    comparison_chunks.kotoba_chunk_length_seconds
                )
            elif backend == "whisperx":
                backend_options["chunk_length_seconds"] = (
                    comparison_chunks.whisperx_chunk_length_seconds
                )
            if backend != "hybrid":
                backend_options.pop("hybrid_rescue", None)
            if backend != "whisperjav":
                backend_options.pop("whisperjav", None)
            if backend == "whisperjav":
                for key in ("repetition_policy", "repetition_min_count"):
                    backend_options.pop(key, None)
            if backend == "kotoba":
                for key in (
                    "subtitle_segmentation",
                    "repetition_policy",
                    "repetition_min_count",
                ):
                    backend_options.pop(key, None)
            normalized_by_backend[backend] = self._normalize_options(
                backend_options
            )

        desired_audio_signature = _audio_extraction_signature(
            normalized_by_backend[TRANSCRIPTION_COMPARISON_BACKENDS[0]]
        )
        reusable_audio_by_source: dict[str, PipelineJob] = {}
        for candidate in sorted(
            reuse_audio_from,
            key=lambda job: (job.updated_at, job.created_at),
            reverse=True,
        ):
            if candidate.source_rel in reusable_audio_by_source:
                continue
            if candidate.source_rel not in unique_source_rels:
                continue
            if _audio_extraction_signature(
                candidate.options
            ) != desired_audio_signature:
                continue
            if not candidate.audio_path or not Path(candidate.audio_path).is_file():
                continue
            reusable_audio_by_source[candidate.source_rel] = candidate

        comparison_id = uuid4().hex
        normalized_parent_id = str(parent_comparison_id or "").strip()
        duration_by_audio_path: dict[str, float | None] = {}
        jobs: list[PipelineJob] = []
        for source_rel in unique_source_rels:
            reusable_audio = reusable_audio_by_source.get(source_rel)
            for backend in TRANSCRIPTION_COMPARISON_BACKENDS:
                persisted_options = dict(normalized_by_backend[backend])
                persisted_options["comparison_id"] = comparison_id
                persisted_options["comparison_schema_version"] = (
                    TRANSCRIPTION_COMPARISON_SCHEMA_VERSION
                )
                persisted_options["comparison_backends"] = list(
                    TRANSCRIPTION_COMPARISON_BACKENDS
                )
                if normalized_parent_id:
                    persisted_options[COMPARISON_PARENT_ID_OPTION] = (
                        normalized_parent_id
                    )
                if reusable_audio is not None:
                    persisted_options[COMPARISON_AUDIO_SOURCE_JOB_ID_OPTION] = (
                        reusable_audio.id
                    )
                    audio_path = str(reusable_audio.audio_path)
                    if audio_path not in duration_by_audio_path:
                        duration_by_audio_path[audio_path] = wav_duration_seconds(
                            Path(audio_path)
                        )
                    chunk_estimate = estimate_transcription_chunks(
                        duration_by_audio_path[audio_path],
                        persisted_options,
                    )
                else:
                    audio_path = None
                    chunk_estimate = 0
                created = self.store.create(
                    job_id=uuid4().hex,
                    source_rel=source_rel,
                    force_overwrite=False,
                    options=persisted_options,
                    operation="transcribe",
                    status=(
                        "audio_ready" if reusable_audio is not None else "queued"
                    ),
                    audio_path=audio_path,
                    audio_sha256=(
                        reusable_audio.audio_sha256
                        if reusable_audio is not None
                        else None
                    ),
                    audio_revision_id=(
                        reusable_audio.audio_revision_id
                        if reusable_audio is not None
                        else None
                    ),
                    chunks_total_estimate=chunk_estimate,
                )
                if reusable_audio is not None:
                    self.store.add_event(
                        created.id,
                        "info",
                        "comparison rerun requested; reusing extracted audio "
                        f"from job {reusable_audio.id}",
                    )
                jobs.append(created)
        return comparison_id, jobs

    def create_selected_translation_jobs(
        self,
        job_ids: Sequence[str],
        *,
        prompt_category_id: str,
    ) -> list[PipelineJob]:
        """Move selected, latest completed transcripts into translation."""
        if not self.translation_server_configured:
            raise ValueError("번역 서버 설정이 필요합니다.")
        selected_ids = list(
            dict.fromkeys(job_id.strip() for job_id in job_ids if job_id.strip())
        )
        if not selected_ids:
            raise ValueError("번역할 전사 완료 작업을 하나 이상 선택하세요.")
        if len(selected_ids) > self.settings.maximum_listed_files:
            raise ValueError("한 번에 등록할 수 있는 파일 수를 초과했습니다.")

        prompt_snapshot = self._prompt_snapshot(prompt_category_id)
        latest_jobs = self.store.latest_jobs_by_source()
        reusable_transcripts: list[PipelineJob] = []
        for job_id in selected_ids:
            job = self.store.get(job_id)
            if job is None:
                raise ValueError("선택한 작업을 찾을 수 없습니다.")
            latest = latest_jobs.get(job.source_rel)
            if (
                not job.can_start_translation
                or latest is None
                or latest.id != job.id
            ):
                raise ValueError(
                    f"{job.source_rel}: 최신 전사 완료 작업만 번역할 수 있습니다."
                )
            if job.options.get("comparison_id"):
                raise ValueError(
                    f"{job.source_rel}: 전사 비교 결과는 비교 상세 화면에서 "
                    "번역할 결과를 선택하세요."
                )
            self.library.resolve_file(job.source_rel)
            transcript_path = Path(job.transcript_path or "")
            try:
                transcript_payload = json.loads(
                    transcript_path.read_text(encoding="utf-8")
                )
                if not isinstance(transcript_payload, Mapping):
                    raise ValueError(
                        "transcript JSON document must be an object"
                    )
                validate_transcript(transcript_payload)
            except (
                OSError,
                UnicodeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                raise ValueError(
                    f"{job.source_rel}: 번역에 사용할 유효한 전사 결과가 "
                    "없습니다."
                ) from error
            reusable_transcripts.append(job)

        transitioned_jobs: list[PipelineJob] = []
        for reusable in reusable_transcripts:
            transitioned_jobs.append(
                self._continue_completed_transcription(
                    reusable,
                    prompt_snapshot=prompt_snapshot,
                    force_overwrite=True,
                    event_message=(
                        "selected completed transcription continued in "
                        "translation queue"
                    ),
                )
            )
        return transitioned_jobs

    def _continue_completed_transcription(
        self,
        job: PipelineJob,
        *,
        prompt_snapshot: Mapping[str, Any],
        force_overwrite: bool,
        event_message: str,
    ) -> PipelineJob:
        """Continue translation in a completed transcription's job record."""
        options = dict(job.options)
        options[TRANSLATION_PROMPT_OPTION] = dict(prompt_snapshot)
        transitioned = self.store.update_if_status(
            job.id,
            {"transcription_completed"},
            status="transcribed",
            force_overwrite=force_overwrite,
            operation="full",
            options_json=json.dumps(options, sort_keys=True),
            translation_path=None,
            srt_path=None,
            ass_path=None,
            blocked_stage=None,
            error=None,
            translation_chunks_total=0,
            translation_chunks_completed=0,
            translation_pause_requested=0,
            job_stop_requested=0,
        )
        if not transitioned:
            raise ValueError(
                f"{job.source_rel}: 전사 완료 상태가 변경되어 "
                "번역으로 전환하지 못했습니다."
            )
        self.store.add_event(job.id, "info", event_message)
        refreshed = self.store.get(job.id)
        if refreshed is None:
            raise RuntimeError("continued translation job could not be read")
        return refreshed

    def create_comparison_translation_jobs(
        self,
        comparison_id: str,
        job_ids: Sequence[str],
        *,
        prompt_category_id: str,
    ) -> list[PipelineJob]:
        """Create translation jobs from selected comparison transcripts."""
        if not self.translation_server_configured:
            raise ValueError("번역 서버 설정이 필요합니다.")
        normalized_comparison_id = comparison_id.strip()
        selected_ids = list(
            dict.fromkeys(
                job_id.strip() for job_id in job_ids if job_id.strip()
            )
        )
        if not selected_ids:
            raise ValueError("번역에 사용할 전사 결과를 하나 이상 선택하세요.")
        if len(selected_ids) > self.settings.maximum_listed_files:
            raise ValueError("한 번에 등록할 수 있는 파일 수를 초과했습니다.")

        prompt_snapshot = self._prompt_snapshot(prompt_category_id)
        selected_transcripts: list[tuple[PipelineJob, dict[str, Any]]] = []
        selected_sources: set[str] = set()
        for job_id in selected_ids:
            job = self.store.get(job_id)
            if job is None or str(
                job.options.get("comparison_id", "")
            ).strip() != normalized_comparison_id:
                raise ValueError("선택한 전사 비교 결과를 찾을 수 없습니다.")
            if job.source_rel in selected_sources:
                raise ValueError(
                    f"{job.source_rel}: 파일마다 하나의 전사 결과만 "
                    "선택하세요."
                )
            if not job.can_start_translation:
                raise ValueError(
                    f"{job.source_rel}: 완료된 전사 결과만 번역할 수 "
                    "있습니다."
                )
            self.library.resolve_file(job.source_rel)
            transcript_path = Path(job.transcript_path or "")
            try:
                payload = json.loads(
                    transcript_path.read_text(encoding="utf-8")
                )
                if not isinstance(payload, Mapping):
                    raise ValueError(
                        "transcript JSON document must be an object"
                    )
                validate_transcript(payload)
            except (
                OSError,
                UnicodeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                raise ValueError(
                    f"{job.source_rel}: 번역에 사용할 유효한 전사 결과가 "
                    "없습니다."
                ) from error
            normalized_payload = dict(payload)
            normalized_payload.setdefault(
                "job_id",
                job.stt_job_id or job.id,
            )
            selected_sources.add(job.source_rel)
            selected_transcripts.append((job, normalized_payload))

        created_jobs: list[PipelineJob] = []
        comparison_option_keys = {
            "comparison_id",
            "comparison_schema_version",
            "comparison_backends",
            COMPARISON_PARENT_ID_OPTION,
            COMPARISON_AUDIO_SOURCE_JOB_ID_OPTION,
        }
        for reusable, transcript_payload in selected_transcripts:
            options = {
                key: value
                for key, value in reusable.options.items()
                if key not in comparison_option_keys
            }
            options[TRANSLATION_PROMPT_OPTION] = dict(prompt_snapshot)
            options["comparison_transcript_source"] = {
                "comparison_id": normalized_comparison_id,
                "job_id": reusable.id,
                "backend": str(reusable.options.get("backend", "")),
            }
            created = self.store.create(
                job_id=uuid4().hex,
                source_rel=reusable.source_rel,
                force_overwrite=True,
                options=options,
                operation="translate",
                audio_path=reusable.audio_path,
                audio_sha256=reusable.audio_sha256,
                audio_revision_id=reusable.audio_revision_id,
            )
            revision_id = uuid4().hex
            transcript_payload["revision"] = {
                "id": revision_id,
                "source_revision_id": reusable.transcript_revision_id,
                "origin": "imported",
            }
            transcript_path = (
                self.settings.jobs_dir
                / created.id
                / "transcript-revisions"
                / revision_id
                / artifact_filename(created.source_rel, "transcript")
            )
            write_json_atomic(transcript_path, transcript_payload)
            persisted = self.store.record_transcript_revision(
                revision_id=revision_id,
                job_id=created.id,
                audio_revision_id=reusable.audio_revision_id,
                remote_job_id=reusable.stt_job_id,
                backend=str(reusable.options.get("backend", "imported")),
                model_revision="imported",
                options_hash=_canonical_payload_hash(reusable.options),
                artifact_path=str(transcript_path),
                content_hash=sha256_file(transcript_path),
                origin="imported",
                status="transcribed",
                chunks_total=len(transcript_payload["segments"]),
            )
            if not persisted:
                raise RuntimeError("imported transcript revision was not saved")
            self.store.add_event(
                created.id,
                "info",
                "translation requested from comparison transcript "
                f"{reusable.id}",
            )
            refreshed = self.store.get(created.id)
            if refreshed is None:
                raise RuntimeError(
                    "comparison translation job could not be read"
                )
            created_jobs.append(refreshed)
        return created_jobs

    def _reusable_transcript(
        self,
        source_rel: str,
    ) -> tuple[PipelineJob, dict[str, Any]]:
        reusable = self.store.latest_transcript_job(source_rel)
        message = (
            f"{source_rel}: 번역에 사용할 유효한 전사 결과가 없습니다. "
            "먼저 전사를 실행하세요."
        )
        if reusable is None or not reusable.transcript_path:
            raise ValueError(message)
        transcript_path = Path(reusable.transcript_path)
        if not transcript_path.is_file():
            raise ValueError(message)
        try:
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("transcript JSON document must be an object")
            validate_transcript(payload)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(message) from error
        return reusable, dict(payload)

    def _normalize_options(
        self,
        options: Mapping[str, Any],
    ) -> dict[str, Any]:
        backend = str(options.get("backend", "kotoba")).strip().lower()
        if backend not in SUPPORTED_STT_BACKENDS:
            supported = ", ".join(sorted(SUPPORTED_STT_BACKENDS))
            raise ValueError(f"backend must be one of: {supported}")

        raw_duration = options.get("duration_seconds")
        parsed_duration = (
            float(raw_duration)
            if raw_duration not in (None, "")
            else None
        )
        extraction = AudioExtraction(
            audio_stream=int(options.get("audio_stream", 0)),
            start_seconds=float(options.get("start_seconds", 0.0)),
            duration_seconds=(
                parsed_duration
                if parsed_duration not in (None, 0.0)
                else None
            ),
        )
        transcription = TranscriptionOptions(
            chunk_length_seconds=int(
                options.get(
                    "chunk_length_seconds",
                    (
                        WHISPERX_MAX_CHUNK_LENGTH_SECONDS
                        if backend == "whisperx"
                        else DEFAULT_CHUNK_LENGTH_SECONDS
                    ),
                )
            ),
            num_speakers=(
                int(options["num_speakers"])
                if options.get("num_speakers") not in (None, "")
                else None
            ),
            min_speakers=(
                int(options["min_speakers"])
                if options.get("min_speakers") not in (None, "")
                else None
            ),
            max_speakers=(
                int(options["max_speakers"])
                if options.get("max_speakers") not in (None, "")
                else None
            ),
            add_punctuation=bool(options.get("add_punctuation", False)),
            noise_filter=bool(options.get("noise_filter", True)),
        )
        extraction.validate()
        transcription.validate()
        if (
            backend == "whisperx"
            and transcription.chunk_length_seconds
            > WHISPERX_MAX_CHUNK_LENGTH_SECONDS
        ):
            raise ValueError(
                "WhisperX chunk_length_seconds must be at most "
                f"{WHISPERX_MAX_CHUNK_LENGTH_SECONDS}"
            )
        if (
            backend in {"hybrid", "whisperjav", "whisperx"}
            and not transcription.noise_filter
        ):
            raise ValueError(
                f"{backend} backend requires noise_filter=true for VAD"
            )
        if backend == "whisperx" and "hybrid_rescue" in options:
            raise ValueError("hybrid_rescue requires backend='hybrid'")
        if backend == "kotoba" and any(
            key in options
            for key in (
                "subtitle_segmentation",
                "repetition_policy",
                "repetition_min_count",
                "hybrid_rescue",
                "whisperjav",
            )
        ):
            raise ValueError(
                "WhisperX quality options require backend='whisperx' or "
                "'hybrid'"
            )
        normalized_options = {
            "backend": backend,
            "audio_stream": extraction.audio_stream,
            "start_seconds": extraction.start_seconds,
            "duration_seconds": extraction.duration_seconds,
            "chunk_length_seconds": transcription.chunk_length_seconds,
            "num_speakers": transcription.num_speakers,
            "min_speakers": transcription.min_speakers,
            "max_speakers": transcription.max_speakers,
            "add_punctuation": transcription.add_punctuation,
            "noise_filter": transcription.noise_filter,
        }
        raw_batch_size = options.get("batch_size")
        if raw_batch_size not in (None, ""):
            if backend not in {"hybrid", "whisperx"}:
                raise ValueError(
                    "batch_size requires backend='whisperx' or 'hybrid'"
                )
            batch_size = int(raw_batch_size)
            if not (
                WHISPERX_MIN_BATCH_SIZE
                <= batch_size
                <= WHISPERX_MAX_BATCH_SIZE
            ):
                raise ValueError(
                    "batch_size must be between "
                    f"{WHISPERX_MIN_BATCH_SIZE} and {WHISPERX_MAX_BATCH_SIZE}"
                )
            normalized_options["batch_size"] = batch_size
        raw_segmentation = options.get("subtitle_segmentation")
        if raw_segmentation is not None and not isinstance(
            raw_segmentation,
            Mapping,
        ):
            raise ValueError("subtitle_segmentation must be an object")
        if backend == "hybrid":
            if "whisperjav" in options:
                raise ValueError("whisperjav options require backend='whisperjav'")
            segmentation = asdict(
                WhisperXSegmentationOptions.from_options(
                    options,
                    defaults=DEFAULT_SUBTITLE_SEGMENTATION,
                )
            )

            rescue = asdict(
                HybridRescueOptions.from_options(options)
            )
            kotoba_chunk_length = rescue["kotoba_chunk_length_seconds"]
            repetition_policy = str(
                options.get("repetition_policy", "flag")
            ).strip().lower()
            if repetition_policy != "flag":
                raise ValueError(
                    "hybrid backend requires repetition_policy='flag'"
                )
            repetition_min_count = int(
                options.get("repetition_min_count", 8)
            )
            if repetition_min_count < 2:
                raise ValueError(
                    "repetition_min_count must be at least 2"
                )

            normalized_options.update(
                {
                    "chunk_length_seconds": kotoba_chunk_length,
                    "subtitle_segmentation": segmentation,
                    "repetition_policy": repetition_policy,
                    "repetition_min_count": repetition_min_count,
                    "hybrid_rescue": rescue,
                }
            )
        elif backend == "whisperx":
            if "whisperjav" in options:
                raise ValueError("whisperjav options require backend='whisperjav'")
            if isinstance(raw_segmentation, Mapping):
                normalized_options["subtitle_segmentation"] = asdict(
                    WhisperXSegmentationOptions.from_options(
                        {"subtitle_segmentation": raw_segmentation}
                    )
                )
            repetition_policy = str(
                options.get("repetition_policy", "flag")
            ).strip().lower()
            if repetition_policy not in {"flag", "reject"}:
                raise ValueError(
                    "repetition_policy must be 'flag' or 'reject'"
                )
            repetition_min_count = int(options.get("repetition_min_count", 8))
            if repetition_min_count < 2:
                raise ValueError(
                    "repetition_min_count must be at least 2"
                )
            normalized_options["repetition_policy"] = repetition_policy
            normalized_options["repetition_min_count"] = repetition_min_count
        elif backend == "whisperjav":
            forbidden = {
                "repetition_policy",
                "repetition_min_count",
                "hybrid_rescue",
            } & set(options)
            if forbidden:
                raise ValueError(
                    "unsupported WhisperJAV quality options: "
                    f"{sorted(forbidden)}"
                )
            normalized_options["subtitle_segmentation"] = asdict(
                WhisperXSegmentationOptions.from_options(
                    options,
                    defaults=DEFAULT_SUBTITLE_SEGMENTATION,
                )
            )
            normalized_options["whisperjav"] = asdict(
                WhisperJAVOptions.from_options(options)
            )
        elif "whisperjav" in options:
            raise ValueError("whisperjav options require backend='whisperjav'")
        return normalized_options

    def retry(self, job_id: str) -> PipelineJob:
        job = self.store.get(job_id)
        if job is None:
            raise ValueError("job not found")
        if job.status not in RETRYABLE_STATUSES:
            raise ValueError("only blocked or failed jobs can be retried")

        target_status = "queued"
        transcript_segments: list[dict[str, Any]] | None = None
        if job.transcript_path and Path(job.transcript_path).is_file():
            try:
                payload = json.loads(
                    Path(job.transcript_path).read_text(encoding="utf-8")
                )
                transcript_segments = validate_transcript(payload)
                target_status = (
                    "transcription_completed"
                    if job.operation == "transcribe"
                    else "transcribed"
                )
            except (OSError, ValueError, json.JSONDecodeError):
                transcript_segments = None

        if (
            job.operation != "transcribe"
            and transcript_segments is not None
            and job.translation_path
            and Path(job.translation_path).is_file()
        ):
            try:
                translation_payload = json.loads(
                    Path(job.translation_path).read_text(encoding="utf-8")
                )
                if not isinstance(translation_payload, Mapping):
                    raise ValueError("translation JSON document must be an object")
                validate_translation_items(
                    translation_payload["translations"],
                    [str(segment["id"]) for segment in transcript_segments],
                )
                target_status = "translated"
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                pass
        elif (
            transcript_segments is None
            and job.audio_path
            and Path(job.audio_path).is_file()
        ):
            target_status = "audio_ready"

        retry_options = dict(job.options)
        chunk_adjusted = False
        backend = str(retry_options.get("backend", ""))
        if backend == "whisperx":
            chunk_length = int(
                retry_options.get(
                    "chunk_length_seconds",
                    WHISPERX_MAX_CHUNK_LENGTH_SECONDS,
                )
            )
            if chunk_length > WHISPERX_MAX_CHUNK_LENGTH_SECONDS:
                retry_options["chunk_length_seconds"] = (
                    WHISPERX_MAX_CHUNK_LENGTH_SECONDS
                )
                chunk_adjusted = True
        elif backend == "hybrid":
            raw_rescue = retry_options.get("hybrid_rescue", {})
            if isinstance(raw_rescue, Mapping):
                rescue = dict(raw_rescue)
                whisperx_chunk_length = int(
                    rescue.get(
                        "whisperx_chunk_length_seconds",
                        WHISPERX_MAX_CHUNK_LENGTH_SECONDS,
                    )
                )
                if (
                    whisperx_chunk_length
                    > WHISPERX_MAX_CHUNK_LENGTH_SECONDS
                ):
                    rescue["whisperx_chunk_length_seconds"] = (
                        WHISPERX_MAX_CHUNK_LENGTH_SECONDS
                    )
                    retry_options["hybrid_rescue"] = rescue
                    chunk_adjusted = True

        retry_fields: dict[str, Any] = {
            "status": target_status,
            "attempt": job.attempt + 1,
            "blocked_stage": None,
            "error": None,
            "translation_pause_requested": 0,
            "job_stop_requested": 0,
        }
        if chunk_adjusted:
            retry_fields["options_json"] = json.dumps(
                retry_options,
                sort_keys=True,
            )
        if target_status in {"queued", "audio_ready"}:
            retry_fields.update(
                {
                    "chunks_created": 0,
                    "chunks_completed": 0,
                    "transcription_stage": None,
                    "transcription_stage_index": 0,
                    "transcription_stage_total": 0,
                }
            )
        if target_status == "audio_ready":
            # A manual retry must submit the persisted WAV again. Retaining a
            # terminal remote ID would only replay its previous failed status.
            retry_fields["stt_job_id"] = None
            retry_fields["stt_runtime_id"] = None
        if (
            target_status == "audio_ready"
            and job.audio_path
            and Path(job.audio_path).is_file()
        ):
            retry_fields["chunks_total_estimate"] = (
                estimate_transcription_chunks(
                    wav_duration_seconds(Path(job.audio_path)),
                    retry_options,
                )
            )
        self.store.update(job.id, **retry_fields)
        if job.phase == "translation" or job.blocked_stage == "translation":
            self._set_translation_circuit("ready")
        if chunk_adjusted:
            self.store.add_event(
                job.id,
                "info",
                "legacy WhisperX chunk length reduced to 30 seconds for retry",
            )
        self.store.add_event(
            job.id,
            "info",
            f"manual retry requested; resuming from {target_status}",
            event_code="job.retry_requested",
            from_state=job.state,
            attempt=job.attempt + 1,
            payload={
                "previous_status": job.status,
                "target_status": target_status,
            },
        )
        retried = self.store.get(job.id)
        if retried is None:
            raise RuntimeError("retried job could not be read")
        return retried

    def retry_all_jobs(self) -> int:
        return self.retry_jobs(
            [job.id for job in self.store.list_open_jobs()]
        )

    def retry_jobs(self, job_ids: Sequence[str]) -> int:
        retried_count = 0
        unique_job_ids = tuple(
            dict.fromkeys(job_id.strip() for job_id in job_ids if job_id.strip())
        )
        for job_id in unique_job_ids:
            listed = self.store.get(job_id)
            if listed is None or not listed.can_retry:
                continue
            try:
                self.retry(listed.id)
            except ValueError:
                current = self.store.get(listed.id)
                if current is None or not current.can_retry:
                    continue
                raise
            retried_count += 1
        return retried_count

    def delete_job_record(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            raise ValueError("job not found")
        if not job.can_delete_record:
            raise ValueError(
                "only completed audio extraction records, retriable jobs, "
                "or jobs missing from the remote transcription server can "
                "be deleted"
            )
        if not self.store.delete(job.id):
            raise RuntimeError("job could not be deleted")

    def delete_missing_remote_transcription(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None or not job.remote_transcription_missing:
            raise ValueError(
                "only jobs missing from the remote transcription server "
                "can be deleted"
            )
        self.delete_job_record(job_id)

    def restart_translation(
        self,
        job_id: str,
        prompt_category_id: str | None = None,
        transcript_revision_id: str | None = None,
    ) -> PipelineJob:
        """Reset translation only while preserving a completed transcript."""
        job = self.store.get(job_id)
        if job is None:
            raise ValueError("job not found")
        if job.status != "completed":
            raise ValueError(
                "translation can only be restarted for completed jobs"
            )
        if not job.transcript_path:
            raise ValueError("transcript artifact is unavailable")

        transcript_path = Path(job.transcript_path)
        current_transcript_payload, current_segments, current_job_id = (
            self._load_translation_transcript(transcript_path)
        )
        transcript_payload = current_transcript_payload
        segments = current_segments
        transcript_job_id = current_job_id

        selected_job = replace(
            job,
            stt_job_id=job.stt_job_id or current_job_id,
        )
        selected_revision_id = job.transcript_revision_id
        selected_chunks_total = job.transcription_chunks_total
        if transcript_revision_id:
            revision = self.store.get_transcript_revision(
                job.id,
                transcript_revision_id,
            )
            if revision is None:
                raise ValueError("선택한 전사 리비전을 찾을 수 없습니다.")
            selected_path = Path(str(revision["artifact_path"])).resolve()
            job_root = (self.settings.jobs_dir / job.id).resolve()
            try:
                selected_path.relative_to(job_root)
            except ValueError as error:
                raise ValueError(
                    "선택한 전사 리비전의 경로가 올바르지 않습니다."
                ) from error
            if (
                not selected_path.is_file()
                or sha256_file(selected_path) != revision["content_hash"]
            ):
                raise ValueError(
                    "선택한 전사 리비전의 파일 무결성을 확인할 수 없습니다."
                )
            transcript_path = selected_path
            transcript_payload, segments, transcript_job_id = (
                self._load_translation_transcript(transcript_path)
            )
            selected_revision_id = str(revision["id"])
            selected_chunks_total = int(revision["chunks_total"])
            selected_job = replace(
                job,
                transcript_path=str(transcript_path),
                transcript_revision_id=selected_revision_id,
                stt_job_id=(
                    str(revision["remote_job_id"])
                    if revision.get("remote_job_id")
                    else transcript_job_id
                ),
            )

        current_prompt_snapshot = job.options.get(TRANSLATION_PROMPT_OPTION)
        if not isinstance(current_prompt_snapshot, Mapping):
            current_prompt_snapshot = self._legacy_prompt_snapshot()
        legacy_translation = self._capture_legacy_translation_generation(
            job,
            current_transcript_payload,
            current_segments,
            current_prompt_snapshot,
            self.remote_servers,
        )
        self._capture_legacy_subtitle_generation(
            job,
            translation_generation_id=(
                str(legacy_translation["id"])
                if legacy_translation is not None
                else None
            ),
        )

        updated_options = dict(job.options)
        if prompt_category_id:
            updated_options[TRANSLATION_PROMPT_OPTION] = self._prompt_snapshot(
                prompt_category_id
            )
        elif not isinstance(
            updated_options.get(TRANSLATION_PROMPT_OPTION),
            Mapping,
        ):
            updated_options[TRANSLATION_PROMPT_OPTION] = (
                self._legacy_prompt_snapshot()
            )
        translation_path = (
            Path(job.translation_path)
            if job.translation_path
            else artifact_path(
                self.settings.jobs_dir,
                job.id,
                job.source_rel,
                "translation",
            )
        )
        updated_prompt_snapshot = updated_options.get(TRANSLATION_PROMPT_OPTION)
        if not isinstance(updated_prompt_snapshot, Mapping):
            updated_prompt_snapshot = self._legacy_prompt_snapshot()
        generation = self._create_translation_generation(
            selected_job,
            transcript_payload,
            updated_prompt_snapshot,
            self.remote_servers,
            origin="restart",
            force_new=True,
        )
        self._write_translation_generation_snapshot(
            translation_path,
            generation,
            transcript_job_id=transcript_job_id,
            status="partial",
            translations=[],
        )
        self.store.update(
            job.id,
            status="transcribed",
            transcript_path=str(transcript_path),
            transcript_revision_id=selected_revision_id,
            stt_job_id=selected_job.stt_job_id,
            translation_path=str(translation_path),
            blocked_stage=None,
            error=None,
            translation_chunks_total=0,
            translation_chunks_completed=0,
            chunks_created=selected_chunks_total,
            chunks_completed=selected_chunks_total,
            chunks_total_estimate=selected_chunks_total,
            translation_pause_requested=0,
            options_json=json.dumps(updated_options, sort_keys=True),
        )
        self.store.add_event(
            job.id,
            "info",
            "translation restart requested; transcript preserved and "
            f"generation {generation['generation_number']} created"
            + (
                f" from transcript revision {selected_revision_id}"
                if selected_revision_id
                else ""
            ),
            event_code="translation.generation_created",
            from_state=job.state,
            phase="translation",
            payload={
                "generation_number": generation["generation_number"],
                "transcript_revision_id": selected_revision_id,
                "prompt_revision_id": generation.get("prompt_revision_id"),
            },
        )
        restarted = self.store.get(job.id)
        if restarted is None:
            raise RuntimeError("restarted job could not be read")
        return restarted

    @staticmethod
    def _load_translation_transcript(
        transcript_path: Path,
    ) -> tuple[Mapping[str, Any], list[dict[str, Any]], str]:
        if not transcript_path.is_file():
            raise ValueError("transcript artifact is unavailable")
        try:
            payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("transcript artifact could not be read") from error
        if not isinstance(payload, Mapping):
            raise ValueError("transcript JSON document must be an object")
        segments = validate_transcript(payload)
        transcript_job_id = str(payload.get("job_id", "")).strip()
        if not transcript_job_id:
            raise ValueError("transcript job_id is unavailable")
        return payload, segments, transcript_job_id

    def reprocess(
        self,
        job_id: str,
        operation: str,
        prompt_category_id: str | None = None,
    ) -> PipelineJob:
        original = self.store.get(job_id)
        if original is None:
            raise ValueError("job not found")
        if original.status not in SUCCESS_STATUSES:
            raise ValueError("only successful jobs can be reprocessed")
        if operation not in SUPPORTED_OPERATIONS:
            raise ValueError("unsupported job operation")
        if operation in {"transcribe", "full"} and not (
            self.transcription_server_configured
        ):
            raise ValueError("전사 서버 설정이 필요합니다.")
        if operation in TRANSLATION_OPERATIONS and not (
            self.translation_server_configured
        ):
            raise ValueError("번역 서버 설정이 필요합니다.")
        return self.create_job(
            original.source_rel,
            force_overwrite=True,
            options=original.options,
            operation=operation,
            prompt_category_id=(
                prompt_category_id
                if operation in TRANSLATION_OPERATIONS
                else None
            ),
        )

    def pause_translation(self, job_id: str) -> PipelineJob:
        job = self.store.get(job_id)
        if job is None:
            raise ValueError("job not found")
        if job.operation not in TRANSLATION_OPERATIONS:
            raise ValueError("this job does not include translation")
        if job.status == "translation_paused":
            return job
        if job.status == "transcribed":
            self.store.update(
                job.id,
                status="translation_paused",
                translation_pause_requested=1,
            )
        elif job.status == "translation_running":
            self.store.update(job.id, translation_pause_requested=1)
        else:
            raise ValueError("translation is not waiting or running")
        self.store.add_event(
            job.id,
            "info",
            "translation pause requested",
            event_code="translation.pause_requested",
            from_state=job.state,
            phase="translation",
        )
        paused = self.store.get(job.id)
        if paused is None:
            raise RuntimeError("paused job could not be read")
        return paused

    def pause_all_translations(self) -> int:
        paused_count = 0
        for listed in self.store.list_open_jobs():
            for _attempt in range(3):
                job = self.store.get(listed.id)
                if job is None or not job.can_pause_translation:
                    break
                fields: dict[str, Any] = {"translation_pause_requested": 1}
                if job.status == "transcribed":
                    fields.update(
                        {
                            "status": "translation_paused",
                            "blocked_stage": None,
                            "error": None,
                        }
                    )
                if self.store.update_if_status(
                    job.id,
                    {job.status},
                    **fields,
                ):
                    self.store.add_event(
                        job.id,
                        "info",
                        "bulk translation pause requested",
                        event_code="translation.pause_requested",
                        from_state=job.state,
                        phase="translation",
                        payload={"scope": "bulk"},
                    )
                    paused_count += 1
                    break
        return paused_count

    def stop_jobs(
        self,
        job_ids: Sequence[str],
        *,
        stop_message: str = USER_SELECTED_STOP_MESSAGE,
    ) -> int:
        stopped_count = 0
        unique_job_ids = tuple(
            dict.fromkeys(job_id.strip() for job_id in job_ids if job_id.strip())
        )
        for job_id in unique_job_ids:
            for _attempt in range(3):
                job = self.store.get(job_id)
                if job is None or not job.can_stop:
                    break
                if job.status in RUNNING_STATUSES:
                    fields: dict[str, Any] = {"job_stop_requested": 1}
                    event_message = (
                        "job stop requested; waiting for a safe stop point"
                    )
                else:
                    fields = {
                        "status": "blocked",
                        "state": JobState.STOPPED.value,
                        "reason_code": JobReason.USER_STOP.value,
                        "blocked_stage": WAITING_STAGE_BY_STATUS[job.status],
                        "error": stop_message,
                        "translation_pause_requested": 0,
                        "job_stop_requested": 0,
                    }
                    event_message = "job stopped by user request"
                if self.store.update_if_status(
                    job.id,
                    {job.status},
                    **fields,
                ):
                    self.store.add_event(
                        job.id,
                        "warning",
                        event_message,
                        event_code=(
                            "job.stop_requested"
                            if job.status in RUNNING_STATUSES
                            else "job.stopped"
                        ),
                        from_state=job.state,
                        phase=job.phase,
                    )
                    if (
                        job.status == "transcription_running"
                        and job.stt_job_id
                    ):
                        stt_client = self._stt_client_for_job(job)
                        try:
                            if stt_client is not None:
                                stt_client.cancel_job(job.stt_job_id)
                        except ExternalServiceError as error:
                            message = self._sanitize_error(str(error))
                            self.store.add_event(
                                job.id,
                                "warning",
                                "remote transcription cancellation is "
                                f"pending: {message}",
                            )
                    stopped_count += 1
                    break
        return stopped_count

    def stop_all_jobs(self) -> int:
        return self.stop_jobs(
            [job.id for job in self.store.list_open_jobs()],
            stop_message=USER_STOP_MESSAGE,
        )

    def resume_translation(self, job_id: str) -> PipelineJob:
        job = self.store.get(job_id)
        if job is None:
            raise ValueError("job not found")
        if job.status != "translation_paused":
            raise ValueError("only paused translations can be resumed")
        self.store.update(
            job.id,
            status="transcribed",
            translation_pause_requested=0,
            blocked_stage=None,
            error=None,
        )
        self.store.add_event(
            job.id,
            "info",
            "translation resume requested",
            event_code="translation.resume_requested",
            from_state=job.state,
            phase="translation",
        )
        resumed = self.store.get(job.id)
        if resumed is None:
            raise RuntimeError("resumed job could not be read")
        return resumed

    def save_artifact(
        self,
        job_id: str,
        kind: str,
        content: str,
    ) -> Path:
        job = self.store.get(job_id)
        if job is None:
            raise ValueError("job not found")
        if job.status not in {
            "transcription_completed",
            "completed",
            "blocked",
            "failed",
        }:
            raise ValueError(
                "완료되거나 중단된 작업의 JSON만 편집할 수 있습니다."
            )
        if len(content.encode("utf-8")) > MAX_EDITABLE_JSON_BYTES:
            raise ValueError("JSON 편집 내용이 20 MiB 제한을 초과했습니다.")

        paths = {
            "transcript": job.transcript_path,
            "translation": job.translation_path,
        }
        if kind not in paths:
            raise ValueError("unsupported artifact kind")
        selected_path = paths[kind]
        if not selected_path or not Path(selected_path).is_file():
            raise ValueError("artifact not found")

        try:
            payload = json.loads(content)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"JSON syntax error at line {error.lineno}, "
                f"column {error.colno}: {error.msg}"
            ) from error
        if not isinstance(payload, Mapping):
            raise ValueError("JSON document must be an object")

        manual_generation: dict[str, Any] | None = None
        if kind == "transcript":
            segments = validate_transcript(payload)
            if job.translation_path and Path(job.translation_path).is_file():
                translation_payload = json.loads(
                    Path(job.translation_path).read_text(encoding="utf-8")
                )
                if not isinstance(translation_payload, Mapping):
                    raise ValueError(
                        "translation JSON document must be an object"
                    )
                validate_translation_items(
                    translation_payload.get("translations"),
                    [str(segment["id"]) for segment in segments],
                )
        else:
            if payload.get("schema_version") != TRANSLATION_SCHEMA_VERSION:
                raise ValueError("unsupported translation schema_version")
            if not job.transcript_path or not Path(job.transcript_path).is_file():
                raise ValueError("transcript artifact is unavailable")
            transcript_payload = json.loads(
                Path(job.transcript_path).read_text(encoding="utf-8")
            )
            if not isinstance(transcript_payload, Mapping):
                raise ValueError("transcript JSON document must be an object")
            segments = validate_transcript(transcript_payload)
            translations = validate_translation_items(
                payload.get("translations"),
                [str(segment["id"]) for segment in segments],
            )
            payload = {
                **dict(payload),
                "status": "completed",
                "translations": translations,
            }

            prompt_snapshot = job.options.get(TRANSLATION_PROMPT_OPTION)
            if not isinstance(prompt_snapshot, Mapping):
                prompt_snapshot = self._legacy_prompt_snapshot()
            legacy_translation = self._capture_legacy_translation_generation(
                job,
                transcript_payload,
                segments,
                prompt_snapshot,
                self.remote_servers,
            )
            self._capture_legacy_subtitle_generation(
                job,
                translation_generation_id=(
                    str(legacy_translation["id"])
                    if legacy_translation is not None
                    else None
                ),
            )
            manual_generation = self._create_translation_generation(
                job,
                transcript_payload,
                prompt_snapshot,
                self.remote_servers,
                origin="manual",
                force_new=True,
                model="manual",
                endpoint_key="manual",
            )
            self.store.save_translation_batch(
                manual_generation["id"],
                batch_index=0,
                generation_attempt=0,
                kind="manual",
                items=self._translation_item_records(segments, translations),
            )
            translations = self.store.complete_translation_generation(
                manual_generation["id"],
                [str(segment["id"]) for segment in segments],
            )

        if kind == "transcript":
            revision_id = uuid4().hex
            payload = {
                **dict(payload),
                "revision": {
                    "id": revision_id,
                    "supersedes_revision_id": job.transcript_revision_id,
                    "origin": "manual",
                },
            }
            artifact = (
                self.settings.jobs_dir
                / job.id
                / "transcript-revisions"
                / revision_id
                / artifact_filename(job.source_rel, "transcript")
            )
            write_json_atomic(artifact, payload)
            if not self.store.record_transcript_revision(
                revision_id=revision_id,
                job_id=job.id,
                audio_revision_id=job.audio_revision_id,
                remote_job_id=job.stt_job_id,
                backend=str(job.options.get("backend", "manual")),
                model_revision="manual",
                options_hash=_canonical_payload_hash(job.options),
                artifact_path=str(artifact),
                content_hash=sha256_file(artifact),
                origin="manual",
                status=None,
                chunks_total=len(payload["segments"]),
            ):
                raise RuntimeError("manual transcript revision was not saved")
        else:
            artifact = Path(selected_path)
        if manual_generation is not None:
            self._write_translation_generation_snapshot(
                artifact,
                manual_generation,
                transcript_job_id=str(transcript_payload["job_id"]),
                status="completed",
                translations=translations,
            )
        elif kind != "transcript":
            write_json_atomic(artifact, payload)

        refreshed = self.store.get(job.id)
        if (
            refreshed is not None
            and refreshed.transcript_path
            and refreshed.translation_path
            and Path(refreshed.transcript_path).is_file()
            and Path(refreshed.translation_path).is_file()
        ):
            self._render_artifacts(
                refreshed,
                overwrite=(
                    refreshed.force_overwrite
                    or bool(refreshed.srt_path)
                    or bool(refreshed.ass_path)
                ),
            )
            self.store.add_event(
                job.id,
                "info",
                f"{kind} JSON edited; subtitle regenerated",
            )
        else:
            self.store.add_event(job.id, "info", f"{kind} JSON edited")
        return artifact

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            self._scheduler_tick()
            self._stop_event.wait(1.0)

    def _scheduler_tick(self) -> None:
        self._schedule_runtime_reprobes()
        if not self.store.ids_with_status("rendering"):
            self._dispatch_one(
                "translated",
                "rendering",
                "render",
                self._render_executor,
                self._render,
            )
        extracting = len(self.store.ids_with_status("extracting"))
        for _ in range(max(0, self.settings.audio_workers - extracting)):
            if not self._dispatch_one(
                "queued",
                "extracting",
                "audio extraction",
                self._audio_executor,
                self._extract,
            ):
                break
        if self.stt_gate_state == "ready":
            self._dispatch_transcriptions()
        self._dispatch_translations()

    def _schedule_runtime_reprobes(self) -> None:
        now = time.time()
        with self._runtime_lock:
            health = {
                runtime_id: dict(value)
                for runtime_id, value in self._runtime_health.items()
            }
        candidates = [
            str(definition["id"])
            for definition in self._runtime_definitions()
            if definition["enabled"]
            and not definition["builtin"]
            and health.get(str(definition["id"]), {}).get("status")
            == "unavailable"
            and now
            - float(
                health.get(str(definition["id"]), {}).get("checked_at") or 0
            )
            >= RUNTIME_REPROBE_INTERVAL_SECONDS
        ]
        for runtime_id in candidates:
            self._set_runtime_health(runtime_id, "checking")
            self._runtime_probe_executor.submit(
                self.probe_runtime_endpoint,
                runtime_id,
            )

    def _dispatch_transcriptions(self) -> int:
        with self._runtime_lock:
            counts = self.store.transcription_runtime_counts()
            health = {
                runtime_id: str(value.get("status", "unknown"))
                for runtime_id, value in self._runtime_health.items()
            }
            slots: list[str] = []
            for definition in self._runtime_definitions():
                runtime_id = str(definition["id"])
                if (
                    not definition["enabled"]
                    or health.get(runtime_id) != "ready"
                ):
                    continue
                available = max(
                    0,
                    int(definition["capacity"])
                    - counts.get(runtime_id, 0),
                )
                slots.extend([runtime_id] * available)
            if slots:
                start = self._runtime_dispatch_cursor % len(slots)
                slots = slots[start:] + slots[:start]
                slots = slots[
                    : max(0, MAX_RUNTIME_ENDPOINTS - sum(counts.values()))
                ]
            dispatched = 0
            for runtime_id in slots:
                if not self._dispatch_one(
                    "audio_ready",
                    "transcription_running",
                    "transcription",
                    self._stt_executor,
                    self._transcribe,
                    stt_runtime_id=runtime_id,
                ):
                    break
                dispatched += 1
            if slots:
                self._runtime_dispatch_cursor = (
                    self._runtime_dispatch_cursor + dispatched
                ) % len(slots)
        return dispatched

    def _dispatch_translations(self) -> int:
        if self.translation_circuit_state != "ready":
            return 0
        if self.store.ids_with_status("translation_running"):
            return 0
        return int(
            self._dispatch_one(
                "transcribed",
                "translation_running",
                "translation",
                self._translation_executor,
                self._translate,
            )
        )

    def _dispatch_one(
        self,
        waiting: str,
        running: str,
        stage: str,
        executor: ThreadPoolExecutor,
        operation: Callable[[PipelineJob], None],
        *,
        stt_runtime_id: str | None = None,
    ) -> bool:
        waiting_ids = self.store.dispatchable_ids_with_status(waiting)
        for job_id in waiting_ids:
            lease_token = self.store.claim_for_dispatch(
                job_id,
                waiting,
                running,
                lease_owner=self._worker_id,
                lease_seconds=JOB_LEASE_SECONDS,
                stt_runtime_id=stt_runtime_id,
            )
            if lease_token is None:
                continue
            self.store.add_event(
                job_id,
                "info",
                f"{stage} started",
                event_code="stage.started",
                from_state=JobState.WAITING.value,
                to_state=JobState.RUNNING.value,
                phase=EVENT_PHASE_BY_STAGE[stage],
                payload={
                    "waiting_status": waiting,
                    "running_status": running,
                    **(
                        {"runtime_id": stt_runtime_id}
                        if stt_runtime_id is not None
                        else {}
                    ),
                },
            )
            self._submit_stage(
                executor,
                job_id,
                stage,
                operation,
                lease_token,
            )
            return True
        return False

    def _submit_stage(
        self,
        executor: ThreadPoolExecutor,
        job_id: str,
        stage: str,
        operation: Callable[[PipelineJob], None],
        lease_token: int,
    ) -> None:
        future = executor.submit(
            self._run_stage,
            job_id,
            stage,
            operation,
            lease_token,
        )
        if not isinstance(future, Future):
            return
        with self._stage_futures_lock:
            self._stage_futures.add(future)
        future.add_done_callback(self._forget_stage_future)

    def _forget_stage_future(self, future: Future[Any]) -> None:
        with self._stage_futures_lock:
            self._stage_futures.discard(future)

    def _run_stage(
        self,
        job_id: str,
        stage: str,
        operation: Callable[[PipelineJob], None],
        expected_lease_token: int | None = None,
    ) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        if expected_lease_token is not None and (
            job.lease_owner != self._worker_id
            or job.lease_token != expected_lease_token
        ):
            self._record_lease_fencing_rejection(stage, "dispatch_precheck")
            LOGGER.warning(
                "discarded superseded %s stage for job %s",
                stage,
                job_id,
            )
            return
        lease_stop: threading.Event | None = None
        lease_heartbeat: threading.Thread | None = None
        if job.lease_owner == self._worker_id:
            lease_stop = threading.Event()
            lease_heartbeat = threading.Thread(
                target=self._lease_heartbeat_loop,
                args=(job_id, job.lease_token, lease_stop),
                name=f"pipeline-lease-{job_id[:8]}",
                daemon=True,
            )
            lease_heartbeat.start()
        try:
            self._raise_if_job_stop_requested(job_id)
            operation(job)
            self._raise_if_job_stop_requested(job_id)
        except WorkerLeaseLost:
            self._record_lease_fencing_rejection(stage, "worker_result")
            LOGGER.warning(
                "discarded superseded %s result for job %s",
                stage,
                job_id,
            )
        except OperationStopped:
            self._mark_job_stopped(job, stage)
        except RemoteTranscriptionFailed as error:
            message = self._sanitize_error(str(error))
            failed_runtime_id = job.stt_runtime_id or BUILTIN_RUNTIME_ID
            if stage == "transcription" and (
                error.failure_scope == "service"
                or (
                    (
                        error.failure_scope == "configuration"
                        or error.failure_code == "auth_required"
                    )
                    and self._has_alternate_ready_runtime(failed_runtime_id)
                )
            ):
                self._set_runtime_health(
                    failed_runtime_id,
                    "unavailable",
                    message=message,
                    reason_code=(
                        JobReason.AUTH_REQUIRED.value
                        if error.failure_code == "auth_required"
                        else JobReason.STT_UNAVAILABLE.value
                    ),
                )
                self._requeue_transcription_after_runtime_failure(
                    job,
                    message=message,
                    failure_code=error.failure_code,
                )
                return
            reason_by_failure_code = {
                "invalid_input": JobReason.INVALID_INPUT.value,
                "model_output_invalid": JobReason.MODEL_OUTPUT_INVALID.value,
                "auth_required": JobReason.AUTH_REQUIRED.value,
                "resource_exhausted": JobReason.RESOURCE_EXHAUSTED.value,
                "service_restarted": JobReason.SERVICE_RESTARTED.value,
                "transcription_processing_error": (
                    JobReason.TRANSCRIPTION_PROCESSING_ERROR.value
                ),
            }
            reason_code = reason_by_failure_code.get(
                error.failure_code,
                JobReason.INTERNAL_ERROR.value,
            )
            blocked = bool(error.retryable) or (
                error.failure_code == "auth_required"
            )
            if not self._update_stage_job(
                job,
                status="blocked" if blocked else "failed",
                blocked_stage=stage,
                reason_code=reason_code,
                error=message,
            ):
                self._record_lease_fencing_rejection(
                    stage,
                    "terminal_transition",
                )
                return
            if error.failure_code == "auth_required":
                self._set_runtime_health(
                    job.stt_runtime_id or BUILTIN_RUNTIME_ID,
                    "unavailable",
                    message=message,
                    reason_code=JobReason.AUTH_REQUIRED.value,
                )
            outcome = "blocked" if blocked else "failed"
            level = "warning" if blocked else "error"
            self.store.add_event(
                job_id,
                level,
                f"{stage} {outcome}: {message}",
                event_code=f"stage.{outcome}",
                from_state=JobState.RUNNING.value,
                phase=EVENT_PHASE_BY_STAGE[stage],
                payload={
                    "failure_code": error.failure_code,
                    "reason_code": reason_code,
                    "retryable": bool(error.retryable),
                },
            )
            getattr(LOGGER, level)(
                "job %s %s %s: %s",
                job_id,
                stage,
                outcome,
                message,
            )
        except ExternalServiceError as error:
            message = self._sanitize_error(str(error))
            if stage == "transcription":
                self._set_runtime_health(
                    job.stt_runtime_id or BUILTIN_RUNTIME_ID,
                    "unavailable",
                    message=message,
                )
                self._requeue_transcription_after_runtime_failure(
                    job,
                    message=message,
                )
                return
            if not self._update_stage_job(
                job,
                status="blocked",
                blocked_stage=stage,
                reason_code=(
                    JobReason.LM_UNAVAILABLE.value
                    if stage == "translation"
                    else JobReason.STT_UNAVAILABLE.value
                ),
                error=message,
            ):
                self._record_lease_fencing_rejection(
                    stage,
                    "terminal_transition",
                )
                return
            if stage == "translation":
                self._set_translation_circuit(
                    "lost",
                    reason_code=JobReason.LM_UNAVAILABLE.value,
                    error=message,
                )
            self.store.add_event(
                job_id,
                "warning",
                f"{stage} blocked: {message}",
                event_code="stage.blocked",
                from_state=JobState.RUNNING.value,
                phase=EVENT_PHASE_BY_STAGE[stage],
                payload={
                    "reason_code": (
                        JobReason.LM_UNAVAILABLE.value
                        if stage == "translation"
                        else JobReason.STT_UNAVAILABLE.value
                    )
                },
            )
            LOGGER.warning("job %s %s blocked: %s", job_id, stage, message)
        except TranslationPaused:
            current = self.store.get(job_id)
            if current is not None and current.job_stop_requested:
                self._mark_job_stopped(job, stage)
            else:
                if not self._update_stage_job(
                    job,
                    status="translation_paused",
                    blocked_stage=None,
                    error=None,
                    translation_pause_requested=1,
                ):
                    self._record_lease_fencing_rejection(
                        stage,
                        "terminal_transition",
                    )
                    return
                self.store.add_event(
                    job_id,
                    "info",
                    "translation paused",
                    event_code="stage.paused",
                    from_state=JobState.RUNNING.value,
                    phase="translation",
                )
        except BaseException as error:
            message = self._sanitize_error(str(error) or error.__class__.__name__)
            if not self._update_stage_job(
                job,
                status="failed",
                blocked_stage=stage,
                reason_code=JobReason.INTERNAL_ERROR.value,
                error=message,
            ):
                self._record_lease_fencing_rejection(
                    stage,
                    "terminal_transition",
                )
                return
            self.store.add_event(
                job_id,
                "error",
                f"{stage} failed: {message}",
                event_code="stage.failed",
                from_state=JobState.RUNNING.value,
                phase=EVENT_PHASE_BY_STAGE[stage],
                payload={"reason_code": JobReason.INTERNAL_ERROR.value},
            )
            LOGGER.exception("job %s %s failed", job_id, stage)
        finally:
            if lease_stop is not None:
                lease_stop.set()
            if lease_heartbeat is not None:
                lease_heartbeat.join(timeout=1)
            if job.lease_owner == self._worker_id:
                self.store.release_job_lease(
                    job_id,
                    lease_owner=self._worker_id,
                    lease_token=job.lease_token,
                )

    def _lease_heartbeat_loop(
        self,
        job_id: str,
        lease_token: int,
        stop_event: threading.Event,
    ) -> None:
        while not stop_event.wait(JOB_LEASE_HEARTBEAT_SECONDS):
            try:
                if not self.store.refresh_job_lease(
                    job_id,
                    lease_owner=self._worker_id,
                    lease_token=lease_token,
                    lease_seconds=JOB_LEASE_SECONDS,
                ):
                    return
            except (OSError, RuntimeError, sqlite3.Error):
                LOGGER.exception("job lease heartbeat failed for %s", job_id)

    def _raise_if_job_stop_requested(self, job_id: str) -> None:
        current = self.store.get(job_id)
        if current is not None and current.job_stop_requested:
            raise OperationStopped("job stop requested")

    def _requeue_transcription_after_runtime_failure(
        self,
        job: PipelineJob,
        *,
        message: str,
        failure_code: str | None = None,
    ) -> None:
        current = self.store.get(job.id)
        if current is not None and current.job_stop_requested:
            self._mark_job_stopped(job, "transcription")
            return
        failed_runtime_id = job.stt_runtime_id or BUILTIN_RUNTIME_ID
        remote_job_id = current.stt_job_id if current is not None else job.stt_job_id
        attempt = (current.attempt if current is not None else job.attempt) + 1
        if not self._update_stage_job(
            job,
            status="audio_ready",
            attempt=attempt,
            stt_job_id=None,
            stt_runtime_id=None,
            blocked_stage=None,
            error=None,
            chunks_created=0,
            chunks_completed=0,
            transcription_stage=None,
            transcription_stage_index=0,
            transcription_stage_total=0,
            job_stop_requested=0,
        ):
            self._record_lease_fencing_rejection(
                "transcription",
                "runtime_failover",
            )
            return
        self.store.add_event(
            job.id,
            "warning",
            "transcription Runtime unavailable; queued for another Runtime",
            event_code="transcription.runtime_failover",
            from_state=JobState.RUNNING.value,
            phase="transcription",
            attempt=attempt,
            payload={
                "failed_runtime_id": failed_runtime_id,
                "remote_job_id": remote_job_id,
                "failure_code": failure_code,
                "reason_code": JobReason.STT_UNAVAILABLE.value,
            },
        )
        LOGGER.warning(
            "job %s transcription Runtime %s unavailable; requeued: %s",
            job.id,
            failed_runtime_id,
            message,
        )

    def _has_alternate_ready_runtime(self, failed_runtime_id: str) -> bool:
        with self._runtime_lock:
            return any(
                runtime_id != failed_runtime_id
                and current.get("status") == "ready"
                for runtime_id, current in self._runtime_health.items()
            )

    def _update_stage_job(self, job: PipelineJob, **fields: Any) -> bool:
        if job.lease_owner == self._worker_id and job.lease_token > 0:
            return self.store.update_if_lease(
                job.id,
                lease_owner=self._worker_id,
                lease_token=job.lease_token,
                **fields,
            )
        self.store.update(job.id, **fields)
        return True

    def _require_stage_update(self, job: PipelineJob, **fields: Any) -> None:
        if not self._update_stage_job(job, **fields):
            raise WorkerLeaseLost("worker lease was superseded")

    def _mark_job_stopped(self, job: PipelineJob, stage: str) -> None:
        if not self._update_stage_job(
            job,
            status="blocked",
            state=JobState.STOPPED.value,
            reason_code=JobReason.USER_STOP.value,
            blocked_stage=stage,
            error=USER_STOP_MESSAGE,
            translation_pause_requested=0,
            job_stop_requested=0,
        ):
            self._record_lease_fencing_rejection(
                stage,
                "terminal_transition",
            )
            return
        self.store.add_event(
            job.id,
            "warning",
            "job stopped by user request",
            event_code="job.stopped",
            from_state=job.state,
            phase=EVENT_PHASE_BY_STAGE[stage],
            payload={"reason_code": JobReason.USER_STOP.value},
        )

    def _extract(self, job: PipelineJob) -> None:
        source = self.library.resolve_file(job.source_rel)
        options = AudioExtraction(
            audio_stream=int(job.options["audio_stream"]),
            start_seconds=float(job.options["start_seconds"]),
            duration_seconds=job.options["duration_seconds"],
        )
        source_hash = sha256_file(source)
        extraction_hash = _canonical_payload_hash(asdict(options))
        next_status = (
            "audio_completed" if job.operation == "extract" else "audio_ready"
        )
        for revision in self.store.audio_revisions_for_signature(
            source_rel=job.source_rel,
            source_hash=source_hash,
            extraction_hash=extraction_hash,
        ):
            reusable_path = Path(str(revision["artifact_path"]))
            if (
                reusable_path.is_file()
                and sha256_file(reusable_path) == revision["content_hash"]
            ):
                chunk_estimate = estimate_transcription_chunks(
                    revision["duration_seconds"],
                    job.options,
                )
                self._require_stage_update(
                    job,
                    status=next_status,
                    audio_path=str(reusable_path),
                    audio_sha256=str(revision["content_hash"]),
                    audio_revision_id=str(revision["id"]),
                    chunks_total_estimate=chunk_estimate,
                )
                self.store.add_event(
                    job.id,
                    "info",
                    "audio extraction reused immutable revision "
                    f"{revision['id']}",
                    event_code="stage.completed",
                    from_state=JobState.RUNNING.value,
                    phase="extraction",
                    payload={
                        "audio_revision_id": revision["id"],
                        "reused": True,
                    },
                )
                return
        revision_id = uuid4().hex
        audio_path = (
            self.settings.jobs_dir
            / job.id
            / "audio-revisions"
            / revision_id
            / "audio.16k.wav"
        )
        extract_audio(source, audio_path, options)
        digest = sha256_file(audio_path)
        audio_duration = wav_duration_seconds(audio_path)
        chunk_estimate = estimate_transcription_chunks(
            audio_duration,
            job.options,
        )
        lease_owner = (
            self._worker_id if job.lease_owner == self._worker_id else None
        )
        persisted = self.store.record_audio_revision(
            revision_id=revision_id,
            job_id=job.id,
            source_rel=job.source_rel,
            source_hash=source_hash,
            extraction_hash=extraction_hash,
            artifact_path=str(audio_path),
            content_hash=digest,
            duration_seconds=audio_duration,
            status=next_status,
            chunks_total_estimate=chunk_estimate,
            lease_owner=lease_owner,
            lease_token=(job.lease_token if lease_owner is not None else None),
        )
        if not persisted:
            raise WorkerLeaseLost("worker lease was superseded")
        progress_detail = (
            f"; {audio_duration:.1f}s; approximately {chunk_estimate} "
            "transcription chunk(s)"
            if audio_duration is not None and chunk_estimate
            else ""
        )
        self.store.add_event(
            job.id,
            "info",
            "audio extraction completed "
            f"({audio_path.stat().st_size} bytes{progress_detail})",
            event_code="stage.completed",
            from_state=JobState.RUNNING.value,
            phase="extraction",
            payload={
                "audio_revision_id": revision_id,
                "chunks_total_estimate": chunk_estimate,
                "reused": False,
            },
        )

    def _transcribe(self, job: PipelineJob) -> None:
        runtime_id = job.stt_runtime_id or BUILTIN_RUNTIME_ID
        stt_client = self._runtime_client(runtime_id)
        if stt_client is None:
            raise ExternalServiceError("assigned Runtime is not configured")
        if not job.audio_path or not Path(job.audio_path).is_file():
            raise RuntimeError("extracted WAV is unavailable")
        options = {
            "backend": job.options.get("backend", "kotoba"),
            "chunk_length_seconds": job.options["chunk_length_seconds"],
            "num_speakers": job.options["num_speakers"],
            "min_speakers": job.options["min_speakers"],
            "max_speakers": job.options["max_speakers"],
            "add_punctuation": job.options["add_punctuation"],
            "noise_filter": job.options.get("noise_filter", True),
        }
        for key in (
            "batch_size",
            "subtitle_segmentation",
            "repetition_policy",
            "repetition_min_count",
            "hybrid_rescue",
            "whisperjav",
        ):
            if key in job.options:
                options[key] = job.options[key]
        audio_duration = wav_duration_seconds(Path(job.audio_path))
        source_start = float(job.options["start_seconds"])
        source_end = (
            round(source_start + audio_duration, 3)
            if audio_duration is not None
            else None
        )
        stt_call_count = (
            2 if options["backend"] in {"hybrid", "whisperjav"} else 1
        )
        request_metadata = {
            "job_id": job.id,
            "request_id": f"pipeline-{job.id}",
            "delivery_mode": "single_wav",
            "audio_sha256": job.audio_sha256,
            "audio_duration_sec": audio_duration,
            "source_start_sec": source_start,
            "source_end_sec": source_end,
            "provider": "remote_stt",
            "backend": options["backend"],
            "chunk_length_seconds": options["chunk_length_seconds"],
            "chunk_length_semantics": (
                "not_applicable"
                if options["backend"] == "whisperjav"
                else "model_internal"
            ),
            "stt_call_count": stt_call_count,
        }
        if options["backend"] == "hybrid":
            request_metadata["backend_chunk_lengths"] = {
                "kotoba": options["hybrid_rescue"][
                    "kotoba_chunk_length_seconds"
                ],
                "whisperx": options["hybrid_rescue"][
                    "whisperx_chunk_length_seconds"
                ],
            }
        elif options["backend"] == "whisperjav":
            request_metadata["backend_group_durations"] = dict(
                options["whisperjav"]
            )
            request_metadata["alignment_call_count"] = 1
            request_metadata["diarization_call_count"] = 1
        LOGGER.info(
            "stt_request job_id=%s request_id=%s delivery_mode=single_wav "
            "audio_sha256=%s duration_sec=%s source_start_sec=%.3f "
            "source_end_sec=%s chunk_length_seconds=%s call_count=%s",
            job.id,
            request_metadata["request_id"],
            job.audio_sha256,
            audio_duration if audio_duration is not None else "unknown",
            source_start,
            source_end if source_end is not None else "unknown",
            options["chunk_length_seconds"],
            stt_call_count,
            extra=request_metadata,
        )

        def save_remote_job(remote_job_id: str) -> None:
            self._require_stage_update(job, stt_job_id=remote_job_id)
            self.store.add_event(
                job.id,
                "info",
                f"remote transcription job accepted: {remote_job_id}",
                event_code="transcription.remote_accepted",
                from_state=JobState.RUNNING.value,
                phase="transcription",
                payload={
                    "remote_job_id": remote_job_id,
                    "runtime_id": runtime_id,
                },
            )

        def update_transcription_progress(progress: Mapping[str, Any]) -> None:
            fields: dict[str, Any] = {}
            current = self.store.get(job.id)
            stage_changed = False
            if "created" in progress and "completed" in progress:
                fields.update(
                    chunks_created=int(progress["created"]),
                    chunks_completed=int(progress["completed"]),
                    chunk_progress_every=int(
                        progress.get("report_every", 10)
                    ),
                )
            if "stage" in progress:
                stage = str(progress["stage"])
                stage_index = int(progress["stage_index"])
                stage_total = int(progress["stage_total"])
                fields.update(
                    transcription_stage=stage,
                    transcription_stage_index=stage_index,
                    transcription_stage_total=stage_total,
                )
                stage_changed = current is None or (
                    current.transcription_stage,
                    current.transcription_stage_index,
                    current.transcription_stage_total,
                ) != (stage, stage_index, stage_total)
            if fields:
                self._require_stage_update(job, **fields)
            if stage_changed:
                self.store.add_event(
                    job.id,
                    "info",
                    f"transcription stage changed to {stage}",
                    event_code="transcription.stage_changed",
                    from_state=JobState.RUNNING.value,
                    phase="transcription",
                    payload={
                        "stage": stage,
                        "stage_index": stage_index,
                        "stage_total": stage_total,
                        "runtime_id": runtime_id,
                    },
                )

        payload = stt_client.transcribe(
            Path(job.audio_path),
            options=options,
            idempotency_key=f"pipeline-{job.id}",
            existing_job_id=job.stt_job_id,
            on_job_created=save_remote_job,
            on_progress=update_transcription_progress,
            should_stop=lambda: bool(
                (current := self.store.get(job.id))
                and current.job_stop_requested
            ),
        )
        offset = float(job.options["start_seconds"])
        if offset:
            for segment in payload["segments"]:
                segment["start"] = round(float(segment["start"]) + offset, 3)
                segment["end"] = round(float(segment["end"]) + offset, 3)
        payload["source"] = {
            "relative_path": job.source_rel,
            "offset_seconds": offset,
        }
        input_metadata = payload.get("input")
        if isinstance(input_metadata, dict):
            remote_duration = input_metadata.get("duration_sec")
            effective_duration = (
                audio_duration
                if audio_duration is not None
                else (
                    float(remote_duration)
                    if remote_duration is not None
                    else None
                )
            )
            input_metadata.update(
                {
                    "source_start_sec": offset,
                    "source_end_sec": (
                        round(offset + effective_duration, 3)
                        if effective_duration is not None
                        else None
                    ),
                }
            )
        validate_transcript(payload)
        revision_id = uuid4().hex
        transcript_path = (
            self.settings.jobs_dir
            / job.id
            / "transcript-revisions"
            / revision_id
            / artifact_filename(job.source_rel, "transcript")
        )
        payload["revision"] = {
            "id": revision_id,
            "audio_revision_id": job.audio_revision_id,
            "origin": "automatic",
        }
        write_json_atomic(transcript_path, payload)
        current = self.store.get(job.id)
        translation_paused = bool(
            job.operation in TRANSLATION_OPERATIONS
            and current
            and current.translation_pause_requested
        )
        if job.operation == "transcribe":
            next_status = "transcription_completed"
        elif translation_paused:
            next_status = "translation_paused"
        else:
            next_status = "transcribed"
        final_segment_count = len(payload["segments"])
        model_revision = _transcription_model_revision(
            payload,
            fallback=str(options["backend"]),
        )
        lease_owner = (
            self._worker_id if job.lease_owner == self._worker_id else None
        )
        persisted = self.store.record_transcript_revision(
            revision_id=revision_id,
            job_id=job.id,
            audio_revision_id=job.audio_revision_id,
            remote_job_id=(
                str(payload["job_id"]) if payload.get("job_id") else job.stt_job_id
            ),
            backend=str(options["backend"]),
            model_revision=model_revision,
            options_hash=_canonical_payload_hash(options),
            artifact_path=str(transcript_path),
            content_hash=sha256_file(transcript_path),
            origin="automatic",
            status=next_status,
            chunks_total=final_segment_count,
            translation_pause_requested=translation_paused,
            lease_owner=lease_owner,
            lease_token=(job.lease_token if lease_owner is not None else None),
        )
        if not persisted:
            raise WorkerLeaseLost("worker lease was superseded")
        self.store.add_event(
            job.id,
            "info",
            f"transcription completed ({len(payload['segments'])} segments)",
            event_code="stage.completed",
            from_state=JobState.RUNNING.value,
            phase="transcription",
            payload={
                "transcript_revision_id": revision_id,
                "segment_count": len(payload["segments"]),
                "chunks_total": final_segment_count,
            },
        )
        noise_filter = payload.get("noise_filter")
        if isinstance(noise_filter, Mapping):
            removed_count = noise_filter.get("removed_count")
            if isinstance(removed_count, int) and removed_count > 0:
                self.store.add_event(
                    job.id,
                    "info",
                    "noise filter removed "
                    f"{removed_count} non-speech diarization span(s)",
                )

    def _make_translation_client(
        self,
        servers: RemoteServerSettings,
    ) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(
            servers.lm_base_url,
            servers.lm_token,
            servers.lm_model,
            max_segments=self.settings.translation_batch_segments,
            max_characters=self.settings.translation_batch_characters,
            request_limiter=self._translation_request_limiter,
            request_observer=self.record_external_request,
        )

    def _translation_generation_contract(
        self,
        job: PipelineJob,
        transcript_payload: Mapping[str, Any],
        prompt_snapshot: Mapping[str, Any],
        servers: RemoteServerSettings,
        *,
        model: str | None = None,
        endpoint_key: str | None = None,
    ) -> dict[str, str]:
        if not job.transcript_path:
            raise ValueError("transcript artifact is unavailable")
        transcript_job_id = str(transcript_payload.get("job_id", "")).strip()
        if not transcript_job_id:
            raise ValueError("transcript job_id is unavailable")
        transcript_hash = sha256_file(Path(job.transcript_path))
        prompt_hash = _canonical_payload_hash(dict(prompt_snapshot))
        selected_model = (model or servers.lm_model).strip()
        selected_endpoint = (endpoint_key or servers.lm_base_url).rstrip("/")
        config_hash = _canonical_payload_hash(
            {
                "schema_version": TRANSLATION_SCHEMA_VERSION,
                "transcript_hash": transcript_hash,
                "prompt_hash": prompt_hash,
                "endpoint_key": selected_endpoint,
                "model": selected_model,
                "source_language": "ja",
                "target_language": "ko",
                "batch_segments": self.settings.translation_batch_segments,
                "batch_characters": self.settings.translation_batch_characters,
            }
        )
        return {
            "transcript_job_id": transcript_job_id,
            "transcript_hash": transcript_hash,
            "prompt_hash": prompt_hash,
            "prompt_revision_id": (
                str(prompt_snapshot["revision_id"])
                if prompt_snapshot.get("revision_id")
                else None
            ),
            "endpoint_key": selected_endpoint,
            "model": selected_model,
            "config_hash": config_hash,
        }

    def _create_translation_generation(
        self,
        job: PipelineJob,
        transcript_payload: Mapping[str, Any],
        prompt_snapshot: Mapping[str, Any],
        servers: RemoteServerSettings,
        *,
        origin: str,
        force_new: bool = False,
        model: str | None = None,
        endpoint_key: str | None = None,
    ) -> dict[str, Any]:
        generation_id = uuid4().hex
        contract = self._translation_generation_contract(
            job,
            transcript_payload,
            prompt_snapshot,
            servers,
            model=model,
            endpoint_key=endpoint_key,
        )
        generation_path = (
            self.settings.jobs_dir
            / job.id
            / "translation-generations"
            / f"{generation_id}.json"
        )
        return self.store.create_translation_generation(
            generation_id=generation_id,
            job_id=job.id,
            transcript_revision_id=job.transcript_revision_id,
            artifact_path=str(generation_path),
            origin=origin,
            force_new=force_new,
            **contract,
        )

    @staticmethod
    def _translation_item_records(
        segments: Sequence[Mapping[str, Any]],
        translations: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        segment_by_id = {
            str(segment["id"]): (index, segment)
            for index, segment in enumerate(segments)
        }
        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in translations:
            segment_id = str(item.get("id", "")).strip()
            text = str(item.get("text", "")).strip()
            if (
                not segment_id
                or segment_id in seen
                or segment_id not in segment_by_id
                or not text
            ):
                raise ValueError("translation item does not match transcript")
            seen.add(segment_id)
            segment_index, segment = segment_by_id[segment_id]
            records.append(
                {
                    "id": segment_id,
                    "text": text,
                    "segment_index": segment_index,
                    "source_hash": _canonical_payload_hash(dict(segment)),
                }
            )
        return records

    @staticmethod
    def _translation_source_records(
        segments: Sequence[Mapping[str, Any]],
        segment_ids: Sequence[str],
    ) -> list[dict[str, str]]:
        segment_by_id = {
            str(segment["id"]): segment for segment in segments
        }
        records: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_segment_id in segment_ids:
            segment_id = str(raw_segment_id).strip()
            if (
                not segment_id
                or segment_id in seen
                or segment_id not in segment_by_id
            ):
                raise ValueError("translation batch does not match transcript")
            seen.add(segment_id)
            records.append(
                {
                    "id": segment_id,
                    "source_hash": _canonical_payload_hash(
                        dict(segment_by_id[segment_id])
                    ),
                }
            )
        return records

    @staticmethod
    def _read_translation_checkpoint(
        translation_path: Path,
        expected_ids: set[str],
    ) -> tuple[list[dict[str, str]], int, Mapping[str, Any] | None]:
        if not translation_path.is_file():
            return [], 0, None
        try:
            payload = json.loads(translation_path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("translation checkpoint must be an object")
            raw_items = payload.get("translations", [])
            if not isinstance(raw_items, list):
                raise ValueError("translations must be a list")
            items: list[dict[str, str]] = []
            seen: set[str] = set()
            ignored = 0
            for item in raw_items:
                if not isinstance(item, Mapping):
                    ignored += 1
                    continue
                segment_id = str(item.get("id", "")).strip()
                text = str(item.get("text", "")).strip()
                if (
                    segment_id in expected_ids
                    and segment_id not in seen
                    and text
                ):
                    seen.add(segment_id)
                    items.append({"id": segment_id, "text": text})
                elif segment_id:
                    ignored += 1
            return items, ignored, payload
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return [], 0, None

    @staticmethod
    def _translation_snapshot_items(
        stored_items: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, str]]:
        return [
            {"id": str(item["id"]), "text": str(item["text"])}
            for item in stored_items
        ]

    def _write_translation_generation_snapshot(
        self,
        translation_path: Path,
        generation: Mapping[str, Any],
        *,
        transcript_job_id: str,
        status: str,
        translations: Sequence[Mapping[str, str]],
    ) -> None:
        payload: dict[str, Any] = {
            "schema_version": TRANSLATION_SCHEMA_VERSION,
            "status": status,
            "transcript_job_id": transcript_job_id,
            "generation": {
                "id": generation["id"],
                "number": generation["generation_number"],
                "config_hash": generation["config_hash"],
            },
            "translations": [dict(item) for item in translations],
        }
        if status == "completed":
            payload["model"] = generation["model"]
        write_json_atomic(Path(str(generation["artifact_path"])), payload)
        write_json_atomic(translation_path, payload)

    def _capture_legacy_translation_generation(
        self,
        job: PipelineJob,
        transcript_payload: Mapping[str, Any],
        segments: Sequence[Mapping[str, Any]],
        prompt_snapshot: Mapping[str, Any],
        servers: RemoteServerSettings,
    ) -> dict[str, Any] | None:
        if not job.translation_path:
            return None
        translation_path = Path(job.translation_path)
        expected_ids = {str(segment["id"]) for segment in segments}
        items, _ignored, payload = self._read_translation_checkpoint(
            translation_path,
            expected_ids,
        )
        if payload is None or not items:
            return None
        generation = None
        generation_metadata = payload.get("generation")
        if isinstance(generation_metadata, Mapping):
            saved_generation_id = str(
                generation_metadata.get("id", "")
            ).strip()
            saved_generation = self.store.get_translation_generation(
                saved_generation_id
            )
            if (
                saved_generation is not None
                and saved_generation["job_id"] == job.id
            ):
                generation = saved_generation
        if generation is None:
            payload_model = (
                str(payload.get("model", "")).strip() or servers.lm_model
            )
            generation = self._create_translation_generation(
                job,
                transcript_payload,
                prompt_snapshot,
                servers,
                origin="legacy",
                model=payload_model,
            )
        if not self.store.translation_items(generation["id"]):
            self.store.save_translation_batch(
                generation["id"],
                batch_index=0,
                generation_attempt=0,
                kind="legacy",
                items=self._translation_item_records(segments, items),
            )
        if len(items) == len(segments):
            completed = self.store.complete_translation_generation(
                generation["id"],
                [str(segment["id"]) for segment in segments],
            )
            self._write_translation_generation_snapshot(
                translation_path,
                generation,
                transcript_job_id=str(transcript_payload["job_id"]),
                status="completed",
                translations=completed,
            )
        return generation

    def _translate(self, job: PipelineJob) -> None:
        remote_runtime = self._remote_runtime
        servers = remote_runtime[2]
        if remote_runtime[1] is None:
            raise ExternalServiceError("translation server is not configured")
        lm_client = self._make_translation_client(servers)
        prompt_snapshot = job.options.get(TRANSLATION_PROMPT_OPTION)
        if not isinstance(prompt_snapshot, Mapping):
            prompt_snapshot = self._legacy_prompt_snapshot()
        translation_prompt = str(
            prompt_snapshot.get("translation_prompt", "")
        ).strip() or KOREAN_JAV_SYSTEM_PROMPT
        review_prompt = str(
            prompt_snapshot.get("review_prompt", "")
        ).strip() or KOREAN_TRANSLATION_REVIEW_PROMPT
        try:
            review_rounds = int(prompt_snapshot.get("review_rounds", 0))
        except (TypeError, ValueError):
            review_rounds = 0
        review_rounds = min(TRANSLATION_REVIEW_ROUNDS, max(0, review_rounds))
        if not job.transcript_path:
            raise RuntimeError("transcript artifact is unavailable")
        transcript_payload = json.loads(
            Path(job.transcript_path).read_text(encoding="utf-8")
        )
        segments = validate_transcript(transcript_payload)
        translation_path = (
            Path(job.translation_path)
            if job.translation_path
            else artifact_path(
                self.settings.jobs_dir,
                job.id,
                job.source_rel,
                "translation",
            )
        )
        self._require_stage_update(
            job,
            translation_path=str(translation_path),
        )
        generation = self._create_translation_generation(
            job,
            transcript_payload,
            prompt_snapshot,
            servers,
            origin="automatic",
        )
        expected_id_list = [str(segment["id"]) for segment in segments]
        expected_ids = set(expected_id_list)
        stored_items = self.store.translation_items(generation["id"])
        checkpoint_source = "generation_store" if stored_items else None
        ignored_checkpoint_ids = 0
        if not stored_items and generation["supersedes_generation_id"] is None:
            checkpoint_items, ignored_checkpoint_ids, _payload = (
                self._read_translation_checkpoint(
                    translation_path,
                    expected_ids,
                )
            )
            if checkpoint_items:
                self.store.save_translation_batch(
                    generation["id"],
                    batch_index=0,
                    generation_attempt=0,
                    kind="legacy",
                    items=self._translation_item_records(
                        segments,
                        checkpoint_items,
                    ),
                )
                stored_items = self.store.translation_items(generation["id"])
                checkpoint_source = "legacy_json"
        if stored_items and checkpoint_source is not None:
            self._record_measurement(
                "translation.checkpoint.items",
                len(stored_items),
                labels={
                    "source": checkpoint_source,
                    "outcome": "reused",
                },
            )
        if ignored_checkpoint_ids:
            self._record_measurement(
                "translation.checkpoint.items",
                ignored_checkpoint_ids,
                labels={
                    "source": "legacy_json",
                    "outcome": "invalidated",
                },
            )
            self.store.add_event(
                job.id,
                "warning",
                "ignored "
                f"{ignored_checkpoint_ids} stale translation checkpoint id(s)",
            )

        existing = {
            str(item["id"]): str(item["text"])
            for item in stored_items
        }
        self._write_translation_generation_snapshot(
            translation_path,
            generation,
            transcript_job_id=str(transcript_payload["job_id"]),
            status="partial",
            translations=self._translation_snapshot_items(stored_items),
        )
        lease_owner = (
            self._worker_id if job.lease_owner == self._worker_id else None
        )
        generation_attempt = self.store.begin_translation_generation_attempt(
            generation["id"],
            lease_owner=lease_owner,
            lease_token=(job.lease_token if lease_owner is not None else None),
        )
        next_batch_index = self.store.next_translation_batch_index(
            generation["id"]
        )
        persisted = dict(existing)
        run_batch_indexes: dict[int, int] = {}

        def start_batch(run_batch_index: int, segment_ids: list[str]) -> None:
            nonlocal next_batch_index
            database_batch_index = next_batch_index
            next_batch_index += 1
            run_batch_indexes[run_batch_index] = database_batch_index
            self.store.start_translation_batch(
                generation["id"],
                batch_index=database_batch_index,
                generation_attempt=generation_attempt,
                items=self._translation_source_records(
                    segments,
                    segment_ids,
                ),
            )

        def complete_batch(
            run_batch_index: int,
            items: list[dict[str, str]],
        ) -> None:
            database_batch_index = run_batch_indexes[run_batch_index]
            self.store.save_translation_batch(
                generation["id"],
                batch_index=database_batch_index,
                generation_attempt=generation_attempt,
                kind="remote",
                items=self._translation_item_records(segments, items),
            )
            for item in items:
                persisted[str(item["id"])] = str(item["text"]).strip()

        def fail_batch(
            run_batch_index: int,
            _segment_ids: list[str],
            error: str,
        ) -> None:
            database_batch_index = run_batch_indexes.get(run_batch_index)
            if database_batch_index is None:
                return
            self.store.fail_translation_batch(
                generation["id"],
                batch_index=database_batch_index,
                error=self._sanitize_error(error),
                generation_attempt=generation_attempt,
            )

        def save_batch(items: list[dict[str, str]]) -> None:
            nonlocal next_batch_index
            changed = [
                item
                for item in items
                if persisted.get(str(item["id"])) != str(item["text"]).strip()
            ]
            if changed:
                self.store.save_translation_batch(
                    generation["id"],
                    batch_index=next_batch_index,
                    generation_attempt=generation_attempt,
                    kind="remote",
                    items=self._translation_item_records(segments, changed),
                )
                next_batch_index += 1
                for item in changed:
                    persisted[str(item["id"])] = str(item["text"]).strip()
            current_items = self.store.translation_items(generation["id"])
            self._write_translation_generation_snapshot(
                translation_path,
                generation,
                transcript_job_id=str(transcript_payload["job_id"]),
                status="partial",
                translations=self._translation_snapshot_items(current_items),
            )
        progress_base = self.store.completed_translation_batch_count(
            generation["id"]
        )

        def update_translation_progress(completed: int, total: int) -> None:
            self._require_stage_update(
                job,
                translation_chunks_total=progress_base + total,
                translation_chunks_completed=progress_base + completed,
            )

        def should_pause() -> bool:
            current = self.store.get(job.id)
            return bool(
                current
                and (
                    current.translation_pause_requested
                    or current.job_stop_requested
                )
            )

        def review_warning(message: str) -> None:
            self.store.add_event(
                job.id,
                "warning",
                "translation review failed; using initial translation: "
                f"{self._sanitize_error(message)}",
            )

        try:
            translations = lm_client.translate(
                segments,
                system_prompt=translation_prompt,
                review_prompt=review_prompt,
                review_rounds=review_rounds,
                existing=existing,
                on_batch=save_batch,
                on_batch_started=start_batch,
                on_logical_batch=complete_batch,
                on_batch_failed=fail_batch,
                on_progress=update_translation_progress,
                should_pause=should_pause,
                on_review_warning=review_warning,
                max_workers=servers.translation_workers,
            )
            self._raise_if_job_stop_requested(job.id)
        except TranslationPaused as error:
            self.store.mark_translation_generation(
                generation["id"],
                state="paused",
                error=str(error),
                generation_attempt=generation_attempt,
            )
            raise
        except ExternalServiceError as error:
            self.store.mark_translation_generation(
                generation["id"],
                state="blocked",
                error=self._sanitize_error(str(error)),
                generation_attempt=generation_attempt,
            )
            raise
        except OperationStopped as error:
            self.store.mark_translation_generation(
                generation["id"],
                state="stopped",
                error=str(error),
                generation_attempt=generation_attempt,
            )
            raise
        except Exception as error:
            self.store.mark_translation_generation(
                generation["id"],
                state="failed",
                error=self._sanitize_error(str(error)),
                generation_attempt=generation_attempt,
            )
            raise

        final_changes = [
            item
            for item in translations
            if persisted.get(str(item["id"])) != str(item["text"]).strip()
        ]
        if final_changes:
            self.store.save_translation_batch(
                generation["id"],
                batch_index=next_batch_index,
                generation_attempt=generation_attempt,
                kind="final",
                items=self._translation_item_records(segments, final_changes),
            )
        completed_translations = self.store.complete_translation_generation(
            generation["id"],
            expected_id_list,
            generation_attempt=generation_attempt,
        )
        self._write_translation_generation_snapshot(
            translation_path,
            generation,
            transcript_job_id=str(transcript_payload["job_id"]),
            status="completed",
            translations=completed_translations,
        )
        refreshed = self.store.get(job.id)
        self._require_stage_update(
            job,
            status="translated",
            translation_pause_requested=0,
            translation_chunks_completed=(
                refreshed.translation_chunks_total if refreshed else 0
            ),
        )
        self.store.add_event(
            job.id,
            "info",
            "translation generation "
            f"{generation['generation_number']} completed "
            f"({len(completed_translations)} segments)",
            event_code="stage.completed",
            from_state=JobState.RUNNING.value,
            phase="translation",
            payload={
                "translation_generation_id": generation["id"],
                "generation_number": generation["generation_number"],
                "segment_count": len(completed_translations),
            },
        )

    def _subtitle_generation_artifact_paths(
        self,
        job_id: str,
        generation_id: str,
    ) -> tuple[Path, Path]:
        directory = (
            self.settings.jobs_dir / job_id / "subtitle-generations"
        )
        return (
            directory / f"{generation_id}.srt",
            directory / f"{generation_id}.ass",
        )

    def _subtitle_publication_manifest_path(self, source_rel: str) -> Path:
        source_key = hashlib.sha256(source_rel.encode("utf-8")).hexdigest()
        return (
            self.settings.state_dir
            / "subtitle-publications"
            / f"{source_key}.json"
        )

    @staticmethod
    def _published_subtitle_pair_matches(
        generation: Mapping[str, Any],
        *,
        srt_path: Path,
        ass_path: Path,
    ) -> bool:
        return (
            srt_path.is_file()
            and ass_path.is_file()
            and sha256_file(srt_path) == generation["srt_hash"]
            and sha256_file(ass_path) == generation["ass_hash"]
        )

    def _read_subtitle_publication_manifest(
        self,
        source_rel: str,
    ) -> Mapping[str, Any] | None:
        path = self._subtitle_publication_manifest_path(source_rel)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != 1
            or payload.get("source_rel") != source_rel
        ):
            return None
        return payload

    def _manifest_generation_for_source(
        self,
        source_rel: str,
        manifest: Mapping[str, Any] | None,
        *,
        srt_path: Path,
        ass_path: Path,
    ) -> tuple[PipelineJob, dict[str, Any]] | None:
        if manifest is None:
            return None
        generation_id = str(manifest.get("subtitle_generation_id", ""))
        generation = self.store.get_subtitle_generation(generation_id)
        if generation is None:
            return None
        job = self.store.get(str(generation["job_id"]))
        if job is None or job.source_rel != source_rel:
            return None
        if (
            manifest.get("job_id") != job.id
            or manifest.get("srt_name") != srt_path.name
            or manifest.get("ass_name") != ass_path.name
            or manifest.get("srt_hash") != generation["srt_hash"]
            or manifest.get("ass_hash") != generation["ass_hash"]
            or manifest.get("transcript_hash")
            != generation["transcript_hash"]
            or manifest.get("translation_hash")
            != generation["translation_hash"]
            or manifest.get("renderer_version")
            != generation["renderer_version"]
            or manifest.get("render_hash") != generation["render_hash"]
            or not self._subtitle_generation_files_valid(generation)
            or not self._published_subtitle_pair_matches(
                generation,
                srt_path=srt_path,
                ass_path=ass_path,
            )
        ):
            return None
        return job, generation

    def _reconcile_subtitle_publications(self) -> int:
        repaired = 0
        failed = 0
        with self._subtitle_publication_lock:
            for generation in (
                self.store.list_recoverable_subtitle_generations()
            ):
                try:
                    repaired += int(
                        self._recover_subtitle_generation_publication(
                            generation
                        )
                    )
                except (OSError, RuntimeError, ValueError):
                    failed += 1
                    LOGGER.exception(
                        "subtitle generation recovery failed for %s",
                        generation["source_rel"],
                    )
            for publication in self.store.list_subtitle_publications():
                try:
                    repaired += int(
                        self._reconcile_subtitle_publication(publication)
                    )
                except (OSError, RuntimeError, ValueError):
                    failed += 1
                    LOGGER.exception(
                        "subtitle publication reconcile failed for %s",
                        publication["source_rel"],
                    )
        self._record_measurement(
            "recovery.subtitle_publications",
            repaired,
            labels={"outcome": "repaired"},
        )
        self._record_measurement(
            "recovery.subtitle_publications",
            failed,
            labels={"outcome": "failed"},
        )
        return repaired

    def _recover_subtitle_generation_publication(
        self,
        generation: Mapping[str, Any],
    ) -> bool:
        source_rel = str(generation["source_rel"])
        job_id = str(generation["job_id"])
        lease_token = self.store.claim_recovery_lease(
            job_id,
            "rendering",
            lease_owner=self._worker_id,
            lease_seconds=JOB_LEASE_SECONDS,
        )
        if lease_token is None:
            return False
        job = self.store.get(job_id)
        if job is None or job.source_rel != source_rel:
            self.store.release_job_lease(
                job_id,
                lease_owner=self._worker_id,
                lease_token=lease_token,
            )
            raise RuntimeError("render recovery job is unavailable")
        source = self.library.resolve_file(source_rel)
        srt_path = source.with_name(f"{source.stem}.ko.srt")
        ass_path = source.with_name(f"{source.stem}.ko.ass")
        try:
            self._publish_subtitle_pair_locked(
                job,
                generation,
                srt_path=srt_path,
                ass_path=ass_path,
                overwrite=True,
            )
        except BaseException:
            self.store.release_job_lease(
                job_id,
                lease_owner=self._worker_id,
                lease_token=lease_token,
            )
            raise
        return True

    def _reconcile_subtitle_publication(
        self,
        publication: Mapping[str, Any],
    ) -> bool:
        source_rel = str(publication["source_rel"])
        source = self.library.resolve_file(source_rel)
        srt_path = source.with_name(f"{source.stem}.ko.srt")
        ass_path = source.with_name(f"{source.stem}.ko.ass")
        manifest_generation = self._manifest_generation_for_source(
            source_rel,
            self._read_subtitle_publication_manifest(source_rel),
            srt_path=srt_path,
            ass_path=ass_path,
        )
        if manifest_generation is not None:
            manifest_job, manifest_target = manifest_generation
            if (
                manifest_target["id"] == publication["id"]
                and manifest_job.srt_path == str(srt_path)
                and manifest_job.ass_path == str(ass_path)
            ):
                return False
            self.store.publish_subtitle_generation(
                str(manifest_target["id"]),
                srt_path=str(srt_path),
                ass_path=str(ass_path),
            )
            return True

        published_job = self.store.get(str(publication["job_id"]))
        if published_job is None:
            raise RuntimeError("published subtitle job is unavailable")
        if not self._subtitle_generation_files_valid(publication):
            raise RuntimeError(
                "published subtitle generation artifacts are invalid"
            )
        if not self._published_subtitle_pair_matches(
            publication,
            srt_path=srt_path,
            ass_path=ass_path,
        ):
            copy_files_atomic(
                (
                    (
                        Path(str(publication["srt_artifact_path"])),
                        srt_path,
                    ),
                    (
                        Path(str(publication["ass_artifact_path"])),
                        ass_path,
                    ),
                ),
                overwrite=True,
            )
        self._write_subtitle_publication_manifest(
            published_job,
            publication,
            srt_path=srt_path,
            ass_path=ass_path,
        )
        self.store.publish_subtitle_generation(
            str(publication["id"]),
            srt_path=str(srt_path),
            ass_path=str(ass_path),
        )
        return True

    @staticmethod
    def _subtitle_generation_files_valid(
        generation: Mapping[str, Any],
    ) -> bool:
        srt_artifact = Path(str(generation["srt_artifact_path"]))
        ass_artifact = Path(str(generation["ass_artifact_path"]))
        return (
            srt_artifact.is_file()
            and ass_artifact.is_file()
            and sha256_file(srt_artifact) == generation["srt_hash"]
            and sha256_file(ass_artifact) == generation["ass_hash"]
        )

    def _write_subtitle_publication_manifest(
        self,
        job: PipelineJob,
        generation: Mapping[str, Any],
        *,
        srt_path: Path,
        ass_path: Path,
    ) -> None:
        if (
            not srt_path.is_file()
            or not ass_path.is_file()
            or sha256_file(srt_path) != generation["srt_hash"]
            or sha256_file(ass_path) != generation["ass_hash"]
        ):
            raise RuntimeError("published subtitle pair is incomplete")
        write_json_atomic(
            self._subtitle_publication_manifest_path(job.source_rel),
            {
                "schema_version": 1,
                "source_rel": job.source_rel,
                "job_id": job.id,
                "subtitle_generation_id": generation["id"],
                "srt_name": srt_path.name,
                "ass_name": ass_path.name,
                "srt_hash": generation["srt_hash"],
                "ass_hash": generation["ass_hash"],
                "transcript_hash": generation["transcript_hash"],
                "translation_hash": generation["translation_hash"],
                "renderer_version": generation["renderer_version"],
                "render_hash": generation["render_hash"],
            },
        )

    def _publish_subtitle_pair_locked(
        self,
        job: PipelineJob,
        generation: Mapping[str, Any],
        *,
        srt_path: Path,
        ass_path: Path,
        overwrite: bool,
        copy_to_media: bool = True,
    ) -> dict[str, Any]:
        if not self._subtitle_generation_files_valid(generation):
            raise ValueError("subtitle generation file is invalid")
        lease_owner = (
            self._worker_id
            if job.lease_owner == self._worker_id and job.lease_token > 0
            else None
        )
        lease_token = job.lease_token if lease_owner is not None else None
        lock_path = self._subtitle_publication_manifest_path(
            job.source_rel
        ).with_suffix(".lock")
        with exclusive_file_lock(lock_path):
            if lease_owner is not None and lease_token is not None:
                if not self.store.lease_is_active(
                    job.id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                ):
                    raise WorkerLeaseLost("worker lease was superseded")
            if copy_to_media:
                copy_files_atomic(
                    (
                        (
                            Path(str(generation["srt_artifact_path"])),
                            srt_path,
                        ),
                        (
                            Path(str(generation["ass_artifact_path"])),
                            ass_path,
                        ),
                    ),
                    overwrite=overwrite,
                )
            if lease_owner is not None and lease_token is not None:
                if not self.store.refresh_job_lease(
                    job.id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    lease_seconds=JOB_LEASE_SECONDS,
                ):
                    raise WorkerLeaseLost("worker lease was superseded")
            self._write_subtitle_publication_manifest(
                job,
                generation,
                srt_path=srt_path,
                ass_path=ass_path,
            )
            return self.store.publish_subtitle_generation(
                str(generation["id"]),
                srt_path=str(srt_path),
                ass_path=str(ass_path),
                lease_owner=lease_owner,
                lease_token=lease_token,
            )

    def _capture_legacy_subtitle_generation(
        self,
        job: PipelineJob,
        *,
        translation_generation_id: str | None,
    ) -> dict[str, Any] | None:
        with self._subtitle_publication_lock:
            return self._capture_legacy_subtitle_generation_locked(
                job,
                translation_generation_id=translation_generation_id,
            )

    def _capture_legacy_subtitle_generation_locked(
        self,
        job: PipelineJob,
        *,
        translation_generation_id: str | None,
    ) -> dict[str, Any] | None:
        published = self.store.published_subtitle_generation(job.id)
        if published is not None:
            return published
        if (
            not job.transcript_path
            or not job.translation_path
            or not job.srt_path
            or not job.ass_path
        ):
            return None
        transcript_path = Path(job.transcript_path)
        translation_path = Path(job.translation_path)
        srt_path = Path(job.srt_path)
        ass_path = Path(job.ass_path)
        if not all(
            path.is_file()
            for path in (
                transcript_path,
                translation_path,
                srt_path,
                ass_path,
            )
        ):
            return None

        generation_id = uuid4().hex
        srt_artifact, ass_artifact = (
            self._subtitle_generation_artifact_paths(
                job.id,
                generation_id,
            )
        )
        copy_files_atomic(
            (
                (srt_path, srt_artifact),
                (ass_path, ass_artifact),
            ),
            overwrite=False,
        )
        transcript_hash = sha256_file(transcript_path)
        translation_hash = sha256_file(translation_path)
        srt_hash = sha256_file(srt_artifact)
        ass_hash = sha256_file(ass_artifact)
        generation = self.store.create_subtitle_generation(
            generation_id=generation_id,
            job_id=job.id,
            translation_generation_id=translation_generation_id,
            transcript_hash=transcript_hash,
            translation_hash=translation_hash,
            renderer_version="legacy",
            render_hash=_canonical_payload_hash(
                {
                    "transcript_hash": transcript_hash,
                    "translation_hash": translation_hash,
                    "srt_hash": srt_hash,
                    "ass_hash": ass_hash,
                    "renderer_version": "legacy",
                }
            ),
            srt_artifact_path=str(srt_artifact),
            ass_artifact_path=str(ass_artifact),
            srt_hash=srt_hash,
            ass_hash=ass_hash,
            origin="legacy",
        )
        return self._publish_subtitle_pair_locked(
            job,
            generation,
            srt_path=srt_path,
            ass_path=ass_path,
            overwrite=False,
            copy_to_media=False,
        )

    @staticmethod
    def _translation_generation_id_from_snapshot(
        job_id: str,
        translation_payload: Mapping[str, Any],
        store: JobStore,
    ) -> str | None:
        metadata = translation_payload.get("generation")
        if not isinstance(metadata, Mapping):
            return None
        generation_id = str(metadata.get("id", "")).strip()
        generation = store.get_translation_generation(generation_id)
        if generation is None or generation["job_id"] != job_id:
            return None
        return generation_id

    def publish_subtitle_generation(
        self,
        job_id: str,
        generation_id: str,
    ) -> PipelineJob:
        job = self.store.get(job_id)
        if job is None:
            raise ValueError("job not found")
        if job.status != "completed":
            raise ValueError("completed subtitles can be published")
        generation = self.store.get_subtitle_generation(generation_id)
        if generation is None or generation["job_id"] != job.id:
            raise ValueError("subtitle generation not found")
        srt_artifact = Path(str(generation["srt_artifact_path"])).resolve()
        ass_artifact = Path(str(generation["ass_artifact_path"])).resolve()
        job_root = (self.settings.jobs_dir / job.id).resolve()
        try:
            srt_artifact.relative_to(job_root)
            ass_artifact.relative_to(job_root)
        except ValueError as error:
            raise ValueError("subtitle generation path is invalid") from error
        if not self._subtitle_generation_files_valid(generation):
            raise ValueError("subtitle generation file is invalid")
        source = self.library.resolve_file(job.source_rel)
        srt_path = source.with_name(f"{source.stem}.ko.srt")
        ass_path = source.with_name(f"{source.stem}.ko.ass")
        with self._subtitle_publication_lock:
            self._publish_subtitle_pair_locked(
                job,
                generation,
                srt_path=srt_path,
                ass_path=ass_path,
                overwrite=True,
            )
        self.store.add_event(
            job.id,
            "info",
            "subtitle generation "
            f"{generation['generation_number']} published",
            event_code="subtitle.published",
            phase="complete",
            payload={
                "subtitle_generation_id": generation["id"],
                "generation_number": generation["generation_number"],
            },
        )
        published_job = self.store.get(job.id)
        if published_job is None:
            raise RuntimeError("published subtitle job could not be read")
        return published_job

    def _render(self, job: PipelineJob) -> None:
        self._render_artifacts(
            job,
            overwrite=(
                job.force_overwrite
                or bool(job.srt_path)
                or bool(job.ass_path)
            ),
        )
        refreshed = self.store.get(job.id)
        if (
            refreshed is None
            or not refreshed.srt_path
            or not refreshed.ass_path
        ):
            raise RuntimeError("rendered subtitle job could not be read")
        self.store.add_event(
            job.id,
            "info",
            "subtitles written: "
            f"{Path(refreshed.srt_path).name}, "
            f"{Path(refreshed.ass_path).name}",
            event_code="stage.completed",
            from_state=JobState.RUNNING.value,
            phase="render",
            payload={
                "srt_filename": Path(refreshed.srt_path).name,
                "ass_filename": Path(refreshed.ass_path).name,
            },
        )

    def _render_artifacts(
        self,
        job: PipelineJob,
        *,
        overwrite: bool,
    ) -> None:
        if not job.transcript_path or not job.translation_path:
            raise RuntimeError("subtitle artifacts are unavailable")
        transcript_payload = json.loads(
            Path(job.transcript_path).read_text(encoding="utf-8")
        )
        translation_payload = json.loads(
            Path(job.translation_path).read_text(encoding="utf-8")
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
        source = self.library.resolve_file(job.source_rel)
        srt_path = source.with_name(f"{source.stem}.ko.srt")
        ass_path = source.with_name(f"{source.stem}.ko.ass")
        if not overwrite:
            for path in (srt_path, ass_path):
                if path.exists():
                    raise FileExistsError(f"subtitle already exists: {path}")

        generation_id = uuid4().hex
        srt_artifact, ass_artifact = (
            self._subtitle_generation_artifact_paths(
                job.id,
                generation_id,
            )
        )
        timeline = write_styled_subtitles_atomic(
            srt_artifact,
            ass_artifact,
            segments,
            translations,
            overwrite=False,
        )
        transcript_path = Path(job.transcript_path)
        translation_path = Path(job.translation_path)
        transcript_hash = sha256_file(transcript_path)
        translation_hash = sha256_file(translation_path)
        generation = self.store.create_subtitle_generation(
            generation_id=generation_id,
            job_id=job.id,
            translation_generation_id=(
                self._translation_generation_id_from_snapshot(
                    job.id,
                    translation_payload,
                    self.store,
                )
            ),
            transcript_hash=transcript_hash,
            translation_hash=translation_hash,
            renderer_version=SUBTITLE_RENDERER_VERSION,
            render_hash=_canonical_payload_hash(
                {
                    "transcript_hash": transcript_hash,
                    "translation_hash": translation_hash,
                    "renderer_version": SUBTITLE_RENDERER_VERSION,
                }
            ),
            srt_artifact_path=str(srt_artifact),
            ass_artifact_path=str(ass_artifact),
            srt_hash=sha256_file(srt_artifact),
            ass_hash=sha256_file(ass_artifact),
            origin="rendered",
        )
        with self._subtitle_publication_lock:
            self._publish_subtitle_pair_locked(
                job,
                generation,
                srt_path=srt_path,
                ass_path=ass_path,
                overwrite=overwrite,
            )
        if timeline.repaired_segment_ids:
            self.store.add_event(
                job.id,
                "warning",
                "repaired "
                f"{len(timeline.repaired_segment_ids)} legacy or abnormal "
                "subtitle timestamp(s)",
            )

    def _sanitize_error(self, message: str) -> str:
        sanitized = message
        for secret in {
            self.settings.stt_token,
            self.settings.lm_token,
            self.remote_servers.stt_token,
            self.remote_servers.lm_token,
            self._subtitle_validator.token,
        }:
            if secret:
                sanitized = sanitized.replace(secret, "[redacted]")
        return sanitized[:2000]

    def sanitize_external_error(self, message: str) -> str:
        return self._sanitize_error(message)
