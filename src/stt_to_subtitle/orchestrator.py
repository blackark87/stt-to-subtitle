"""Stage-based web orchestration with independent bounded workers."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import nullcontext
from dataclasses import asdict, replace
import hashlib
import json
import logging
import math
from pathlib import Path
import re
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
    HYBRID_STABLE_SUBTITLE_SEGMENTATION,
    HybridRescueOptions,
    OWSMAuditOptions,
    TranscriptionOptions,
    WHISPERX_MAX_BATCH_SIZE,
    WHISPERX_MAX_CHUNK_LENGTH_SECONDS,
    WHISPERX_MIN_BATCH_SIZE,
    WhisperJAVOptions,
    WhisperXSegmentationOptions,
)
from .path_display import PathDisplayRule
from .prompt_improvement import (
    PROMPT_DRAFT_INSTRUCTION_VERSION,
    PROMPT_DRAFT_SYSTEM_PROMPT,
    PROMPT_IMPROVEMENT_INSTRUCTION_VERSION,
    PROMPT_IMPROVEMENT_SYSTEM_PROMPT,
    improvement_request_payload,
    parse_improvement_result,
    parse_prompt_draft_result,
    prompt_draft_request_payload,
    split_feedback_by_job,
)
from .resource_groups import ResourceGroupLimiter
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
    media_duration_bucket_minutes,
)
from .job_state import JobReason, JobState
from .service_clients import (
    EXTERNAL_MODEL_DEFAULT_URLS,
    ExternalStructuredCompletionClient,
    ExternalServiceError,
    OpenAICompatibleClient,
    OperationStopped,
    RemoteTranscriptionFailed,
    STTAPIClient,
    SubtitleValidationClient,
    TranslationDeferred,
    TranslationPaused,
    external_review_client,
    list_external_models,
)
from .subtitle import write_styled_subtitles_atomic
from .subtitle_validation import build_subtitle_validator_payload
from .translation_prompt import (
    KOREAN_EXTERNAL_EDITOR_PROMPT,
    KOREAN_JAV_DRAFT_PROMPT,
    KOREAN_JAV_EXTERNAL_EDITOR_PROMPT,
    KOREAN_JAV_REVIEW_PROMPT,
    KOREAN_VARIETY_EXTERNAL_EDITOR_PROMPT,
)
from .translation_routing import (
    BackendTranslationRouting,
    TranslationRoutingDefaults,
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
    "external review": "external_review",
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
    "http://runtime:8100",
}


def _transcriber_identity(
    readiness: Mapping[str, Any] | object,
) -> Mapping[str, Any] | None:
    if not isinstance(readiness, Mapping):
        return None
    identity = readiness.get("transcriber") or readiness.get("runtime")
    return identity if isinstance(identity, Mapping) else None


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
TRANSLATION_EXECUTION_MODE_OPTION = "translation_execution_mode"
TRANSLATION_REVIEW_ROUNDS = 1
TRANSLATION_MODES = frozenset({
    "draft_only",
    "review_existing",
    "draft_and_review",
})
SUBTITLE_RENDERER_VERSION = "1"
SUPPORTED_OPERATIONS = {
    "extract",
    "transcribe",
    "translate",
    "full",
    "draft_translate",
    "review_translate",
    "external_review",
}
TRANSLATION_OPERATIONS = {
    "translate",
    "full",
    "draft_translate",
    "review_translate",
    "external_review",
}
LOCAL_TRANSLATION_OPERATIONS = {
    "translate",
    "full",
    "draft_translate",
    "review_translate",
}
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
    if str(options.get("backend", "hybrid")) == "hybrid":
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


SUPPORTED_STT_BACKENDS = {
    "hybrid",
    "whisperjav",
}


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
        self.settings.transcription_audio_dir.mkdir(parents=True, exist_ok=True)
        self.library = MediaLibrary(
            settings.media_root,
            settings.maximum_listed_files,
            duration_probe=probe_media_duration,
        )
        self.store = JobStore(settings.state_dir / "jobs.sqlite3")
        self._path_display_rules = tuple(
            self.store.list_path_display_rules()
        )
        rebased_paths = 0
        for previous_root in (
            settings.state_dir / "jobs",
            Path("/var/lib/stt-work"),
            Path("/var/lib/stt/jobs"),
        ):
            rebased_paths += self.store.rebase_artifact_paths(
                previous_root=previous_root,
                current_root=settings.jobs_dir,
                current_audio_root=settings.transcription_audio_dir,
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
        self._resource_groups = ResourceGroupLimiter()
        self._translation_routing = BackendTranslationRouting(
            TranslationRoutingDefaults(
                state_dir=settings.translation_dir,
                builtin_name=settings.translation_builtin_name,
                builtin_base_url=settings.translation_builtin_base_url,
                builtin_token=settings.translation_builtin_token,
                builtin_capacity=settings.translation_builtin_capacity,
                draft_enabled=settings.translation_builtin_draft_enabled,
                review_enabled=settings.translation_builtin_review_enabled,
                draft_batch_preferred=(
                    settings.translation_builtin_draft_batch_preferred
                ),
                review_batch_preferred=(
                    settings.translation_builtin_review_batch_preferred
                ),
                connect_timeout_seconds=(
                    settings.translation_connect_timeout_seconds
                ),
                read_timeout_seconds=settings.translation_read_timeout_seconds,
                stt_hard_breaker_hosts=(
                    settings.translation_stt_hard_breaker_hosts
                ),
                stt_hard_breaker_timeout_seconds=(
                    settings.translation_stt_hard_breaker_timeout_seconds
                ),
            ),
            stt_hard_breaker_active=(
                self._builtin_transcription_uses_shared_memory
            ),
            resource_groups=self._resource_groups,
        )
        self._remote_runtime: tuple[
            STTAPIClient | None,
            RemoteServerSettings,
        ] = (None, initial_servers)
        self._stt_gate_lock = threading.RLock()
        self._runtime_lock = threading.RLock()
        self._runtime_clients: dict[str, STTAPIClient] = {}
        self._runtime_health: dict[str, dict[str, Any]] = {}
        self._runtime_dispatch_cursor = 0
        self._translation_circuit_lock = threading.RLock()
        self._phase_creation_lock = threading.RLock()
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
            if (
                self._translation_routing.is_configured("draft")
                or self._translation_routing.is_configured("review")
            )
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
        self._refresh_resource_group_limits()
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
            # Draft and review use separate job lanes but share host locks.
            # External validation has its own lane and no local GPU lock.
            max_workers=3,
            thread_name_prefix="pipeline-translation",
        )
        self._prompt_improvement_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="prompt-improvement",
        )

    @property
    def stt_client(self) -> STTAPIClient | None:
        return self._remote_runtime[0]

    @property
    def remote_servers(self) -> RemoteServerSettings:
        return self._remote_runtime[1]

    @property
    def remote_servers_configured(self) -> bool:
        return self.transcription_server_configured

    @property
    def transcription_server_configured(self) -> bool:
        with self._runtime_lock:
            return bool(self._runtime_clients)

    @property
    def translation_server_configured(self) -> bool:
        return self._translation_routing.is_configured()

    def _builtin_transcription_uses_shared_memory(self) -> bool:
        if not self.settings.translation_stt_hard_breaker_hosts:
            return False
        try:
            counts = self.store.transcription_runtime_counts()
        except (OSError, RuntimeError, sqlite3.Error):
            LOGGER.exception("failed to read built-in transcription count")
            return True
        return counts.get(BUILTIN_RUNTIME_ID, 0) > 0

    def remote_servers_view(self) -> dict[str, Any]:
        servers = self.remote_servers
        with self._stt_gate_lock:
            stt_gate_state = self._stt_gate_state
            stt_gate_message = self._stt_gate_message
        return {
            "stt_base_url": servers.stt_base_url,
            "stt_token_configured": bool(servers.stt_token),
            "configured": self.remote_servers_configured,
            "transcription_configured": self.transcription_server_configured,
            "translation_configured": self.translation_server_configured,
            "stt_gate_state": stt_gate_state,
            "stt_gate_message": stt_gate_message,
        }

    def translation_groups_view(self) -> list[dict[str, Any]]:
        return self._translation_routing.groups()

    def _refresh_resource_group_limits(self) -> None:
        capacities: dict[str, int] = {}
        entries = [
            *self._runtime_definitions(),
            *(
                server
                for group in self._translation_routing.groups()
                for server in group["servers"]
            ),
        ]
        for entry in entries:
            group_id = str(entry.get("resource_group_id", "local-gpu"))
            capacity = max(1, int(entry.get("capacity", 1)))
            capacities[group_id] = min(
                capacity,
                capacities.get(group_id, capacity),
            )
        self._resource_groups.configure(capacities)

    def update_translation_server_model(
        self,
        stage: str,
        endpoint_id: str,
        model: str,
    ) -> dict[str, Any]:
        result = self._translation_routing.update_server_model(
            stage,
            endpoint_id,
            model,
        )
        self._refresh_translation_circuit_from_routing()
        return result

    def create_translation_endpoint(
        self,
        stage: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = self._translation_routing.create_server(stage, payload)
        self._refresh_resource_group_limits()
        self._refresh_translation_circuit_from_routing()
        return result

    def update_translation_endpoint(
        self,
        stage: str,
        endpoint_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = self._translation_routing.update_server(
            stage,
            endpoint_id,
            payload,
        )
        self._refresh_resource_group_limits()
        self._refresh_translation_circuit_from_routing()
        return result

    def delete_translation_endpoint(self, stage: str, endpoint_id: str) -> None:
        self._translation_routing.delete_server(stage, endpoint_id)
        self._refresh_resource_group_limits()
        self._refresh_translation_circuit_from_routing()

    def probe_translation_endpoint(
        self,
        stage: str,
        endpoint_id: str,
    ) -> dict[str, Any]:
        result = self._translation_routing.probe_server(stage, endpoint_id)
        self._refresh_translation_circuit_from_routing()
        return result

    def update_translation_endpoint_routing(
        self,
        stage: str,
        endpoint_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = self._translation_routing.update_routing(
            stage,
            endpoint_id,
            payload,
        )
        self._refresh_translation_circuit_from_routing()
        return result

    def _refresh_translation_circuit_from_routing(self) -> None:
        self._set_translation_circuit(
            "ready"
            if (
                self._translation_routing.is_configured("draft")
                or self._translation_routing.is_configured("review")
            )
            else "offline"
        )

    @staticmethod
    def _validate_runtime_values(
        *,
        name: str,
        base_url: str,
        capacity: int,
    ) -> tuple[str, str, int]:
        normalized_name = name.strip()
        if not normalized_name or len(normalized_name) > 80:
            raise ValueError("전사 서버 이름은 1~80자여야 합니다.")
        if not 1 <= capacity <= 8:
            raise ValueError("전사 서버 동시 작업 수는 1~8이어야 합니다.")
        normalized_url = normalize_server_url(base_url, "RUNTIME_BASE_URL")
        return normalized_name, normalized_url, capacity

    @staticmethod
    def _validate_resource_group_id(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 80:
            raise ValueError("리소스 그룹 ID는 1~80자여야 합니다.")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", normalized):
            raise ValueError(
                "리소스 그룹 ID는 영문자 또는 숫자로 시작하고 "
                "영문자, 숫자, 점, 밑줄, 하이픈만 사용할 수 있습니다."
            )
        return normalized

    @staticmethod
    def _validate_runtime_batch_size(
        value: int | None,
        *,
        name: str,
    ) -> int | None:
        if value is None:
            return None
        if not WHISPERX_MIN_BATCH_SIZE <= value <= WHISPERX_MAX_BATCH_SIZE:
            raise ValueError(f"{name} 배치 크기는 1~64여야 합니다.")
        return value

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
        batch_settings = self.store.runtime_batch_settings()
        builtin_batches = batch_settings.get(BUILTIN_RUNTIME_ID, {})
        if servers.stt_is_complete:
            definitions.append(
                {
                    "id": BUILTIN_RUNTIME_ID,
                    "name": "기본 전사 서버",
                    "base_url": servers.stt_base_url,
                    "token": servers.stt_token,
                    "enabled": True,
                    "capacity": 1,
                    "resource_group_id": servers.resource_group_id,
                    "kotoba_batch_size": builtin_batches.get(
                        "kotoba_batch_size"
                    ),
                    "whisperx_batch_size": builtin_batches.get(
                        "whisperx_batch_size"
                    ),
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
                "resource_group_id": endpoint.resource_group_id,
                "kotoba_batch_size": batch_settings.get(
                    endpoint.id, {}
                ).get("kotoba_batch_size"),
                "whisperx_batch_size": batch_settings.get(
                    endpoint.id, {}
                ).get("whisperx_batch_size"),
                "builtin": False,
            }
            for endpoint in self.store.list_runtime_endpoints()
        )
        return definitions

    def _runtime_definition(self, runtime_id: str) -> dict[str, Any]:
        for definition in self._runtime_definitions():
            if definition["id"] == runtime_id:
                return definition
        raise ValueError("전사 서버를 찾을 수 없습니다.")

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
                message or "사용 가능한 전사 서버가 없습니다.",
                reason_code=reason_code or JobReason.STT_UNAVAILABLE.value,
            )
        elif definitions:
            self._set_stt_gate("unknown", "연결 확인이 필요합니다.")
        else:
            self._set_stt_gate("offline", "전사 서버가 등록되지 않았습니다.")

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
                    "resource_group_id": definition["resource_group_id"],
                    "kotoba_batch_size": definition["kotoba_batch_size"],
                    "whisperx_batch_size": definition[
                        "whisperx_batch_size"
                    ],
                    "builtin": bool(definition["builtin"]),
                    "status": current.get("status", "unknown"),
                    "message": current.get("message"),
                    "checked_at": current.get("checked_at"),
                    "running_jobs": running,
                    "available_slots": max(0, capacity - running),
                    "identity": _transcriber_identity(readiness),
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
        resource_group_id: str = "local-gpu",
        kotoba_batch_size: int | None = None,
        whisperx_batch_size: int | None = None,
    ) -> dict[str, Any]:
        name, base_url, capacity = self._validate_runtime_values(
            name=name,
            base_url=base_url,
            capacity=capacity,
        )
        resource_group_id = self._validate_resource_group_id(
            resource_group_id
        )
        kotoba_batch_size = self._validate_runtime_batch_size(
            kotoba_batch_size,
            name="Kotoba",
        )
        whisperx_batch_size = self._validate_runtime_batch_size(
            whisperx_batch_size,
            name="WhisperX",
        )
        with self._runtime_lock:
            if (
                len(self.store.list_runtime_endpoints())
                >= MAX_RUNTIME_ENDPOINTS - 1
            ):
                raise ValueError("등록 가능한 외부 전사 서버 수를 초과했습니다.")
            if base_url == self.remote_servers.stt_base_url:
                raise ValueError("기본 전사 서버와 같은 주소는 등록할 수 없습니다.")
            endpoint = self.store.create_runtime_endpoint(
                name=name,
                base_url=base_url,
                token=token,
                enabled=enabled,
                capacity=capacity,
                resource_group_id=resource_group_id,
            )
            self.store.save_runtime_batch_settings(
                endpoint.id,
                kotoba_batch_size=kotoba_batch_size,
                whisperx_batch_size=whisperx_batch_size,
            )
            self._install_runtime_endpoint(endpoint)
            self._refresh_resource_group_limits()
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
        resource_group_id: str = "local-gpu",
        kotoba_batch_size: int | None = None,
        whisperx_batch_size: int | None = None,
        clear_kotoba_batch_size: bool = False,
        clear_whisperx_batch_size: bool = False,
    ) -> dict[str, Any]:
        name, base_url, capacity = self._validate_runtime_values(
            name=name,
            base_url=base_url,
            capacity=capacity,
        )
        resource_group_id = self._validate_resource_group_id(
            resource_group_id
        )
        kotoba_batch_size = self._validate_runtime_batch_size(
            kotoba_batch_size,
            name="Kotoba",
        )
        whisperx_batch_size = self._validate_runtime_batch_size(
            whisperx_batch_size,
            name="WhisperX",
        )
        with self._runtime_lock:
            definition = self._runtime_definition(runtime_id)
            current_kotoba_batch_size = definition["kotoba_batch_size"]
            current_whisperx_batch_size = definition["whisperx_batch_size"]
            resolved_kotoba_batch_size = (
                None
                if clear_kotoba_batch_size
                else current_kotoba_batch_size
                if kotoba_batch_size is None
                else kotoba_batch_size
            )
            resolved_whisperx_batch_size = (
                None
                if clear_whisperx_batch_size
                else current_whisperx_batch_size
                if whisperx_batch_size is None
                else whisperx_batch_size
            )
            if runtime_id == BUILTIN_RUNTIME_ID:
                if (
                    name != definition["name"]
                    or base_url != definition["base_url"]
                    or not enabled
                    or capacity != definition["capacity"]
                    or clear_token
                    or token is not None
                ):
                    raise ValueError(
                        "기본 전사 서버는 GPU 공유 그룹과 배치 크기만 "
                        "변경할 수 있습니다."
                    )
                if resource_group_id != definition["resource_group_id"]:
                    self._set_remote_servers(
                        RemoteServerSettings(
                            stt_base_url=self.remote_servers.stt_base_url,
                            stt_token=self.remote_servers.stt_token,
                            resource_group_id=resource_group_id,
                        ),
                        persist=True,
                    )
                self.store.save_runtime_batch_settings(
                    runtime_id,
                    kotoba_batch_size=resolved_kotoba_batch_size,
                    whisperx_batch_size=resolved_whisperx_batch_size,
                )
                self._refresh_resource_group_limits()
                return self._runtime_view(runtime_id)
            current = self.store.get_runtime_endpoint(runtime_id)
            if current is None:
                raise ValueError("전사 서버를 찾을 수 없습니다.")
            if self.store.runtime_has_active_transcriptions(runtime_id) and (
                not enabled or base_url != current.base_url
            ):
                raise ValueError(
                    "진행 중인 작업이 있는 전사 서버 설정은 변경할 수 없습니다."
                )
            if base_url == self.remote_servers.stt_base_url:
                raise ValueError("기본 전사 서버와 같은 주소는 등록할 수 없습니다.")
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
                resource_group_id=resource_group_id,
            )
            self.store.save_runtime_batch_settings(
                runtime_id,
                kotoba_batch_size=resolved_kotoba_batch_size,
                whisperx_batch_size=resolved_whisperx_batch_size,
            )
            self._install_runtime_endpoint(updated)
            self._refresh_resource_group_limits()
        if enabled:
            self.probe_runtime_endpoint(runtime_id)
        return self._runtime_view(runtime_id)

    def delete_runtime_endpoint(self, runtime_id: str) -> None:
        if runtime_id == BUILTIN_RUNTIME_ID:
            raise ValueError("기본 전사 서버는 삭제할 수 없습니다.")
        with self._runtime_lock:
            if self.store.runtime_has_active_transcriptions(runtime_id):
                raise ValueError(
                    "진행 중인 작업이 있는 전사 서버는 삭제할 수 없습니다."
                )
            self.store.delete_runtime_endpoint(runtime_id)
            self._runtime_clients.pop(runtime_id, None)
            self._runtime_health.pop(runtime_id, None)
            self._refresh_resource_group_limits()
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
            raise ValueError("비활성화된 전사 서버는 확인할 수 없습니다.")
        self._set_runtime_health(runtime_id, "checking")
        client = self._runtime_client(runtime_id)
        if client is None:
            raise ValueError("전사 서버 연결 설정이 없습니다.")
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
            identity = _transcriber_identity(readiness)
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
                        and _transcriber_identity(current["readiness"])
                        is not None
                        and str(
                            _transcriber_identity(
                                current["readiness"]
                            ).get("id", "")
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
                    message="같은 전사 서버 ID가 이미 등록되어 있습니다.",
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

    @staticmethod
    def _external_model_defaults(provider: str) -> str:
        try:
            return EXTERNAL_MODEL_DEFAULT_URLS[provider]
        except KeyError as error:
            raise ValueError("지원하지 않는 외부 모델 제공자입니다.") from error

    @staticmethod
    def _external_model_public(
        profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "provider": str(profile["provider"]),
            "base_url": str(profile["base_url"]),
            "credential_configured": bool(profile.get("credential")),
            "region": str(profile["region"]),
            "selected_model": str(profile["selected_model"]),
            "models": list(profile.get("models", [])),
            "status": str(profile["status"]),
            "message": profile.get("message"),
            "checked_at": profile.get("checked_at"),
            "updated_at": profile.get("updated_at"),
            "configured": bool(
                profile.get("credential")
                and profile.get("selected_model")
                and profile.get("status") == "ready"
            ),
        }

    def external_model_profiles_view(self) -> list[dict[str, Any]]:
        stored = {
            str(profile["provider"]): profile
            for profile in self.store.list_external_model_profiles()
        }
        now = time.time()
        return [
            self._external_model_public(
                stored.get(provider)
                or {
                    "provider": provider,
                    "base_url": self._external_model_defaults(provider),
                    "credential": "",
                    "region": "",
                    "selected_model": "",
                    "models": [],
                    "status": "unchecked",
                    "message": None,
                    "checked_at": None,
                    "updated_at": now,
                }
            )
            for provider in ("openrouter", "bedrock", "nvidia_build")
        ]

    def update_external_model_profile(
        self,
        provider: str,
        *,
        base_url: str,
        credential: str | None,
        clear_credential: bool,
        region: str,
    ) -> dict[str, Any]:
        default_url = self._external_model_defaults(provider)
        current = self.store.get_external_model_profile(provider) or {}
        stored_credential = (
            ""
            if clear_credential
            else credential
            if credential is not None
            else str(current.get("credential", ""))
        )
        normalized_base_url = (
            ""
            if provider == "bedrock"
            else default_url
            if provider == "openrouter"
            else normalize_server_url(
                base_url.strip() or default_url,
                "EXTERNAL_MODEL_BASE_URL",
            )
        )
        normalized_region = region.strip().lower() if provider == "bedrock" else ""
        profile = self.store.save_external_model_profile(
            provider=provider,
            base_url=normalized_base_url,
            credential=stored_credential,
            region=normalized_region,
            selected_model=str(current.get("selected_model", "")),
            models=current.get("models", ()),
            status="unchecked",
            message="연결 점검 후 저장된 모델을 다시 확인합니다.",
        )
        return self._external_model_public(profile)

    def probe_external_model_profile(
        self,
        provider: str,
    ) -> dict[str, Any]:
        profile = self.store.get_external_model_profile(provider)
        if profile is None:
            raise ValueError("외부 모델 제공자 설정을 먼저 저장하세요.")
        try:
            models = list_external_models(
                provider=provider,
                base_url=str(profile["base_url"]),
                credential=str(profile["credential"]),
                region=str(profile["region"]),
                request_observer=self.record_external_request,
            )
            if not models:
                raise ExternalServiceError(
                    "인증은 성공했지만 사용할 수 있는 텍스트 모델이 없습니다."
                )
        except (ExternalServiceError, ValueError) as error:
            sanitized = self._sanitize_error(str(error))
            self.store.save_external_model_profile(
                provider=provider,
                base_url=str(profile["base_url"]),
                credential=str(profile["credential"]),
                region=str(profile["region"]),
                selected_model="",
                models=(),
                status="failed",
                message=sanitized,
                checked_at=time.time(),
            )
            raise ValueError(sanitized) from error
        selected = str(profile.get("selected_model", ""))
        if selected not in models:
            selected = ""
        ready = self.store.save_external_model_profile(
            provider=provider,
            base_url=str(profile["base_url"]),
            credential=str(profile["credential"]),
            region=str(profile["region"]),
            selected_model=selected,
            models=models,
            status="ready",
            message=f"인증 완료 · {len(models)}개 모델 사용 가능",
            checked_at=time.time(),
        )
        return self._external_model_public(ready)

    def select_external_model(
        self,
        provider: str,
        model: str,
    ) -> dict[str, Any]:
        profile = self.store.get_external_model_profile(provider)
        selected = model.strip()
        if profile is None or profile["status"] != "ready":
            raise ValueError("먼저 외부 모델 제공자의 연결을 점검하세요.")
        if selected not in profile["models"]:
            raise ValueError("연결 점검에서 확인된 모델만 선택할 수 있습니다.")
        saved = self.store.save_external_model_profile(
            provider=provider,
            base_url=str(profile["base_url"]),
            credential=str(profile["credential"]),
            region=str(profile["region"]),
            selected_model=selected,
            models=profile["models"],
            status="ready",
            message=str(profile.get("message") or "인증 완료"),
            checked_at=profile.get("checked_at"),
        )
        return self._external_model_public(saved)

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
                "사용 가능한 전사 서버가 없습니다.",
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

    def translation_feedback_view(
        self,
        *,
        category_id: str | None = None,
        stage: str | None = None,
        included: bool | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_translation_feedback(
            category_id=category_id,
            stage=stage,
            included=included,
        )

    def set_translation_feedback_included(
        self,
        feedback_id: str,
        *,
        included: bool,
    ) -> dict[str, Any]:
        return self.store.set_translation_feedback_included(
            feedback_id,
            included=included,
        )

    @staticmethod
    def prompt_authoring_view() -> dict[str, str]:
        return {
            "improvement_instruction_version": (
                PROMPT_IMPROVEMENT_INSTRUCTION_VERSION
            ),
            "improvement_system_prompt": PROMPT_IMPROVEMENT_SYSTEM_PROMPT,
            "draft_instruction_version": PROMPT_DRAFT_INSTRUCTION_VERSION,
            "draft_system_prompt": PROMPT_DRAFT_SYSTEM_PROMPT,
        }

    def _external_generation_profile(
        self,
        *,
        provider: str,
        model: str,
    ) -> dict[str, Any]:
        profile = self.store.get_external_model_profile(provider)
        selected_model = model.strip()
        if (
            profile is None
            or profile["status"] != "ready"
            or not profile["credential"]
            or not selected_model
            or selected_model not in profile["models"]
        ):
            raise ValueError(
                "외부 모델 제공자를 점검하고 목록에 있는 모델을 선택하세요."
            )
        return profile

    def _external_generation_client(
        self,
        *,
        provider: str,
        model: str,
    ) -> ExternalStructuredCompletionClient:
        profile = self._external_generation_profile(
            provider=provider,
            model=model,
        )
        return ExternalStructuredCompletionClient(
            provider=provider,
            base_url=str(profile["base_url"]),
            credential=str(profile["credential"]),
            model=model,
            region=str(profile["region"]),
            request_observer=self.record_external_request,
        )

    def create_prompt_draft(
        self,
        *,
        name: str,
        domain_description: str,
        provider: str,
        model: str,
    ) -> dict[str, str]:
        normalized_name = name.strip()
        normalized_description = domain_description.strip()
        if not normalized_name:
            raise ValueError("프롬프트 이름을 입력하세요.")
        if not normalized_description:
            raise ValueError("도메인 설명을 입력하세요.")
        client = self._external_generation_client(
            provider=provider,
            model=model,
        )
        result = parse_prompt_draft_result(
            client.complete(
                prompt_draft_request_payload(
                    name=normalized_name,
                    domain_description=normalized_description,
                )
            )
        )
        return {
            **result,
            "provider": provider,
            "model": model,
            "instruction_version": PROMPT_DRAFT_INSTRUCTION_VERSION,
        }

    def create_prompt_improvement(
        self,
        *,
        category_id: str,
        stage: str,
        provider: str,
        model: str,
    ) -> dict[str, Any]:
        category = self.store.get_prompt_category(category_id)
        if category is None:
            raise ValueError("프롬프트 카테고리를 찾을 수 없습니다.")
        if stage not in {"translation", "review"}:
            raise ValueError("개선 단계는 translation 또는 review여야 합니다.")
        self._external_generation_profile(provider=provider, model=model)
        feedback = [
            item
            for item in self.store.list_translation_feedback(
                category_id=category_id,
                stage=stage,
                included=True,
                limit=1000,
            )
            if item["base_revision_id"] == category.prompt_revision_id
        ]
        train, holdout = split_feedback_by_job(
            feedback,
            seed=f"{category_id}:{stage}:{category.prompt_revision_id}",
        )
        run = self.store.create_prompt_improvement_run(
            category_id=category_id,
            stage=stage,
            base_revision_id=category.prompt_revision_id,
            train_feedback_ids=[str(item["id"]) for item in train],
            holdout_feedback_ids=[str(item["id"]) for item in holdout],
            endpoint_contract=provider,
            model_contract=model,
        )
        self._prompt_improvement_executor.submit(
            self._run_prompt_improvement,
            str(run["id"]),
        )
        return run

    def _run_prompt_improvement(self, run_id: str) -> None:
        if not self.store.claim_prompt_improvement_run(run_id):
            return
        try:
            run = self.store.get_prompt_improvement_run(run_id)
            if run is None:
                return
            revision = self.store.prompt_revision_by_id(
                str(run["base_revision_id"])
            )
            if revision is None:
                raise ValueError("기준 프롬프트 revision을 찾을 수 없습니다.")
            feedback = {
                str(item["id"]): item
                for item in self.store.list_translation_feedback(
                    category_id=str(run["category_id"]),
                    stage=str(run["stage"]),
                    included=None,
                    current_only=False,
                    limit=1000,
                )
            }
            train = [
                feedback[feedback_id]
                for feedback_id in run["train_feedback_ids"]
                if feedback_id in feedback
            ]
            holdout = [
                feedback[feedback_id]
                for feedback_id in run["holdout_feedback_ids"]
                if feedback_id in feedback
            ]
            expected_count = len(run["train_feedback_ids"]) + len(
                run["holdout_feedback_ids"]
            )
            if len(train) + len(holdout) != expected_count:
                raise ValueError("선택된 번역 피드백을 모두 찾을 수 없습니다.")
            prompt_field = (
                "translation_prompt"
                if run["stage"] == "translation"
                else "review_prompt"
            )
            provider = str(run["endpoint_contract"])
            model = str(run["model_contract"])
            client = self._external_generation_client(
                provider=provider,
                model=model,
            )
            proposed_prompt, evaluation = parse_improvement_result(
                client.complete(
                    improvement_request_payload(
                        stage=str(run["stage"]),
                        current_prompt=str(revision[prompt_field]),
                        train=train,
                        holdout=holdout,
                    )
                )
            )
            self.store.complete_prompt_improvement_run(
                run_id,
                proposed_prompt=proposed_prompt,
                evaluation=evaluation,
            )
        except Exception as error:
            self.store.fail_prompt_improvement_run(
                run_id,
                self._sanitize_error(str(error) or error.__class__.__name__),
            )
            LOGGER.exception("prompt improvement run %s failed", run_id)

    def prompt_improvement_runs_view(
        self,
        *,
        category_id: str | None = None,
        stage: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_prompt_improvement_runs(
            category_id=category_id,
            stage=stage,
        )

    def cancel_prompt_improvement(self, run_id: str) -> dict[str, Any]:
        return self.store.set_prompt_improvement_run_status(
            run_id,
            from_statuses={"queued", "running"},
            status="cancelled",
        )

    def reject_prompt_improvement(self, run_id: str) -> dict[str, Any]:
        return self.store.set_prompt_improvement_run_status(
            run_id,
            from_statuses={"ready"},
            status="rejected",
        )

    def activate_prompt_improvement(
        self,
        run_id: str,
    ) -> dict[str, Any]:
        category, run = self.store.activate_prompt_improvement_run(run_id)
        return {"category": asdict(category), "run": run}

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
            "translation_mode": "draft_and_review",
            "target_stage": "review",
            "review_rounds": TRANSLATION_REVIEW_ROUNDS,
        }

    @staticmethod
    def _legacy_prompt_snapshot() -> dict[str, Any]:
        return {
            "category_id": "jav",
            "category_name": "JAV (기존 작업)",
            "revision_id": None,
            "revision_number": None,
            "translation_prompt": KOREAN_JAV_DRAFT_PROMPT,
            "review_prompt": KOREAN_JAV_REVIEW_PROMPT,
            "translation_mode": "draft_only",
            "target_stage": "draft",
            "review_rounds": 0,
        }

    @staticmethod
    def _translation_mode(
        prompt_snapshot: Mapping[str, Any],
    ) -> str:
        explicit = str(
            prompt_snapshot.get("translation_mode", "")
        ).strip()
        if explicit in TRANSLATION_MODES:
            return explicit
        if str(prompt_snapshot.get("target_stage", "")).strip() == "draft":
            return "draft_only"
        try:
            review_rounds = int(prompt_snapshot.get("review_rounds", 0))
        except (TypeError, ValueError):
            review_rounds = 0
        return "draft_and_review" if review_rounds > 0 else "draft_only"

    @staticmethod
    def _prompt_snapshot_for_mode(
        prompt_snapshot: Mapping[str, Any],
        translation_mode: str,
        *,
        review_source_generation_id: str | None = None,
    ) -> dict[str, Any]:
        if translation_mode not in TRANSLATION_MODES:
            raise ValueError("지원하지 않는 번역 실행 방식입니다.")
        snapshot = dict(prompt_snapshot)
        snapshot["translation_mode"] = translation_mode
        snapshot["target_stage"] = (
            "draft" if translation_mode == "draft_only" else "review"
        )
        snapshot["review_rounds"] = (
            0
            if translation_mode == "draft_only"
            else TRANSLATION_REVIEW_ROUNDS
        )
        snapshot.pop("review_source_generation_id", None)
        if translation_mode == "review_existing":
            if not review_source_generation_id:
                raise ValueError("2차 보정에 사용할 1차 번역 결과가 없습니다.")
            snapshot["review_source_generation_id"] = (
                review_source_generation_id
            )
        return snapshot

    def _require_translation_mode_routes(
        self,
        translation_mode: str,
        execution_mode: str,
        *,
        error_type: type[Exception] = ValueError,
    ) -> None:
        required_stages = (
            ("draft",)
            if translation_mode == "draft_only"
            else ("review",)
            if translation_mode == "review_existing"
            else ("draft", "review")
        )
        labels = {"draft": "1차 번역", "review": "2차 보정"}
        missing = [
            labels[stage]
            for stage in required_stages
            if not self._translation_routing.is_configured(
                stage,
                execution_mode,
            )
        ]
        if missing:
            raise error_type(
                f"{', '.join(missing)} 서버 설정이 필요합니다."
            )

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
        if any(
            endpoint.base_url == normalized.stt_base_url
            for endpoint in self.store.list_runtime_endpoints()
        ):
            raise ValueError("외부 전사 서버와 같은 주소를 기본값으로 설정할 수 없습니다.")
        stt_client = STTAPIClient(
            normalized.stt_base_url,
            normalized.stt_token,
            request_observer=self.record_external_request,
        )
        if persist:
            self.store.save_remote_server_settings(
                stt_base_url=normalized.stt_base_url,
                stt_token=normalized.stt_token,
                resource_group_id=normalized.resource_group_id,
            )
        self._remote_runtime = (stt_client, normalized)
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
        for run in self.store.list_prompt_improvement_runs(limit=1000):
            if run["status"] == "running":
                self.store.fail_prompt_improvement_run(
                    str(run["id"]),
                    "서비스 재시작으로 개선 작업이 중단되었습니다. 다시 요청하세요.",
                )
            elif run["status"] == "queued":
                self._prompt_improvement_executor.submit(
                    self._run_prompt_improvement,
                    str(run["id"]),
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
            self._prompt_improvement_executor,
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
        is_test: bool = False,
        options: Mapping[str, Any],
        operation: str = "full",
        prompt_category_id: str | None = None,
    ) -> PipelineJob:
        return self.create_jobs(
            [source_rel],
            force_overwrite=force_overwrite,
            is_test=is_test,
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
        if operation not in SUPPORTED_OPERATIONS:
            raise ValueError("unsupported job operation")
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
        is_test: bool = False,
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
            normalized_options[TRANSLATION_EXECUTION_MODE_OPTION] = (
                "batch" if len(unique_source_rels) > 1 else "live"
            )
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
                    is_test=int(is_test or reusable_audio.is_test),
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
                            execution_mode=str(
                                normalized_options[
                                    TRANSLATION_EXECUTION_MODE_OPTION
                                ]
                            ),
                        )
                    )
                    continue
                reusable_options = dict(reusable.options)
                reusable_options[TRANSLATION_PROMPT_OPTION] = (
                    normalized_options[TRANSLATION_PROMPT_OPTION]
                )
                reusable_options[TRANSLATION_EXECUTION_MODE_OPTION] = (
                    normalized_options[TRANSLATION_EXECUTION_MODE_OPTION]
                )
                created = self.store.create(
                    job_id=uuid4().hex,
                    source_rel=source_rel,
                    force_overwrite=force_overwrite,
                    is_test=(is_test or reusable.is_test),
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
                    is_test=is_test,
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
        """Queue one validated transcription comparison profile per source."""
        raise ValueError("전사 비교 작업 생성은 더 이상 지원하지 않습니다.")
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

        comparison_profile = str(
            options.get("comparison_profile", "whisperjav")
        ).strip().lower()
        if comparison_profile == "whisperjav":
            variants = TRANSCRIPTION_COMPARISON_VARIANTS
            schema_version = TRANSCRIPTION_COMPARISON_SCHEMA_VERSION
        elif comparison_profile == HYBRID_OWSM_COMPARISON_PROFILE:
            variants = HYBRID_OWSM_COMPARISON_VARIANTS
            schema_version = HYBRID_OWSM_COMPARISON_SCHEMA_VERSION
        else:
            raise ValueError(
                "comparison_profile must be 'whisperjav' or "
                f"'{HYBRID_OWSM_COMPARISON_PROFILE}'"
            )

        common_options = dict(options)
        for key in (
            "backend",
            "batch_size",
            "kotoba_batch_size",
            "subtitle_segmentation",
            "repetition_policy",
            "repetition_min_count",
            "hybrid_rescue",
            "owsm_audit",
            "whisperjav",
            "comparison_id",
            "comparison_profile",
            "comparison_schema_version",
            "comparison_backends",
            "comparison_variants",
            "comparison_variant_id",
            "comparison_variant_label",
            "comparison_variant_order",
        ):
            common_options.pop(key, None)

        normalized_variants: list[
            tuple[Mapping[str, Any], dict[str, Any]]
        ] = []
        for variant in variants:
            variant_options = {
                **common_options,
                "backend": str(variant.get("backend", "whisperjav")),
                "subtitle_segmentation": dict(
                    variant["subtitle_segmentation"]
                ),
            }
            for option_name in (
                "chunk_length_seconds",
                "hybrid_rescue",
                "owsm_audit",
                "whisperjav",
            ):
                if option_name in variant:
                    value = variant[option_name]
                    variant_options[option_name] = (
                        dict(value) if isinstance(value, Mapping) else value
                    )
            normalized_variants.append(
                (variant, self._normalize_options(variant_options))
            )

        desired_audio_signature = _audio_extraction_signature(
            normalized_variants[0][1]
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
            for variant_order, (variant, normalized_options) in enumerate(
                normalized_variants
            ):
                persisted_options = dict(normalized_options)
                persisted_options["comparison_id"] = comparison_id
                persisted_options["comparison_schema_version"] = (
                    schema_version
                )
                persisted_options["comparison_profile"] = comparison_profile
                persisted_options["comparison_backends"] = list(
                    dict.fromkeys(
                        str(item.get("backend", "whisperjav"))
                        for item in variants
                    )
                )
                persisted_options["comparison_variants"] = [
                    str(item["id"])
                    for item in variants
                ]
                persisted_options["comparison_variant_id"] = str(
                    variant["id"]
                )
                persisted_options["comparison_variant_label"] = str(
                    variant["label"]
                )
                persisted_options["comparison_variant_order"] = variant_order
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
                    is_test=True,
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
        translation_mode: str = "draft_and_review",
        target_stage: str | None = None,
    ) -> list[PipelineJob]:
        """Queue draft, existing-draft review, or combined translation."""
        if target_stage is not None:
            if target_stage not in {"draft", "review"}:
                raise ValueError("지원하지 않는 번역 단계입니다.")
            translation_mode = (
                "draft_only"
                if target_stage == "draft"
                else "draft_and_review"
            )
        if translation_mode not in TRANSLATION_MODES:
            raise ValueError("지원하지 않는 번역 실행 방식입니다.")
        selected_ids = list(
            dict.fromkeys(job_id.strip() for job_id in job_ids if job_id.strip())
        )
        if not selected_ids:
            raise ValueError("번역할 전사 완료 작업을 하나 이상 선택하세요.")
        if len(selected_ids) > self.settings.maximum_listed_files:
            raise ValueError("한 번에 등록할 수 있는 파일 수를 초과했습니다.")

        execution_mode = "batch" if len(selected_ids) > 1 else "live"
        self._require_translation_mode_routes(
            translation_mode,
            execution_mode,
        )
        prompt_snapshot = (
            self._prompt_snapshot_for_mode(
                self._prompt_snapshot(prompt_category_id),
                translation_mode,
            )
            if translation_mode != "review_existing"
            else None
        )
        latest_jobs = self.store.latest_jobs_by_source()
        selections: list[tuple[PipelineJob, str | None]] = []
        for job_id in selected_ids:
            job = self.store.get(job_id)
            if job is None:
                raise ValueError("선택한 작업을 찾을 수 없습니다.")
            latest = latest_jobs.get(job.source_rel)
            if latest is None or latest.id != job.id:
                raise ValueError(
                    f"{job.source_rel}: 최신 작업만 번역할 수 있습니다."
                )
            if translation_mode == "review_existing":
                current_prompt = job.options.get(TRANSLATION_PROMPT_OPTION)
                if not isinstance(current_prompt, Mapping):
                    current_prompt = self._legacy_prompt_snapshot()
                if (
                    job.status != "completed"
                    or job.operation not in TRANSLATION_OPERATIONS
                    or self._translation_mode(current_prompt) != "draft_only"
                ):
                    raise ValueError(
                        f"{job.source_rel}: 1차 번역만 완료된 작업만 "
                        "2차 보정할 수 있습니다."
                    )
            elif not job.can_start_translation:
                raise ValueError(
                    f"{job.source_rel}: 최신 전사 완료 작업만 번역할 수 "
                    "있습니다."
                )
            self.library.resolve_file(job.source_rel)
            transcript_path = Path(job.transcript_path or "")
            try:
                transcript_payload, segments, _transcript_job_id = (
                    self._load_translation_transcript(transcript_path)
                )
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
            review_source_generation_id = None
            if translation_mode == "review_existing":
                source_generation, _source_items = (
                    self._review_source_generation(
                        job,
                        transcript_payload,
                        segments,
                        current_prompt,
                    )
                )
                review_source_generation_id = str(source_generation["id"])
            selections.append((job, review_source_generation_id))

        transitioned_jobs: list[PipelineJob] = []
        for reusable, review_source_generation_id in selections:
            if translation_mode == "review_existing":
                restarted = self.restart_translation(
                    reusable.id,
                    prompt_category_id=prompt_category_id,
                    translation_mode=translation_mode,
                    review_source_generation_id=(
                        review_source_generation_id
                    ),
                    execution_mode=execution_mode,
                )
                self.store.add_event(
                    reusable.id,
                    "info",
                    "selected completed first-pass translation continued "
                    "in review queue",
                )
                transitioned_jobs.append(restarted)
            else:
                if prompt_snapshot is None:
                    raise RuntimeError("translation prompt is unavailable")
                transitioned_jobs.append(
                    self._continue_completed_transcription(
                        reusable,
                        prompt_snapshot=prompt_snapshot,
                        force_overwrite=True,
                        event_message=(
                            "selected completed transcription continued in "
                            "translation queue"
                        ),
                        execution_mode=execution_mode,
                    )
                )
        return transitioned_jobs

    def _create_independent_translation_job(
        self,
        source_job: PipelineJob,
        *,
        operation: str,
        prompt_snapshot: Mapping[str, Any],
        execution_mode: str,
        input_translation_generation_id: str | None = None,
        comparison_translation_generation_id: str | None = None,
        comparison_translation_job_id: str | None = None,
        external_model: Mapping[str, str] | None = None,
    ) -> PipelineJob:
        if not source_job.transcript_path:
            raise ValueError("입력 작업에 전사 결과가 없습니다.")
        source_transcript = Path(source_job.transcript_path)
        transcript_payload, segments, _transcript_job_id = (
            self._load_translation_transcript(source_transcript)
        )
        options = dict(source_job.options)
        options[TRANSLATION_PROMPT_OPTION] = dict(prompt_snapshot)
        options[TRANSLATION_EXECUTION_MODE_OPTION] = execution_mode
        options["pipeline_parent_job_id"] = source_job.id
        options["input_transcript_revision_id"] = (
            source_job.transcript_revision_id
        )
        options["input_translation_generation_id"] = (
            input_translation_generation_id
        )
        options["comparison_translation_generation_id"] = (
            comparison_translation_generation_id
        )
        options["comparison_translation_job_id"] = (
            comparison_translation_job_id
        )
        source_subtitles = self.store.list_subtitle_generations(source_job.id)
        options["input_subtitle_generation_id"] = (
            str(source_subtitles[-1]["id"]) if source_subtitles else None
        )
        if external_model is not None:
            options["external_model"] = dict(external_model)

        created = self.store.create(
            job_id=uuid4().hex,
            source_rel=source_job.source_rel,
            force_overwrite=True,
            is_test=source_job.is_test,
            options=options,
            operation=operation,
            audio_path=source_job.audio_path,
            audio_sha256=source_job.audio_sha256,
            audio_revision_id=source_job.audio_revision_id,
        )
        revision_id = uuid4().hex
        transcript_path = (
            self.settings.jobs_dir
            / created.id
            / "transcript-revisions"
            / revision_id
            / artifact_filename(created.source_rel, "transcript")
        )
        copy_files_atomic(
            ((source_transcript, transcript_path),),
            overwrite=False,
        )
        persisted = self.store.record_transcript_revision(
            revision_id=revision_id,
            job_id=created.id,
            audio_revision_id=source_job.audio_revision_id,
            remote_job_id=source_job.stt_job_id,
            backend=str(source_job.options.get("backend", "imported")),
            model_revision="imported",
            options_hash=_canonical_payload_hash(source_job.options),
            artifact_path=str(transcript_path),
            content_hash=sha256_file(transcript_path),
            origin="imported",
            status="transcribed",
            chunks_total=len(segments),
        )
        if not persisted:
            raise RuntimeError("단계 입력 전사 리비전을 저장하지 못했습니다.")
        self.store.add_event(
            created.id,
            "info",
            f"independent {operation} job created from {source_job.id}",
            event_code="pipeline.phase.created",
            phase={
                "draft_translate": "draft_translation",
                "review_translate": "review_translation",
                "external_review": "external_review",
            }[operation],
            payload={
                "parent_job_id": source_job.id,
                "input_transcript_revision_id": (
                    source_job.transcript_revision_id
                ),
                "input_translation_generation_id": (
                    input_translation_generation_id
                ),
                "comparison_translation_generation_id": (
                    comparison_translation_generation_id
                ),
            },
        )
        refreshed = self.store.get(created.id)
        if refreshed is None:
            raise RuntimeError("생성한 단계 작업을 읽지 못했습니다.")
        return refreshed

    def create_phase_translation_jobs(
        self,
        job_ids: Sequence[str],
        *,
        prompt_category_id: str,
        stage: str,
    ) -> list[PipelineJob]:
        """Create independent draft or review jobs from strict predecessors."""

        if stage not in {"draft", "review"}:
            raise ValueError("지원하지 않는 번역 단계입니다.")
        if not self.translation_server_configured:
            raise ValueError("번역 서버 설정이 필요합니다.")
        selected_ids = list(dict.fromkeys(value for value in job_ids if value))
        if not selected_ids:
            raise ValueError("처리할 이전 단계 작업을 하나 이상 선택하세요.")
        execution_mode = "batch" if len(selected_ids) > 1 else "live"
        mode = "draft_only" if stage == "draft" else "review_existing"
        self._require_translation_mode_routes(mode, execution_mode)
        base_prompt = self._prompt_snapshot(prompt_category_id)
        prepared: list[tuple[PipelineJob, str | None]] = []
        for job_id in selected_ids:
            source_job = self.store.get(job_id)
            if source_job is None:
                raise ValueError("선택한 작업을 찾을 수 없습니다.")
            source_generation_id: str | None = None
            if stage == "draft":
                if (
                    source_job.operation != "transcribe"
                    or source_job.status != "transcription_completed"
                    or not source_job.transcript_path
                ):
                    raise ValueError(
                        f"{source_job.source_rel}: 완료된 전사 작업만 "
                        "1차 번역할 수 있습니다."
                    )
            else:
                prompt = source_job.options.get(TRANSLATION_PROMPT_OPTION)
                is_legacy_draft = (
                    source_job.operation in {"translate", "full"}
                    and isinstance(prompt, Mapping)
                    and self._translation_mode(prompt) == "draft_only"
                )
                if (
                    source_job.status != "completed"
                    or (
                        source_job.operation != "draft_translate"
                        and not is_legacy_draft
                    )
                ):
                    raise ValueError(
                        f"{source_job.source_rel}: 완료된 1차 번역 작업만 "
                        "2차 번역할 수 있습니다."
                    )
                generation = self.store.latest_translation_generation(
                    source_job.id
                )
                if generation is None and is_legacy_draft:
                    transcript_payload, segments, _transcript_job_id = (
                        self._load_translation_transcript(
                            Path(source_job.transcript_path or "")
                        )
                    )
                    generation = self._capture_legacy_translation_generation(
                        source_job,
                        transcript_payload,
                        segments,
                        prompt,
                    )
                if generation is None or generation["state"] != "completed":
                    raise ValueError("완료된 1차 번역 세대가 없습니다.")
                source_generation_id = str(generation["id"])
            prepared.append((source_job, source_generation_id))

        operation = (
            "draft_translate" if stage == "draft" else "review_translate"
        )
        with self._phase_creation_lock:
            existing_jobs = self.store.list_jobs(limit=None)
            for source_job, source_generation_id in prepared:
                duplicate = next(
                    (
                        candidate
                        for candidate in existing_jobs
                        if candidate.operation == operation
                        and str(
                            candidate.options.get(
                                "pipeline_parent_job_id",
                                "",
                            )
                            or ""
                        )
                        == source_job.id
                        and str(
                            candidate.options.get(
                                "input_translation_generation_id",
                                "",
                            )
                            or ""
                        )
                        == str(source_generation_id or "")
                    ),
                    None,
                )
                if duplicate is not None:
                    phase_label = "1차" if stage == "draft" else "2차"
                    raise ValueError(
                        f"동일한 입력의 {phase_label} 번역 작업이 이미 "
                        f"있습니다: {duplicate.id}. 기존 작업을 사용하거나 "
                        "실패한 작업을 재시도하세요."
                    )

            created_jobs: list[PipelineJob] = []
            for source_job, source_generation_id in prepared:
                prompt_snapshot = self._prompt_snapshot_for_mode(
                    base_prompt,
                    mode,
                    review_source_generation_id=source_generation_id,
                )
                created = self._create_independent_translation_job(
                    source_job,
                    operation=operation,
                    prompt_snapshot=prompt_snapshot,
                    execution_mode=execution_mode,
                    input_translation_generation_id=source_generation_id,
                )
                created_jobs.append(created)
                existing_jobs.append(created)
        return created_jobs

    def create_external_review_jobs(
        self,
        job_ids: Sequence[str],
        *,
        provider: str,
        model: str,
    ) -> list[PipelineJob]:
        """Create external review jobs that remain unpublished until approved."""

        profile = self.store.get_external_model_profile(provider)
        requested_model = model.strip()
        selected_model = (
            str(profile.get("selected_model", "")).strip()
            if profile is not None
            else ""
        )
        if (
            profile is None
            or profile["status"] != "ready"
            or not profile["credential"]
            or not selected_model
            or selected_model not in profile["models"]
        ):
            raise ValueError(
                "외부 모델 제공자를 점검하고 사용 가능한 모델을 선택하세요."
            )
        if requested_model != selected_model:
            raise ValueError(
                "요청 모델이 설정에 저장된 외부 검토 모델과 일치하지 않습니다."
            )
        selected_ids = list(dict.fromkeys(value for value in job_ids if value))
        if not selected_ids:
            raise ValueError("완료된 2차 번역 작업을 하나 이상 선택하세요.")
        prepared: list[tuple[PipelineJob, str, str, str]] = []
        for job_id in selected_ids:
            source_job = self.store.get(job_id)
            if (
                source_job is None
                or source_job.operation != "review_translate"
                or source_job.status != "completed"
            ):
                raise ValueError("완료된 2차 번역 작업만 외부 검토할 수 있습니다.")
            generation = self.store.latest_translation_generation(source_job.id)
            if generation is None or generation["state"] != "completed":
                raise ValueError("완료된 2차 번역 세대가 없습니다.")
            prior_generation_id = str(
                source_job.options.get(
                    "input_translation_generation_id",
                    "",
                )
                or ""
            ).strip()
            prior_job_id = str(
                source_job.options.get("pipeline_parent_job_id", "") or ""
            ).strip()
            prior_generation = self.store.get_translation_generation(
                prior_generation_id
            )
            if (
                not prior_generation_id
                or not prior_job_id
                or prior_generation is None
                or prior_generation["state"] != "completed"
                or str(prior_generation["job_id"]) != prior_job_id
            ):
                raise ValueError(
                    "외부 교정에서 비교할 완료된 1차 번역 세대가 없습니다."
                )
            prepared.append(
                (
                    source_job,
                    str(generation["id"]),
                    prior_generation_id,
                    prior_job_id,
                )
            )

        with self._phase_creation_lock:
            existing_jobs = self.store.list_jobs(limit=None)
            for (
                source_job,
                generation_id,
                _prior_generation_id,
                _prior_job_id,
            ) in prepared:
                duplicate = next(
                    (
                        candidate
                        for candidate in existing_jobs
                        if candidate.operation == "external_review"
                        and str(
                            candidate.options.get(
                                "pipeline_parent_job_id",
                                "",
                            )
                            or ""
                        )
                        == source_job.id
                        and str(
                            candidate.options.get(
                                "input_translation_generation_id",
                                "",
                            )
                            or ""
                        )
                        == generation_id
                    ),
                    None,
                )
                if duplicate is not None:
                    raise ValueError(
                        "동일한 입력의 외부 모델 검토 작업이 이미 "
                        f"있습니다: {duplicate.id}. 기존 작업을 사용하거나 "
                        "실패한 작업을 재시도하세요."
                    )

            created_jobs: list[PipelineJob] = []
            for (
                source_job,
                generation_id,
                prior_generation_id,
                prior_job_id,
            ) in prepared:
                source_prompt = source_job.options.get(
                    TRANSLATION_PROMPT_OPTION
                )
                base_prompt = (
                    dict(source_prompt)
                    if isinstance(source_prompt, Mapping)
                    else self._legacy_prompt_snapshot()
                )
                base_prompt["review_prompt"] = {
                    "jav": KOREAN_JAV_EXTERNAL_EDITOR_PROMPT,
                    "variety": KOREAN_VARIETY_EXTERNAL_EDITOR_PROMPT,
                }.get(
                    str(base_prompt.get("category_id", "")).strip(),
                    KOREAN_EXTERNAL_EDITOR_PROMPT,
                )
                prompt_snapshot = self._prompt_snapshot_for_mode(
                    base_prompt,
                    "review_existing",
                    review_source_generation_id=generation_id,
                )
                created = self._create_independent_translation_job(
                    source_job,
                    operation="external_review",
                    prompt_snapshot=prompt_snapshot,
                    execution_mode=(
                        "batch" if len(prepared) > 1 else "live"
                    ),
                    input_translation_generation_id=generation_id,
                    comparison_translation_generation_id=(
                        prior_generation_id
                    ),
                    comparison_translation_job_id=prior_job_id,
                    external_model={
                        "provider": provider,
                        "model": selected_model,
                    },
                )
                created_jobs.append(created)
                existing_jobs.append(created)
        return created_jobs

    def _continue_completed_transcription(
        self,
        job: PipelineJob,
        *,
        prompt_snapshot: Mapping[str, Any],
        force_overwrite: bool,
        event_message: str,
        execution_mode: str,
    ) -> PipelineJob:
        """Continue translation in a completed transcription's job record."""
        options = dict(job.options)
        options[TRANSLATION_PROMPT_OPTION] = dict(prompt_snapshot)
        options[TRANSLATION_EXECUTION_MODE_OPTION] = execution_mode
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
        raise ValueError("전사 비교 전용 번역은 더 이상 지원하지 않습니다.")
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

        prompt_snapshot = self._prompt_snapshot_for_mode(
            self._prompt_snapshot(prompt_category_id),
            "draft_only",
        )
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
            "comparison_profile",
            "comparison_schema_version",
            "comparison_backends",
            "comparison_variants",
            "comparison_variant_id",
            "comparison_variant_label",
            "comparison_variant_order",
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
            options[TRANSLATION_EXECUTION_MODE_OPTION] = (
                "batch" if len(selected_transcripts) > 1 else "live"
            )
            options["comparison_transcript_source"] = {
                "comparison_id": normalized_comparison_id,
                "job_id": reusable.id,
                "backend": str(reusable.options.get("backend", "")),
            }
            options["pipeline_parent_job_id"] = reusable.id
            options["input_transcript_revision_id"] = (
                reusable.transcript_revision_id
            )
            created = self.store.create(
                job_id=uuid4().hex,
                source_rel=reusable.source_rel,
                force_overwrite=True,
                is_test=True,
                options=options,
                operation="draft_translate",
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
        backend = str(options.get("backend", "hybrid")).strip().lower()
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
                    DEFAULT_CHUNK_LENGTH_SECONDS,
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
        if not transcription.noise_filter:
            raise ValueError(
                f"{backend} backend requires noise_filter=true for VAD"
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
            if backend != "hybrid":
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
            hybrid_rescue = HybridRescueOptions.from_options(options)
            segmentation = asdict(
                WhisperXSegmentationOptions.from_options(
                    options,
                    defaults=(
                        HYBRID_STABLE_SUBTITLE_SEGMENTATION
                        if hybrid_rescue.stable_ts_regroup_enabled
                        else DEFAULT_SUBTITLE_SEGMENTATION
                    ),
                )
            )

            rescue = asdict(hybrid_rescue)
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
            normalized_options["owsm_audit"] = asdict(
                OWSMAuditOptions.from_options(options)
            )
        elif backend == "whisperjav":
            forbidden = {
                "repetition_policy",
                "repetition_min_count",
                "hybrid_rescue",
                "owsm_audit",
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

    def _review_source_generation(
        self,
        job: PipelineJob,
        transcript_payload: Mapping[str, Any],
        segments: Sequence[Mapping[str, Any]],
        prompt_snapshot: Mapping[str, Any],
        *,
        generation_id: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Return one complete, transcript-matched first-pass generation."""
        if not job.transcript_path:
            raise ValueError("2차 보정에 사용할 전사 결과가 없습니다.")
        if generation_id is None:
            self._capture_legacy_translation_generation(
                job,
                transcript_payload,
                segments,
                prompt_snapshot,
            )
            generation = self.store.latest_translation_generation(job.id)
        else:
            generation = self.store.get_translation_generation(generation_id)
        input_generation_id = str(
            job.options.get("input_translation_generation_id", "") or ""
        ).strip()
        parent_job_id = str(
            job.options.get("pipeline_parent_job_id", "") or ""
        ).strip()
        owns_generation = bool(
            generation is not None
            and (
                generation["job_id"] == job.id
                or (
                    input_generation_id
                    and parent_job_id
                    and generation["id"] == input_generation_id
                    and generation["job_id"] == parent_job_id
                )
            )
        )
        if generation is None or not owns_generation:
            raise ValueError("2차 보정에 사용할 1차 번역 결과가 없습니다.")
        if generation["state"] != "completed":
            raise ValueError("완료된 1차 번역 결과만 2차 보정할 수 있습니다.")
        if generation["transcript_hash"] != sha256_file(
            Path(job.transcript_path)
        ):
            raise ValueError(
                "1차 번역과 현재 전사 결과가 달라 2차 보정할 수 없습니다."
            )
        stored_items = self.store.translation_items(generation["id"])
        expected_ids = [str(segment["id"]) for segment in segments]
        try:
            validated = validate_translation_items(
                self._translation_snapshot_items(stored_items),
                expected_ids,
            )
        except ValueError as error:
            raise ValueError(
                "1차 번역 결과가 완전하지 않아 2차 보정할 수 없습니다."
            ) from error
        source_hashes = {
            str(item["id"]): str(item["source_hash"])
            for item in stored_items
        }
        if any(
            source_hashes.get(str(segment["id"]))
            != _canonical_payload_hash(dict(segment))
            for segment in segments
        ):
            raise ValueError(
                "1차 번역 구간과 현재 전사 구간이 달라 2차 보정할 수 "
                "없습니다."
            )
        return generation, {
            str(item["id"]): str(item["text"]) for item in validated
        }

    def _external_comparison_translations(
        self,
        job: PipelineJob,
        segments: Sequence[Mapping[str, Any]],
    ) -> dict[str, str]:
        """Load the transcript-matched first pass for final adjudication."""

        if not job.transcript_path:
            raise ValueError("외부 교정에 사용할 전사 결과가 없습니다.")
        generation_id = str(
            job.options.get("comparison_translation_generation_id", "")
            or ""
        ).strip()
        owner_job_id = str(
            job.options.get("comparison_translation_job_id", "") or ""
        ).strip()
        generation = self.store.get_translation_generation(generation_id)
        if (
            not generation_id
            or not owner_job_id
            or generation is None
            or str(generation["job_id"]) != owner_job_id
        ):
            raise ValueError(
                "외부 교정에서 비교할 1차 번역 결과가 없습니다."
            )
        if generation["state"] != "completed":
            raise ValueError(
                "완료된 1차 번역 결과만 외부 교정에 사용할 수 있습니다."
            )
        if generation["transcript_hash"] != sha256_file(
            Path(job.transcript_path)
        ):
            raise ValueError(
                "1차 번역과 현재 전사 결과가 달라 외부 교정할 수 없습니다."
            )
        stored_items = self.store.translation_items(generation["id"])
        expected_ids = [str(segment["id"]) for segment in segments]
        try:
            validated = validate_translation_items(
                self._translation_snapshot_items(stored_items),
                expected_ids,
            )
        except ValueError as error:
            raise ValueError(
                "1차 번역 결과가 완전하지 않아 외부 교정할 수 없습니다."
            ) from error
        source_hashes = {
            str(item["id"]): str(item["source_hash"])
            for item in stored_items
        }
        if any(
            source_hashes.get(str(segment["id"]))
            != _canonical_payload_hash(dict(segment))
            for segment in segments
        ):
            raise ValueError(
                "1차 번역 구간과 현재 전사 구간이 달라 외부 교정할 수 "
                "없습니다."
            )
        return {
            str(item["id"]): str(item["text"]) for item in validated
        }

    def restart_translation(
        self,
        job_id: str,
        prompt_category_id: str | None = None,
        transcript_revision_id: str | None = None,
        *,
        translation_mode: str | None = None,
        review_source_generation_id: str | None = None,
        execution_mode: str | None = None,
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
        )
        self._capture_legacy_subtitle_generation(
            job,
            translation_generation_id=(
                str(legacy_translation["id"])
                if legacy_translation is not None
                else None
            ),
        )

        if translation_mode is not None and translation_mode not in (
            TRANSLATION_MODES
        ):
            raise ValueError("지원하지 않는 번역 실행 방식입니다.")
        resolved_execution_mode = (
            execution_mode
            if execution_mode is not None
            else str(
                job.options.get(TRANSLATION_EXECUTION_MODE_OPTION, "live")
            ).strip()
        )
        if resolved_execution_mode not in {"live", "batch"}:
            raise ValueError("지원하지 않는 번역 실행 모드입니다.")

        updated_options = dict(job.options)
        if prompt_category_id:
            base_prompt_snapshot = self._prompt_snapshot(prompt_category_id)
        elif not isinstance(
            updated_options.get(TRANSLATION_PROMPT_OPTION),
            Mapping,
        ):
            base_prompt_snapshot = self._legacy_prompt_snapshot()
        else:
            base_prompt_snapshot = dict(
                updated_options[TRANSLATION_PROMPT_OPTION]
            )
        resolved_translation_mode = (
            translation_mode
            if translation_mode is not None
            else self._translation_mode(base_prompt_snapshot)
        )
        source_generation_id = review_source_generation_id
        if resolved_translation_mode == "review_existing":
            source_generation, _source_items = self._review_source_generation(
                selected_job,
                transcript_payload,
                segments,
                current_prompt_snapshot,
                generation_id=source_generation_id,
            )
            source_generation_id = str(source_generation["id"])
        elif source_generation_id is not None:
            raise ValueError(
                "1차 번역 결과 지정은 2차 보정에서만 사용할 수 있습니다."
            )
        self._require_translation_mode_routes(
            resolved_translation_mode,
            resolved_execution_mode,
        )
        updated_prompt_snapshot = self._prompt_snapshot_for_mode(
            base_prompt_snapshot,
            resolved_translation_mode,
            review_source_generation_id=source_generation_id,
        )
        updated_options[TRANSLATION_PROMPT_OPTION] = updated_prompt_snapshot
        updated_options[TRANSLATION_EXECUTION_MODE_OPTION] = (
            resolved_execution_mode
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
        selected_job = replace(selected_job, options=updated_options)
        generation = self._create_translation_generation(
            selected_job,
            transcript_payload,
            updated_prompt_snapshot,
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
                "translation_mode": resolved_translation_mode,
                "review_source_generation_id": source_generation_id,
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
            is_test=original.is_test,
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
        *,
        publish_subtitle: bool = True,
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
        feedback_source_generation: dict[str, Any] | None = None
        feedback_source_items: list[dict[str, Any]] = []
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
            )
            feedback_source_generation = legacy_translation
            if feedback_source_generation is not None:
                feedback_source_items = self.store.translation_items(
                    str(feedback_source_generation["id"])
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
            if feedback_source_generation is not None:
                self._record_translation_feedback_changes(
                    job,
                    prompt_snapshot=prompt_snapshot,
                    segments=segments,
                    source_generation=feedback_source_generation,
                    source_items=feedback_source_items,
                    manual_generation=manual_generation,
                    edited_items=translations,
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
                publish=publish_subtitle,
            )
            self.store.add_event(
                job.id,
                "info",
                (
                    f"{kind} JSON edited; subtitle regenerated"
                    if publish_subtitle
                    else f"{kind} JSON edited; subtitle generation saved unpublished"
                ),
            )
        else:
            self.store.add_event(job.id, "info", f"{kind} JSON edited")
        return artifact

    def _record_translation_feedback_changes(
        self,
        job: PipelineJob,
        *,
        prompt_snapshot: Mapping[str, Any],
        segments: Sequence[Mapping[str, Any]],
        source_generation: Mapping[str, Any],
        source_items: Sequence[Mapping[str, Any]],
        manual_generation: Mapping[str, Any],
        edited_items: Sequence[Mapping[str, Any]],
    ) -> None:
        revision_id = str(
            source_generation.get("prompt_revision_id")
            or prompt_snapshot.get("revision_id")
            or ""
        ).strip()
        revision = self.store.prompt_revision_by_id(revision_id)
        if revision is None:
            return
        stage = (
            "translation"
            if self._translation_mode(prompt_snapshot) == "draft_only"
            else "review"
        )
        source_by_id = {
            str(item["id"]): str(item["text"])
            for item in source_items
        }
        edited_by_id = {
            str(item["id"]): str(item["text"])
            for item in edited_items
        }
        segment_by_id = {
            str(segment["id"]): (index, segment)
            for index, segment in enumerate(segments)
        }
        for segment_id, model_text in source_by_id.items():
            edited_text = edited_by_id.get(segment_id, model_text)
            if edited_text == model_text or segment_id not in segment_by_id:
                continue
            index, segment = segment_by_id[segment_id]
            previous_text = (
                str(segments[index - 1].get("text", ""))
                if index > 0
                else ""
            )
            next_text = (
                str(segments[index + 1].get("text", ""))
                if index + 1 < len(segments)
                else ""
            )
            self.store.record_translation_feedback(
                job_id=job.id,
                category_id=str(revision["category_id"]),
                base_revision_id=revision_id,
                stage=stage,
                source_generation_id=str(source_generation["id"]),
                manual_generation_id=str(manual_generation["id"]),
                segment_id=segment_id,
                source_text=str(segment.get("text", "")),
                model_text=model_text,
                edited_text=edited_text,
                context={
                    "previous_source_text": previous_text,
                    "next_source_text": next_text,
                    "start": segment.get("start"),
                    "end": segment.get("end"),
                    "speaker": segment.get("speaker"),
                },
            )

    def edit_translation_item(
        self,
        job_id: str,
        *,
        generation_id: str,
        segment_id: str,
        text: str,
    ) -> dict[str, Any]:
        """Create an immutable manual generation with one translated cue edited."""

        job = self.store.get(job_id)
        if job is None:
            raise ValueError("job not found")
        generation = self.store.get_translation_generation(generation_id)
        if generation is None or generation["job_id"] != job_id:
            raise ValueError("translation generation not found")
        edited_text = text.strip()
        if not edited_text:
            raise ValueError("자막 문장은 비워 둘 수 없습니다.")
        items = self.store.translation_items(generation_id)
        if not any(str(item["id"]) == segment_id for item in items):
            raise ValueError("translation segment not found")
        translations = [
            {
                "id": str(item["id"]),
                "text": (
                    edited_text
                    if str(item["id"]) == segment_id
                    else str(item["text"])
                ),
            }
            for item in items
        ]
        self.save_artifact(
            job_id,
            "translation",
            json.dumps(
                {
                    "schema_version": TRANSLATION_SCHEMA_VERSION,
                    "status": "completed",
                    "translations": translations,
                },
                ensure_ascii=False,
            ),
            publish_subtitle=False,
        )
        manual_generation = self.store.list_translation_generations(job_id)[-1]
        subtitle_generation = next(
            (
                item
                for item in reversed(self.store.list_subtitle_generations(job_id))
                if item.get("translation_generation_id")
                == manual_generation["id"]
            ),
            None,
        )
        return {
            "generation": manual_generation,
            "subtitle_generation": subtitle_generation,
            "item": {"id": segment_id, "text": edited_text},
        }

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            self._scheduler_tick()
            self._stop_event.wait(1.0)

    def _scheduler_tick(self) -> None:
        self._reconcile_interrupted_jobs()
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
                runtime_id: dict(value)
                for runtime_id, value in self._runtime_health.items()
            }
            slots: list[str] = []
            for definition in self._runtime_definitions():
                runtime_id = str(definition["id"])
                if (
                    not definition["enabled"]
                    or health.get(runtime_id, {}).get("status") != "ready"
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
                readiness = health.get(runtime_id, {}).get("readiness")
                advertised_backends = (
                    readiness.get("backends")
                    if isinstance(readiness, Mapping)
                    else None
                )

                def supports_runtime(job: PipelineJob) -> bool:
                    if not isinstance(advertised_backends, Mapping):
                        # Backward compatibility for runtimes predating
                        # capability advertisement.
                        return True
                    backend = str(job.options.get("backend", "kotoba"))
                    capability = advertised_backends.get(backend)
                    return (
                        isinstance(capability, Mapping)
                        and capability.get("status") == "ready"
                    )

                if not self._dispatch_one(
                    "audio_ready",
                    "transcription_running",
                    "transcription",
                    self._stt_executor,
                    self._transcribe,
                    stt_runtime_id=runtime_id,
                    job_filter=supports_runtime,
                ):
                    continue
                dispatched += 1
            if slots:
                self._runtime_dispatch_cursor = (
                    self._runtime_dispatch_cursor + dispatched
                ) % len(slots)
        return dispatched

    def _dispatch_translations(self) -> int:
        def execution_mode(job: PipelineJob) -> str:
            mode = str(
                job.options.get(TRANSLATION_EXECUTION_MODE_OPTION, "live")
            ).strip()
            return mode if mode in {"live", "batch"} else "live"

        def local_stages(job: PipelineJob) -> tuple[str, ...]:
            if job.operation == "draft_translate":
                return ("draft",)
            if job.operation == "review_translate":
                return ("review",)
            if job.operation == "external_review":
                return ()
            prompt_snapshot = job.options.get(TRANSLATION_PROMPT_OPTION)
            if not isinstance(prompt_snapshot, Mapping):
                prompt_snapshot = self._legacy_prompt_snapshot()
            translation_mode = self._translation_mode(prompt_snapshot)
            if translation_mode == "review_existing":
                return ("review",)
            if translation_mode == "draft_and_review":
                return ("draft", "review")
            return ("draft",)

        def running_jobs() -> list[PipelineJob]:
            return [
                job
                for job_id in self.store.ids_with_status(
                    "translation_running"
                )
                if (job := self.store.get(job_id)) is not None
            ]

        def active_local_routes() -> list[tuple[str, str]]:
            return [
                (stage, execution_mode(job))
                for job in running_jobs()
                for stage in local_stages(job)
            ]

        current_running = running_jobs()
        external_running = any(
            job.operation == "external_review" for job in current_running
        )
        dispatched = 0
        if not external_running and self._dispatch_one(
            "transcribed",
            "translation_running",
            "external review",
            self._translation_executor,
            self._translate,
            job_filter=lambda job: job.operation == "external_review",
        ):
            dispatched += 1

        if self.translation_circuit_state != "ready":
            return dispatched

        def has_route(job: PipelineJob, stages: Sequence[str]) -> bool:
            mode = execution_mode(job)
            return all(
                self._translation_routing.is_configured(stage, mode)
                and not self._translation_routing.route_suspended_by_stt(
                    stage,
                    mode,
                )
                for stage in stages
            )

        def conflicts_with_active_route(
            job: PipelineJob,
            stages: Sequence[str],
        ) -> bool:
            mode = execution_mode(job)
            active_routes = active_local_routes()
            for stage in stages:
                occupied_hosts: set[str] = set()
                for active_stage, active_mode in active_routes:
                    if stage == active_stage:
                        return True
                    occupied_hosts.update(
                        self._translation_routing.configured_hosts(
                            active_stage,
                            active_mode,
                        )
                    )
                candidate_hosts = self._translation_routing.configured_hosts(
                    stage,
                    mode,
                )
                if candidate_hosts and not (
                    set(candidate_hosts) - occupied_hosts
                ):
                    return True
            return False

        waiting_jobs = [
            job
            for job_id in self.store.dispatchable_ids_with_status("transcribed")
            if (job := self.store.get(job_id)) is not None
        ]
        independent_operations = {"draft_translate", "review_translate"}
        for selected_operation, selected_stage in (
            ("draft_translate", "draft"),
            ("review_translate", "review"),
        ):
            if not any(
                job.operation == selected_operation for job in waiting_jobs
            ):
                continue
            if self._dispatch_one(
                "transcribed",
                "translation_running",
                "translation",
                self._translation_executor,
                self._translate,
                job_filter=lambda job: (
                    job.operation == selected_operation
                    and has_route(job, (selected_stage,))
                    and not conflicts_with_active_route(
                        job,
                        (selected_stage,),
                    )
                ),
            ):
                dispatched += 1
        independent_active = any(
            job.operation in independent_operations for job in running_jobs()
        )
        independent_paused = any(
            (job := self.store.get(job_id)) is not None
            and job.operation in independent_operations
            for job_id in self.store.ids_with_status("translation_paused")
        )
        independent_waiting = any(
            job.operation in independent_operations for job in waiting_jobs
        )
        if independent_active or independent_paused or independent_waiting:
            return dispatched

        if self._dispatch_one(
            "transcribed",
            "translation_running",
            "translation",
            self._translation_executor,
            self._translate,
            job_filter=lambda job: (
                job.operation
                not in {
                    "draft_translate",
                    "review_translate",
                    "external_review",
                }
                and has_route(job, local_stages(job))
                and not conflicts_with_active_route(
                    job,
                    local_stages(job),
                )
            ),
        ):
            dispatched += 1
        return dispatched

    def _dispatch_one(
        self,
        waiting: str,
        running: str,
        stage: str,
        executor: ThreadPoolExecutor,
        operation: Callable[[PipelineJob], None],
        *,
        stt_runtime_id: str | None = None,
        job_filter: Callable[[PipelineJob], bool] | None = None,
    ) -> bool:
        waiting_ids = self.store.dispatchable_ids_with_status(waiting)
        for job_id in waiting_ids:
            waiting_job = self.store.get(job_id)
            if waiting_job is None or (
                job_filter is not None and not job_filter(waiting_job)
            ):
                continue
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
        hard_breaker_engaged = False
        try:
            self._raise_if_job_stop_requested(job_id)
            if (
                stage == "transcription"
                and (job.stt_runtime_id or BUILTIN_RUNTIME_ID)
                == BUILTIN_RUNTIME_ID
            ):
                breaker = (
                    self._translation_routing.engage_stt_hard_breaker()
                )
                hard_breaker_engaged = bool(breaker["enabled"])
                if hard_breaker_engaged:
                    self.store.add_event(
                        job_id,
                        "info",
                        "shared-accelerator translation hard breaker engaged",
                        event_code="transcription.hard_breaker.engaged",
                        phase="transcription",
                        payload={
                            "unloaded_models": len(
                                breaker["unloaded_models"]
                            ),
                        },
                    )
            resource_context = nullcontext()
            if stage == "transcription":
                definition = self._runtime_definition(
                    job.stt_runtime_id or BUILTIN_RUNTIME_ID
                )
                resource_context = self._resource_groups.reserve(
                    str(definition["resource_group_id"]),
                    timeout=self.settings.translation_read_timeout_seconds,
                )
            with resource_context:
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
        except TranslationDeferred as error:
            message = self._sanitize_error(str(error))
            if not self._update_stage_job(
                job,
                status="transcribed",
                blocked_stage=None,
                reason_code=None,
                error=None,
            ):
                self._record_lease_fencing_rejection(
                    stage,
                    "hard_breaker_defer",
                )
                return
            self.store.add_event(
                job_id,
                "info",
                f"translation deferred: {message}",
                event_code="translation.hard_breaker.deferred",
                from_state=JobState.RUNNING.value,
                to_state=JobState.WAITING.value,
                phase="translation",
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
            translation_reason = {
                "draft_translate": JobReason.DRAFT_TRANSLATION_UNAVAILABLE,
                "review_translate": JobReason.REVIEW_TRANSLATION_UNAVAILABLE,
                "external_review": (
                    JobReason.EXTERNAL_MODEL_UNAVAILABLE
                ),
            }.get(job.operation, JobReason.LM_UNAVAILABLE)
            reason_code = (
                translation_reason.value
                if stage in {"translation", "external review"}
                else JobReason.STT_UNAVAILABLE.value
            )
            phase_label = {
                "draft_translate": "1차 번역 서버",
                "review_translate": "2차 번역 서버",
                "external_review": "외부 검토 모델",
            }.get(job.operation)
            if phase_label:
                message = f"{phase_label} 연결 불가: {message}"
            if not self._update_stage_job(
                job,
                status="blocked",
                blocked_stage=stage,
                reason_code=reason_code,
                error=message,
            ):
                self._record_lease_fencing_rejection(
                    stage,
                    "terminal_transition",
                )
                return
            if stage == "translation" and job.operation != "external_review":
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
                    "reason_code": reason_code,
                    "operation": job.operation,
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
            if hard_breaker_engaged:
                self._translation_routing.release_stt_hard_breaker()
                try:
                    self.store.add_event(
                        job_id,
                        "info",
                        "shared-accelerator translation hard breaker released",
                        event_code="transcription.hard_breaker.released",
                        phase="transcription",
                    )
                except (OSError, RuntimeError, sqlite3.Error, ValueError):
                    LOGGER.exception(
                        "hard breaker release event write failed for %s",
                        job_id,
                    )
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
            "transcription server unavailable; queued for another server",
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
            "job %s transcription server %s unavailable; requeued: %s",
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
            self.settings.transcription_audio_dir
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
            raise ExternalServiceError("assigned Transcriber is not configured")
        runtime_definition = self._runtime_definition(runtime_id)
        if not job.audio_path or not Path(job.audio_path).is_file():
            raise RuntimeError("extracted WAV is unavailable")
        options = {
            "backend": job.options.get("backend", "hybrid"),
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
            "owsm_audit",
            "whisperjav",
        ):
            if key in job.options:
                options[key] = job.options[key]
        backend = str(options["backend"])
        kotoba_batch_size = runtime_definition["kotoba_batch_size"]
        whisperx_batch_size = runtime_definition["whisperx_batch_size"]
        if backend == "kotoba" and kotoba_batch_size is not None:
            options["batch_size"] = kotoba_batch_size
        elif backend == "whisperx" and whisperx_batch_size is not None:
            options["batch_size"] = whisperx_batch_size
        elif backend == "hybrid":
            if whisperx_batch_size is not None:
                options["batch_size"] = whisperx_batch_size
            if kotoba_batch_size is not None:
                options["kotoba_batch_size"] = kotoba_batch_size
        audio_duration = wav_duration_seconds(Path(job.audio_path))
        source_start = float(job.options["start_seconds"])
        source_end = (
            round(source_start + audio_duration, 3)
            if audio_duration is not None
            else None
        )
        stt_call_count = 3 if options["backend"] == "hybrid" else 2
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
            "batch_size": options.get("batch_size"),
            "kotoba_batch_size": options.get("kotoba_batch_size"),
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
        *,
        request_observer: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> OpenAICompatibleClient:
        return OpenAICompatibleClient(
            "",
            "",
            "",
            max_segments=self.settings.translation_batch_segments,
            max_characters=self.settings.translation_batch_characters,
            request_observer=(
                request_observer or self.record_external_request
            ),
            completion_request=lambda stage, mode, payload: (
                self._translation_routing.request_completion(
                    stage,
                    mode,
                    payload,
                    request_observer=(
                        request_observer or self.record_external_request
                    ),
                )
            ),
        )

    def _translation_generation_contract(
        self,
        job: PipelineJob,
        transcript_payload: Mapping[str, Any],
        prompt_snapshot: Mapping[str, Any],
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
        execution_mode = str(
            job.options.get(TRANSLATION_EXECUTION_MODE_OPTION, "live")
        ).strip()
        if execution_mode not in {"live", "batch"}:
            execution_mode = "live"
        selected_model = (
            model or self._translation_routing.model_contract()
        ).strip()
        selected_endpoint = (
            endpoint_key
            or self._translation_routing.endpoint_contract(execution_mode)
        ).rstrip("/")
        config_hash = _canonical_payload_hash(
            {
                "schema_version": TRANSLATION_SCHEMA_VERSION,
                "transcript_hash": transcript_hash,
                "prompt_hash": prompt_hash,
                "endpoint_key": selected_endpoint,
                "model": selected_model,
                "request_options": (
                    self._translation_routing.request_options_contract()
                    if model is None
                    else "external-provider-defaults"
                ),
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
                str(payload.get("model", "")).strip()
                or self._translation_routing.model_contract()
            )
            generation = self._create_translation_generation(
                job,
                transcript_payload,
                prompt_snapshot,
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
        prompt_snapshot = job.options.get(TRANSLATION_PROMPT_OPTION)
        if not isinstance(prompt_snapshot, Mapping):
            prompt_snapshot = self._legacy_prompt_snapshot()
        translation_mode = self._translation_mode(prompt_snapshot)
        execution_mode = str(
            job.options.get(TRANSLATION_EXECUTION_MODE_OPTION, "live")
        ).strip()
        if execution_mode not in {"live", "batch"}:
            execution_mode = "live"
        external_profile: Mapping[str, Any] | None = None
        external_selection = job.options.get("external_model")
        if job.operation == "external_review":
            if not isinstance(external_selection, Mapping):
                raise ValueError("외부 모델 선택 정보가 없습니다.")
            provider = str(external_selection.get("provider", "")).strip()
            selected_model = str(
                external_selection.get("model", "")
            ).strip()
            external_profile = self.store.get_external_model_profile(
                provider
            )
            if (
                external_profile is None
                or external_profile["status"] != "ready"
                or not external_profile["credential"]
                or selected_model not in external_profile["models"]
            ):
                raise ExternalServiceError(
                    "외부 모델 제공자 인증 또는 선택 모델을 다시 "
                    "점검하세요."
                )
        else:
            self._require_translation_mode_routes(
                translation_mode,
                execution_mode,
                error_type=ExternalServiceError,
            )
        draft_pass = translation_mode != "review_existing"
        review_rounds = (
            0
            if translation_mode == "draft_only"
            else TRANSLATION_REVIEW_ROUNDS
        )
        translation_prompt = str(
            prompt_snapshot.get("translation_prompt", "")
        ).strip() or KOREAN_JAV_DRAFT_PROMPT
        review_prompt = str(
            prompt_snapshot.get("review_prompt", "")
        ).strip() or KOREAN_JAV_REVIEW_PROMPT
        media_duration: float | None = None
        if job.audio_revision_id:
            audio_revision = self.store.get_audio_revision(
                job.audio_revision_id
            )
            if audio_revision is not None:
                media_duration = audio_revision["duration_seconds"]
        duration_bucket = media_duration_bucket_minutes(media_duration)
        pass_lock = threading.Lock()
        pass_intervals: dict[str, list[tuple[float, float]]] = {
            "draft": [],
            "review": [],
        }
        pass_request_seconds = {"draft": 0.0, "review": 0.0}
        pass_request_counts = {"draft": 0, "review": 0}

        def observe_translation_request(
            observation: Mapping[str, Any],
        ) -> None:
            self.record_external_request(observation)
            pass_name = {
                "translation": "draft",
                "review": "review",
            }.get(str(observation.get("operation", "")))
            if pass_name is None:
                return
            elapsed = max(
                0.0,
                float(observation.get("elapsed_seconds", 0.0)),
            )
            finished = time.monotonic()
            with pass_lock:
                pass_intervals[pass_name].append(
                    (finished - elapsed, finished)
                )
                pass_request_seconds[pass_name] += elapsed
                pass_request_counts[pass_name] += 1

        def active_seconds(intervals: Sequence[tuple[float, float]]) -> float:
            if not intervals:
                return 0.0
            ordered = sorted(intervals)
            active = 0.0
            current_start, current_end = ordered[0]
            for started_at, finished_at in ordered[1:]:
                if started_at <= current_end:
                    current_end = max(current_end, finished_at)
                else:
                    active += current_end - current_start
                    current_start, current_end = started_at, finished_at
            return active + current_end - current_start

        def record_translation_pass_metrics(outcome: str) -> None:
            with pass_lock:
                snapshots = {
                    pass_name: (
                        tuple(pass_intervals[pass_name]),
                        pass_request_seconds[pass_name],
                        pass_request_counts[pass_name],
                    )
                    for pass_name in ("draft", "review")
                }
            for pass_name, (
                intervals,
                request_seconds,
                request_count,
            ) in snapshots.items():
                if request_count == 0:
                    continue
                measured_active_seconds = active_seconds(intervals)
                labels: dict[str, str | int | bool] = {
                    "pass": pass_name,
                    "outcome": outcome,
                }
                if duration_bucket is not None:
                    labels["media_duration_bucket_minutes"] = (
                        duration_bucket
                    )
                self._record_measurement(
                    "translation.pass.active_seconds",
                    measured_active_seconds,
                    labels=labels,
                )
                self._record_measurement(
                    "translation.pass.requests",
                    request_count,
                    labels=labels,
                )
                try:
                    self.store.add_event(
                        job.id,
                        "info",
                        f"translation {pass_name} timing measured",
                        event_code="translation.pass.measured",
                        phase="translation",
                        payload={
                            "pass": pass_name,
                            "outcome": outcome,
                            "active_seconds": measured_active_seconds,
                            "request_seconds": request_seconds,
                            "request_count": request_count,
                            "media_duration_seconds": media_duration,
                            "media_duration_bucket_minutes": duration_bucket,
                        },
                    )
                except (OSError, RuntimeError, sqlite3.Error, ValueError):
                    LOGGER.exception(
                        "translation pass timing event write failed"
                    )

        if external_profile is not None:
            lm_client = external_review_client(
                provider=str(external_profile["provider"]),
                base_url=str(external_profile["base_url"]),
                credential=str(external_profile["credential"]),
                model=str(external_selection["model"]),
                region=str(external_profile["region"]),
                max_segments=self.settings.translation_batch_segments,
                max_characters=self.settings.translation_batch_characters,
                request_observer=observe_translation_request,
            )
        else:
            lm_client = self._make_translation_client(
                request_observer=observe_translation_request,
            )
        self.store.add_event(
            job.id,
            "info",
            "translation route selected",
            event_code="translation.route.selected",
            phase="translation",
            payload={
                "execution_mode": execution_mode,
                "translation_mode": translation_mode,
                "draft_pass": draft_pass,
                "local_review_pass": bool(review_rounds),
                "external_validation": (
                    "configured"
                    if self._subtitle_validator.is_complete
                    else "disabled"
                ),
            },
        )
        if not job.transcript_path:
            raise RuntimeError("transcript artifact is unavailable")
        transcript_payload = json.loads(
            Path(job.transcript_path).read_text(encoding="utf-8")
        )
        segments = validate_transcript(transcript_payload)
        draft_translations: dict[str, str] = {}
        comparison_translations: dict[str, str] | None = None
        if not draft_pass:
            review_source_generation_id = str(
                prompt_snapshot.get("review_source_generation_id", "")
            ).strip()
            _source_generation, draft_translations = (
                self._review_source_generation(
                    job,
                    transcript_payload,
                    segments,
                    prompt_snapshot,
                    generation_id=review_source_generation_id or None,
                )
            )
            if job.operation == "external_review":
                comparison_translations = (
                    self._external_comparison_translations(job, segments)
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
        self._require_stage_update(
            job,
            translation_path=str(translation_path),
        )
        generation = self._create_translation_generation(
            job,
            transcript_payload,
            prompt_snapshot,
            origin="automatic",
            model=(
                str(external_selection["model"])
                if external_profile is not None
                else None
            ),
            endpoint_key=(
                f"external:{external_profile['provider']}"
                if external_profile is not None
                else None
            ),
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

        translation_outcome = "failed"
        translation_workers = (
            1
            if external_profile is not None
            else self._translation_routing.worker_limit(
                execution_mode,
                stage="draft" if draft_pass else "review",
            )
        )
        review_priority = (
            self._translation_routing.review_priority(execution_mode)
            if review_rounds and external_profile is None
            else nullcontext()
        )
        try:
            with review_priority:
                translations = lm_client.translate(
                    segments,
                    system_prompt=translation_prompt,
                    review_prompt=review_prompt,
                    review_rounds=review_rounds,
                    draft_pass=draft_pass,
                    draft_translations=draft_translations,
                    comparison_translations=comparison_translations,
                    existing=existing,
                    on_batch=save_batch,
                    on_batch_started=start_batch,
                    on_logical_batch=complete_batch,
                    on_batch_failed=fail_batch,
                    on_progress=update_translation_progress,
                    should_pause=should_pause,
                    max_workers=translation_workers,
                    execution_mode=execution_mode,
                )
            self._raise_if_job_stop_requested(job.id)
            translation_outcome = "completed"
        except TranslationDeferred as error:
            translation_outcome = "deferred"
            self.store.mark_translation_generation(
                generation["id"],
                state="partial",
                error=None,
                generation_attempt=generation_attempt,
            )
            raise
        except TranslationPaused as error:
            translation_outcome = "paused"
            self.store.mark_translation_generation(
                generation["id"],
                state="paused",
                error=str(error),
                generation_attempt=generation_attempt,
            )
            raise
        except ExternalServiceError as error:
            translation_outcome = "blocked"
            self.store.mark_translation_generation(
                generation["id"],
                state="blocked",
                error=self._sanitize_error(str(error)),
                generation_attempt=generation_attempt,
            )
            raise
        except OperationStopped as error:
            translation_outcome = "stopped"
            self.store.mark_translation_generation(
                generation["id"],
                state="stopped",
                error=str(error),
                generation_attempt=generation_attempt,
            )
            raise
        except Exception as error:
            translation_outcome = "failed"
            self.store.mark_translation_generation(
                generation["id"],
                state="failed",
                error=self._sanitize_error(str(error)),
                generation_attempt=generation_attempt,
            )
            raise
        finally:
            record_translation_pass_metrics(translation_outcome)

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
                "translation_mode": translation_mode,
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
        publish = job.operation not in {"draft_translate", "external_review"}
        self._render_artifacts(
            job,
            overwrite=(
                job.force_overwrite
                or bool(job.srt_path)
                or bool(job.ass_path)
            ),
            publish=publish,
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
                "published": publish,
            },
        )

    def _render_artifacts(
        self,
        job: PipelineJob,
        *,
        overwrite: bool,
        publish: bool = True,
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
        if publish and not overwrite:
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
        if publish:
            with self._subtitle_publication_lock:
                self._publish_subtitle_pair_locked(
                    job,
                    generation,
                    srt_path=srt_path,
                    ass_path=ass_path,
                    overwrite=overwrite,
                )
        else:
            lease_owner = (
                self._worker_id
                if job.lease_owner == self._worker_id and job.lease_token > 0
                else None
            )
            self.store.complete_unpublished_subtitle_generation(
                str(generation["id"]),
                lease_owner=lease_owner,
                lease_token=(job.lease_token if lease_owner else None),
            )
            self.store.add_event(
                job.id,
                "info",
                "subtitle generation awaits explicit publication",
                event_code="subtitle.awaiting_publication",
                phase=job.phase,
                payload={
                    "subtitle_generation_id": generation["id"],
                    "operation": job.operation,
                },
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
        external_credentials = {
            str(profile.get("credential", ""))
            for profile in self.store.list_external_model_profiles()
        }
        for secret in {
            self.settings.stt_token,
            self.remote_servers.stt_token,
            self._subtitle_validator.token,
            *self._translation_routing.tokens(),
            *external_credentials,
        }:
            if secret:
                sanitized = sanitized.replace(secret, "[redacted]")
        return sanitized[:2000]

    def sanitize_external_error(self, message: str) -> str:
        return self._sanitize_error(message)
