"""Stage-based NAS orchestration with independent bounded workers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import threading
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4
import wave

from .artifacts import artifact_path
from .audio import AudioExtraction, extract_audio
from .contracts import (
    TRANSLATION_SCHEMA_VERSION,
    validate_transcript,
    validate_translation_items,
)
from .files import sha256_file, write_json_atomic
from .kotoba import DEFAULT_CHUNK_LENGTH_SECONDS, TranscriptionOptions
from .nas_config import (
    MediaLibrary,
    NASSettings,
    RemoteServerSettings,
    probe_media_duration,
)
from .nas_store import NASJob, NASStore, PromptCategory, SUCCESS_STATUSES
from .service_clients import (
    ExternalServiceError,
    OpenAICompatibleClient,
    OperationStopped,
    STTAPIClient,
    TranslationPaused,
)
from .subtitle import write_styled_subtitles_atomic
from .translation_prompt import (
    KOREAN_JAV_SYSTEM_PROMPT,
    KOREAN_TRANSLATION_REVIEW_PROMPT,
)

LOGGER = logging.getLogger(__name__)
MAX_EDITABLE_JSON_BYTES = 20 * 1024 * 1024
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
USER_STOP_MESSAGE = "사용자 요청으로 전체 작업이 중단되었습니다."
TRANSLATION_PROMPT_OPTION = "translation_prompt"
TRANSLATION_REVIEW_ROUNDS = 2
SUPPORTED_OPERATIONS = {"extract", "transcribe", "translate", "full"}
TRANSLATION_OPERATIONS = {"translate", "full"}


class NASOrchestrator:
    """Advance persisted jobs while keeping each remote resource independent."""

    def __init__(self, settings: NASSettings) -> None:
        settings.validate()
        self.settings = settings
        self.settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.library = MediaLibrary(
            settings.media_root,
            settings.maximum_listed_files,
            duration_probe=probe_media_duration,
        )
        self.store = NASStore(settings.state_dir / "jobs.sqlite3")
        saved_servers = self.store.get_remote_server_settings()
        initial_servers = (
            RemoteServerSettings(**saved_servers)
            if saved_servers is not None
            else settings.remote_servers()
        )
        self._remote_runtime: tuple[
            STTAPIClient | None,
            OpenAICompatibleClient | None,
            RemoteServerSettings,
        ] = (None, None, initial_servers)
        if initial_servers.is_complete:
            try:
                self._set_remote_servers(initial_servers, persist=False)
            except ValueError as error:
                LOGGER.warning("remote server settings are invalid: %s", error)
        self._stop_event = threading.Event()
        self._scheduler = threading.Thread(
            target=self._scheduler_loop,
            name="nas-job-scheduler",
            daemon=True,
        )
        self._audio_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="nas-audio",
        )
        self._stt_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="nas-stt",
        )
        self._translation_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="nas-translation",
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
        return self.stt_client is not None and self.lm_client is not None

    def remote_servers_view(self) -> dict[str, Any]:
        servers = self.remote_servers
        return {
            "stt_base_url": servers.stt_base_url,
            "stt_token_configured": bool(servers.stt_token),
            "lm_base_url": servers.lm_base_url,
            "lm_token_configured": bool(servers.lm_token),
            "lm_model": servers.lm_model,
            "translation_workers": servers.translation_workers,
            "configured": self.remote_servers_configured,
        }

    def active_prompt_categories(self) -> list[PromptCategory]:
        return self.store.list_prompt_categories()

    def all_prompt_categories(self) -> list[PromptCategory]:
        return self.store.list_prompt_categories(include_archived=True)

    def _prompt_snapshot(self, category_id: str) -> dict[str, Any]:
        category = self.store.get_prompt_category(category_id.strip())
        if category is None or category.archived:
            raise ValueError("사용할 수 있는 번역 프롬프트를 선택하세요.")
        return {
            "category_id": category.id,
            "category_name": category.name,
            "translation_prompt": category.translation_prompt,
            "review_prompt": category.review_prompt,
            "review_rounds": TRANSLATION_REVIEW_ROUNDS,
        }

    @staticmethod
    def _legacy_prompt_snapshot() -> dict[str, Any]:
        return {
            "category_id": "jav",
            "category_name": "JAV (기존 작업)",
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
        stt_client = STTAPIClient(
            normalized.stt_base_url,
            normalized.stt_token,
            poll_interval=self.settings.stt_poll_interval,
        )
        lm_client = OpenAICompatibleClient(
            normalized.lm_base_url,
            normalized.lm_token,
            normalized.lm_model,
            max_segments=self.settings.translation_batch_segments,
            max_characters=self.settings.translation_batch_characters,
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
        self._remote_runtime = (stt_client, lm_client, normalized)
        return normalized

    def start(self) -> None:
        interrupted = self.store.recover_interrupted()
        if interrupted:
            LOGGER.warning(
                "%d interrupted job(s) now require manual retry",
                interrupted,
            )
        self._scheduler.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._scheduler.is_alive():
            self._scheduler.join(timeout=5)
        for executor in (
            self._audio_executor,
            self._stt_executor,
            self._translation_executor,
        ):
            executor.shutdown(wait=False, cancel_futures=True)

    def create_job(
        self,
        source_rel: str,
        *,
        force_overwrite: bool,
        options: Mapping[str, Any],
        operation: str = "full",
        prompt_category_id: str | None = None,
    ) -> NASJob:
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
            has_subtitle = any(
                path.exists()
                for path in (
                    source.with_name(f"{source.stem}.ko.srt"),
                    source.with_name(f"{source.stem}.ko.ass"),
                )
            )
            if (
                operation in TRANSLATION_OPERATIONS
                and has_subtitle
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
    ) -> list[NASJob]:
        if operation not in SUPPORTED_OPERATIONS:
            raise ValueError("unsupported job operation")
        if operation != "extract" and not self.remote_servers_configured:
            raise ValueError(
                "먼저 서버 설정에서 전사 서버와 번역 서버를 저장하세요."
            )
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
        reusable_audio_jobs: dict[str, NASJob] = {}
        if operation == "transcribe":
            latest_jobs = self.store.latest_jobs_by_source()
            for source_rel in unique_source_rels:
                reusable_audio = self.store.latest_audio_job(source_rel)
                latest = latest_jobs.get(source_rel)
                if (
                    reusable_audio is not None
                    and latest is not None
                    and latest.id == reusable_audio.id
                ):
                    reusable_audio_jobs[source_rel] = reusable_audio

        jobs: list[NASJob] = []
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
                    stt_job_id=None,
                    transcript_path=None,
                    translation_path=None,
                    srt_path=None,
                    ass_path=None,
                    blocked_stage=None,
                    error=None,
                    chunks_created=0,
                    chunks_completed=0,
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
                )
                transcript_path = artifact_path(
                    self.settings.state_dir,
                    created.id,
                    source_rel,
                    "transcript",
                )
                write_json_atomic(transcript_path, transcript_payload)
                self.store.update(
                    created.id,
                    status="transcribed",
                    audio_path=reusable.audio_path,
                    audio_sha256=reusable.audio_sha256,
                    transcript_path=str(transcript_path),
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

    def _reusable_transcript(
        self,
        source_rel: str,
    ) -> tuple[NASJob, dict[str, Any]]:
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
        normalized_options = {
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
        return normalized_options

    def retry(self, job_id: str) -> NASJob:
        job = self.store.get(job_id)
        if job is None:
            raise ValueError("job not found")
        if job.status not in {"blocked", "failed"}:
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

        retry_fields: dict[str, Any] = {
            "status": target_status,
            "blocked_stage": None,
            "error": None,
            "translation_pause_requested": 0,
            "job_stop_requested": 0,
        }
        if target_status in {"queued", "audio_ready"}:
            retry_fields.update(
                {
                    "chunks_created": 0,
                    "chunks_completed": 0,
                }
            )
        self.store.update(job.id, **retry_fields)
        self.store.add_event(
            job.id,
            "info",
            f"manual retry requested; resuming from {target_status}",
        )
        retried = self.store.get(job.id)
        if retried is None:
            raise RuntimeError("retried job could not be read")
        return retried

    def delete_job_record(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job is None:
            raise ValueError("job not found")
        if not job.can_delete_record:
            raise ValueError(
                "only completed audio extraction records or jobs missing "
                "from the remote transcription server can be deleted"
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
    ) -> NASJob:
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
        if not transcript_path.is_file():
            raise ValueError("transcript artifact is unavailable")
        try:
            transcript_payload = json.loads(
                transcript_path.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError("transcript artifact could not be read") from error
        if not isinstance(transcript_payload, Mapping):
            raise ValueError("transcript JSON document must be an object")
        validate_transcript(transcript_payload)
        transcript_job_id = str(
            transcript_payload.get("job_id", "")
        ).strip()
        if not transcript_job_id:
            raise ValueError("transcript job_id is unavailable")

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
                self.settings.state_dir,
                job.id,
                job.source_rel,
                "translation",
            )
        )
        write_json_atomic(
            translation_path,
            {
                "schema_version": TRANSLATION_SCHEMA_VERSION,
                "status": "partial",
                "transcript_job_id": transcript_job_id,
                "translations": [],
            },
        )
        self.store.update(
            job.id,
            status="transcribed",
            translation_path=str(translation_path),
            blocked_stage=None,
            error=None,
            translation_chunks_total=0,
            translation_chunks_completed=0,
            translation_pause_requested=0,
            options_json=json.dumps(updated_options, sort_keys=True),
        )
        self.store.add_event(
            job.id,
            "info",
            "translation restart requested; transcript preserved and "
            "translation checkpoint reset",
        )
        restarted = self.store.get(job.id)
        if restarted is None:
            raise RuntimeError("restarted job could not be read")
        return restarted

    def reprocess(
        self,
        job_id: str,
        operation: str,
        prompt_category_id: str | None = None,
    ) -> NASJob:
        original = self.store.get(job_id)
        if original is None:
            raise ValueError("job not found")
        if original.status not in SUCCESS_STATUSES:
            raise ValueError("only successful jobs can be reprocessed")
        if operation not in SUPPORTED_OPERATIONS:
            raise ValueError("unsupported job operation")
        if operation != "extract" and not self.remote_servers_configured:
            raise ValueError(
                "먼저 서버 설정에서 전사 서버와 번역 서버를 저장하세요."
            )
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

    def pause_translation(self, job_id: str) -> NASJob:
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
        self.store.add_event(job.id, "info", "translation pause requested")
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
                    )
                    paused_count += 1
                    break
        return paused_count

    def stop_all_jobs(self) -> int:
        stopped_count = 0
        for listed in self.store.list_open_jobs():
            for _attempt in range(3):
                job = self.store.get(listed.id)
                if job is None or not job.can_stop:
                    break
                if job.status in RUNNING_STATUSES:
                    fields: dict[str, Any] = {"job_stop_requested": 1}
                    event_message = (
                        "bulk job stop requested; waiting for a safe stop point"
                    )
                else:
                    fields = {
                        "status": "blocked",
                        "blocked_stage": WAITING_STAGE_BY_STATUS[job.status],
                        "error": USER_STOP_MESSAGE,
                        "translation_pause_requested": 0,
                        "job_stop_requested": 0,
                    }
                    event_message = "job stopped by bulk user request"
                if self.store.update_if_status(
                    job.id,
                    {job.status},
                    **fields,
                ):
                    self.store.add_event(job.id, "warning", event_message)
                    stopped_count += 1
                    break
        return stopped_count

    def resume_translation(self, job_id: str) -> NASJob:
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
        self.store.add_event(job.id, "info", "translation resume requested")
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

        artifact = Path(selected_path)
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
            audio_busy = bool(
                self.store.ids_with_status("extracting")
                or self.store.ids_with_status("rendering")
            )
            if not audio_busy:
                if self.store.ids_with_status("translated"):
                    self._dispatch_one(
                        "translated",
                        "rendering",
                        "render",
                        self._audio_executor,
                        self._render,
                    )
                else:
                    self._dispatch_one(
                        "queued",
                        "extracting",
                        "audio extraction",
                        self._audio_executor,
                        self._extract,
                    )
            if not self.store.ids_with_status("transcription_running"):
                self._dispatch_one(
                    "audio_ready",
                    "transcription_running",
                    "transcription",
                    self._stt_executor,
                    self._transcribe,
                )
            self._dispatch_translations()
            self._stop_event.wait(1.0)

    def _dispatch_translations(self) -> int:
        slots = max(
            0,
            self.remote_servers.translation_workers
            - len(self.store.ids_with_status("translation_running")),
        )
        dispatched = 0
        for _ in range(slots):
            if not self._dispatch_one(
                "transcribed",
                "translation_running",
                "translation",
                self._translation_executor,
                self._translate,
            ):
                break
            dispatched += 1
        return dispatched

    def _dispatch_one(
        self,
        waiting: str,
        running: str,
        stage: str,
        executor: ThreadPoolExecutor,
        operation: Callable[[NASJob], None],
    ) -> bool:
        waiting_ids = self.store.dispatchable_ids_with_status(waiting)
        for job_id in waiting_ids:
            if not self.store.claim_for_dispatch(job_id, waiting, running):
                continue
            self.store.add_event(job_id, "info", f"{stage} started")
            executor.submit(
                self._run_stage,
                job_id,
                stage,
                operation,
            )
            return True
        return False

    def _run_stage(
        self,
        job_id: str,
        stage: str,
        operation: Callable[[NASJob], None],
    ) -> None:
        job = self.store.get(job_id)
        if job is None:
            return
        try:
            self._raise_if_job_stop_requested(job_id)
            operation(job)
            self._raise_if_job_stop_requested(job_id)
        except OperationStopped:
            self._mark_job_stopped(job_id, stage)
        except ExternalServiceError as error:
            message = self._sanitize_error(str(error))
            self.store.update(
                job_id,
                status="blocked",
                blocked_stage=stage,
                error=message,
            )
            self.store.add_event(job_id, "warning", f"{stage} blocked: {message}")
            LOGGER.warning("job %s %s blocked: %s", job_id, stage, message)
        except TranslationPaused:
            current = self.store.get(job_id)
            if current is not None and current.job_stop_requested:
                self._mark_job_stopped(job_id, stage)
            else:
                self.store.update(
                    job_id,
                    status="translation_paused",
                    blocked_stage=None,
                    error=None,
                    translation_pause_requested=1,
                )
                self.store.add_event(job_id, "info", "translation paused")
        except BaseException as error:
            message = self._sanitize_error(str(error) or error.__class__.__name__)
            self.store.update(
                job_id,
                status="failed",
                blocked_stage=stage,
                error=message,
            )
            self.store.add_event(job_id, "error", f"{stage} failed: {message}")
            LOGGER.exception("job %s %s failed", job_id, stage)

    def _raise_if_job_stop_requested(self, job_id: str) -> None:
        current = self.store.get(job_id)
        if current is not None and current.job_stop_requested:
            raise OperationStopped("job stop requested")

    def _mark_job_stopped(self, job_id: str, stage: str) -> None:
        self.store.update(
            job_id,
            status="blocked",
            blocked_stage=stage,
            error=USER_STOP_MESSAGE,
            translation_pause_requested=0,
            job_stop_requested=0,
        )
        self.store.add_event(job_id, "warning", "job stopped by user request")

    def _extract(self, job: NASJob) -> None:
        source = self.library.resolve_file(job.source_rel)
        artifact_dir = self.settings.state_dir / "jobs" / job.id
        audio_path = artifact_dir / "audio.16k.wav"
        options = AudioExtraction(
            audio_stream=int(job.options["audio_stream"]),
            start_seconds=float(job.options["start_seconds"]),
            duration_seconds=job.options["duration_seconds"],
        )
        extract_audio(source, audio_path, options)
        digest = sha256_file(audio_path)
        next_status = (
            "audio_completed" if job.operation == "extract" else "audio_ready"
        )
        self.store.update(
            job.id,
            status=next_status,
            audio_path=str(audio_path),
            audio_sha256=digest,
        )
        self.store.add_event(
            job.id,
            "info",
            f"audio extraction completed ({audio_path.stat().st_size} bytes)",
        )

    def _transcribe(self, job: NASJob) -> None:
        stt_client = self.stt_client
        if stt_client is None:
            raise ExternalServiceError("transcription server is not configured")
        if not job.audio_path or not Path(job.audio_path).is_file():
            raise RuntimeError("extracted WAV is unavailable")
        options = {
            "chunk_length_seconds": job.options["chunk_length_seconds"],
            "num_speakers": job.options["num_speakers"],
            "min_speakers": job.options["min_speakers"],
            "max_speakers": job.options["max_speakers"],
            "add_punctuation": job.options["add_punctuation"],
            "noise_filter": job.options.get("noise_filter", True),
        }
        try:
            with wave.open(job.audio_path, "rb") as wav_file:
                audio_duration: float | None = round(
                    wav_file.getnframes() / wav_file.getframerate(),
                    3,
                )
        except (EOFError, wave.Error, ZeroDivisionError):
            audio_duration = None
        source_start = float(job.options["start_seconds"])
        source_end = (
            round(source_start + audio_duration, 3)
            if audio_duration is not None
            else None
        )
        request_metadata = {
            "job_id": job.id,
            "request_id": f"nas-{job.id}",
            "delivery_mode": "single_wav",
            "audio_sha256": job.audio_sha256,
            "audio_duration_sec": audio_duration,
            "source_start_sec": source_start,
            "source_end_sec": source_end,
            "provider": "remote_stt",
            "chunk_length_seconds": options["chunk_length_seconds"],
            "chunk_length_semantics": "model_internal",
            "stt_call_count": 1,
        }
        LOGGER.info(
            "stt_request job_id=%s request_id=%s delivery_mode=single_wav "
            "audio_sha256=%s duration_sec=%s source_start_sec=%.3f "
            "source_end_sec=%s chunk_length_seconds=%s call_count=1",
            job.id,
            request_metadata["request_id"],
            job.audio_sha256,
            audio_duration if audio_duration is not None else "unknown",
            source_start,
            source_end if source_end is not None else "unknown",
            options["chunk_length_seconds"],
            extra=request_metadata,
        )

        def save_remote_job(remote_job_id: str) -> None:
            self.store.update(job.id, stt_job_id=remote_job_id)
            self.store.add_event(
                job.id,
                "info",
                f"remote transcription job accepted: {remote_job_id}",
            )

        def update_chunk_progress(progress: Mapping[str, Any]) -> None:
            created = int(progress["created"])
            completed = int(progress["completed"])
            report_every = int(progress.get("report_every", 10))
            self.store.update(
                job.id,
                chunks_created=created,
                chunks_completed=completed,
                chunk_progress_every=report_every,
            )

        payload = stt_client.transcribe(
            Path(job.audio_path),
            options=options,
            idempotency_key=f"nas-{job.id}",
            existing_job_id=job.stt_job_id,
            on_job_created=save_remote_job,
            on_progress=update_chunk_progress,
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
        transcript_path = artifact_path(
            self.settings.state_dir,
            job.id,
            job.source_rel,
            "transcript",
        )
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
        self.store.update(
            job.id,
            status=next_status,
            transcript_path=str(transcript_path),
        )
        self.store.add_event(
            job.id,
            "info",
            f"transcription completed ({len(payload['segments'])} segments)",
        )
        noise_filter = payload.get("noise_filter")
        if isinstance(noise_filter, Mapping):
            removed_count = int(noise_filter.get("removed_count", 0))
            if removed_count > 0:
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
        )

    def _translate(self, job: NASJob) -> None:
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
                self.settings.state_dir,
                job.id,
                job.source_rel,
                "translation",
            )
        )
        self.store.update(job.id, translation_path=str(translation_path))

        existing: dict[str, str] = {}
        expected_ids = {str(segment["id"]) for segment in segments}
        ignored_checkpoint_ids = 0
        if translation_path.is_file():
            try:
                partial = json.loads(translation_path.read_text(encoding="utf-8"))
                if not isinstance(partial, Mapping):
                    raise ValueError(
                        "translation checkpoint must be an object"
                    )
                for item in partial.get("translations", []):
                    segment_id = str(item["id"])
                    text = str(item["text"]).strip()
                    if segment_id in expected_ids and text:
                        existing[segment_id] = text
                    elif segment_id:
                        ignored_checkpoint_ids += 1
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                existing = {}
        if ignored_checkpoint_ids:
            self.store.add_event(
                job.id,
                "warning",
                "ignored "
                f"{ignored_checkpoint_ids} stale translation checkpoint id(s)",
            )

        def save_batch(items: list[dict[str, str]]) -> None:
            write_json_atomic(
                translation_path,
                {
                    "schema_version": TRANSLATION_SCHEMA_VERSION,
                    "status": "partial",
                    "transcript_job_id": transcript_payload["job_id"],
                    "translations": items,
                },
            )
        progress_base = job.translation_chunks_completed

        def update_translation_progress(completed: int, total: int) -> None:
            self.store.update(
                job.id,
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

        translations = lm_client.translate(
            segments,
            system_prompt=translation_prompt,
            review_prompt=review_prompt,
            review_rounds=review_rounds,
            existing=existing,
            on_batch=save_batch,
            on_progress=update_translation_progress,
            should_pause=should_pause,
            on_review_warning=review_warning,
        )
        self._raise_if_job_stop_requested(job.id)
        write_json_atomic(
            translation_path,
            {
                "schema_version": TRANSLATION_SCHEMA_VERSION,
                "status": "completed",
                "transcript_job_id": transcript_payload["job_id"],
                "model": servers.lm_model,
                "translations": translations,
            },
        )
        refreshed = self.store.get(job.id)
        self.store.update(
            job.id,
            status="translated",
            translation_pause_requested=0,
            translation_chunks_completed=(
                refreshed.translation_chunks_total if refreshed else 0
            ),
        )
        self.store.add_event(
            job.id,
            "info",
            f"translation completed ({len(translations)} segments)",
        )

    def _render(self, job: NASJob) -> None:
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
        )

    def _render_artifacts(
        self,
        job: NASJob,
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
        timeline = write_styled_subtitles_atomic(
            srt_path,
            ass_path,
            segments,
            translations,
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
        self.store.update(
            job.id,
            status="completed",
            srt_path=str(srt_path),
            ass_path=str(ass_path),
            blocked_stage=None,
            error=None,
        )

    def _sanitize_error(self, message: str) -> str:
        sanitized = message
        for secret in (self.settings.stt_token, self.settings.lm_token):
            if secret:
                sanitized = sanitized.replace(secret, "[redacted]")
        return sanitized[:2000]
