"""Stage-based NAS orchestration with independent bounded workers."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import threading
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4

from .artifacts import artifact_path
from .audio import AudioExtraction, extract_audio
from .contracts import (
    TRANSLATION_SCHEMA_VERSION,
    validate_transcript,
    validate_translation_items,
)
from .files import sha256_file, write_json_atomic
from .kotoba import DEFAULT_CHUNK_LENGTH_SECONDS, TranscriptionOptions
from .nas_config import MediaLibrary, NASSettings, probe_media_duration
from .nas_store import NASJob, NASStore
from .service_clients import ExternalServiceError, LMStudioClient, STTAPIClient
from .subtitle import write_styled_subtitles_atomic

LOGGER = logging.getLogger(__name__)
MAX_EDITABLE_JSON_BYTES = 20 * 1024 * 1024


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
        self.stt_client = STTAPIClient(
            settings.stt_base_url,
            settings.stt_token,
            poll_interval=settings.stt_poll_interval,
        )
        self.lm_client = LMStudioClient(
            settings.lm_base_url,
            settings.lm_token,
            settings.lm_model,
            max_segments=settings.translation_batch_segments,
            max_characters=settings.translation_batch_characters,
        )
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
            max_workers=1,
            thread_name_prefix="nas-translation",
        )

    def start(self) -> None:
        interrupted = self.store.recover_interrupted()
        if interrupted:
            LOGGER.warning(
                "%d interrupted NAS job(s) now require manual retry",
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
    ) -> NASJob:
        return self.create_jobs(
            [source_rel],
            force_overwrite=force_overwrite,
            options=options,
        )[0]

    def create_jobs(
        self,
        source_rels: Sequence[str],
        *,
        force_overwrite: bool,
        options: Mapping[str, Any],
    ) -> list[NASJob]:
        unique_source_rels = list(dict.fromkeys(source_rels))
        if not unique_source_rels:
            raise ValueError("작업할 미디어 파일을 하나 이상 선택하세요.")
        if len(unique_source_rels) > self.settings.maximum_listed_files:
            raise ValueError("한 번에 등록할 수 있는 파일 수를 초과했습니다.")

        normalized_options = self._normalize_options(options)
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
            if existing_subtitles and not force_overwrite:
                raise FileExistsError(
                    f"{source_rel}: 기존 한국어 자막 파일이 있습니다. "
                    "덮어쓰기를 명시적으로 선택하세요."
                )

        return [
            self.store.create(
                job_id=uuid4().hex,
                source_rel=source_rel,
                force_overwrite=force_overwrite,
                options=normalized_options,
            )
            for source_rel in unique_source_rels
        ]

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
                target_status = "transcribed"
            except (OSError, ValueError, json.JSONDecodeError):
                transcript_segments = None

        if (
            transcript_segments is not None
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
            raise RuntimeError("retried NAS job could not be read")
        return retried

    def restart_translation(self, job_id: str) -> NASJob:
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
        )
        self.store.add_event(
            job.id,
            "info",
            "translation restart requested; transcript preserved and "
            "translation checkpoint reset",
        )
        restarted = self.store.get(job.id)
        if restarted is None:
            raise RuntimeError("restarted NAS job could not be read")
        return restarted

    def save_artifact(
        self,
        job_id: str,
        kind: str,
        content: str,
    ) -> Path:
        job = self.store.get(job_id)
        if job is None:
            raise ValueError("job not found")
        if job.status not in {"completed", "blocked", "failed"}:
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
            if not self.store.ids_with_status("translation_running"):
                self._dispatch_one(
                    "transcribed",
                    "translation_running",
                    "translation",
                    self._translation_executor,
                    self._translate,
                )
            self._stop_event.wait(1.0)

    def _dispatch_one(
        self,
        waiting: str,
        running: str,
        stage: str,
        executor: ThreadPoolExecutor,
        operation: Callable[[NASJob], None],
    ) -> bool:
        waiting_ids = self.store.ids_with_status(waiting)
        if not waiting_ids:
            return False
        job_id = waiting_ids[0]
        self.store.update(
            job_id,
            status=running,
            blocked_stage=None,
            error=None,
        )
        self.store.add_event(job_id, "info", f"{stage} started")
        executor.submit(
            self._run_stage,
            job_id,
            stage,
            operation,
        )
        return True

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
            operation(job)
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
        self.store.update(
            job.id,
            status="audio_ready",
            audio_path=str(audio_path),
            audio_sha256=digest,
        )
        self.store.add_event(
            job.id,
            "info",
            f"audio extraction completed ({audio_path.stat().st_size} bytes)",
        )

    def _transcribe(self, job: NASJob) -> None:
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

        def save_remote_job(remote_job_id: str) -> None:
            self.store.update(job.id, stt_job_id=remote_job_id)
            self.store.add_event(
                job.id,
                "info",
                f"remote transcription job accepted: {remote_job_id}",
            )

        def update_chunk_progress(progress: Mapping[str, Any]) -> None:
            current = self.store.get(job.id)
            previous_created = current.chunks_created if current else 0
            previous_completed = current.chunks_completed if current else 0
            created = int(progress["created"])
            completed = int(progress["completed"])
            in_progress = max(0, created - completed)
            report_every = int(progress.get("report_every", 10))
            final = bool(progress.get("final", False))
            self.store.update(
                job.id,
                chunks_created=created,
                chunks_completed=completed,
                chunk_progress_every=report_every,
            )
            should_log = (
                (previous_created == 0 and created > 0)
                or completed // report_every
                > previous_completed // report_every
                or final
            )
            if should_log:
                self.store.add_event(
                    job.id,
                    "info",
                    "transcription chunks: "
                    f"created {created}, "
                    f"completed {completed}, "
                    f"in progress {in_progress}",
                )

        payload = self.stt_client.transcribe(
            Path(job.audio_path),
            options=options,
            idempotency_key=f"nas-{job.id}",
            existing_job_id=job.stt_job_id,
            on_job_created=save_remote_job,
            on_progress=update_chunk_progress,
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
        validate_transcript(payload)
        transcript_path = artifact_path(
            self.settings.state_dir,
            job.id,
            job.source_rel,
            "transcript",
        )
        write_json_atomic(transcript_path, payload)
        self.store.update(
            job.id,
            status="transcribed",
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

    def _translate(self, job: NASJob) -> None:
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
            self.store.add_event(
                job.id,
                "info",
                f"translation checkpoint saved ({len(items)}/{len(segments)})",
            )

        translations = self.lm_client.translate(
            segments,
            existing=existing,
            on_batch=save_batch,
        )
        write_json_atomic(
            translation_path,
            {
                "schema_version": TRANSLATION_SCHEMA_VERSION,
                "status": "completed",
                "transcript_job_id": transcript_payload["job_id"],
                "model": self.settings.lm_model,
                "translations": translations,
            },
        )
        self.store.update(job.id, status="translated")
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
