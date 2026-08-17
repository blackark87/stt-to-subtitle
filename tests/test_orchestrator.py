import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from stt_to_subtitle.web_config import WebSettings, RemoteServerSettings
from stt_to_subtitle.orchestrator import (
    SubtitleOrchestrator,
    estimate_transcription_chunks,
)
from stt_to_subtitle.service_clients import TranslationPaused


class SubtitleOrchestratorTests(unittest.TestCase):
    def test_estimates_transcription_chunks_from_audio_duration(self) -> None:
        self.assertEqual(
            estimate_transcription_chunks(
                121.0,
                {"backend": "kotoba", "chunk_length_seconds": 60},
            ),
            3,
        )
        self.assertEqual(
            estimate_transcription_chunks(
                121.0,
                {
                    "backend": "hybrid",
                    "chunk_length_seconds": 60,
                    "hybrid_rescue": {"kotoba_chunk_length_seconds": 15},
                },
            ),
            9,
        )

    def make_orchestrator(
        self,
        root: Path,
        media_root: Path,
    ) -> SubtitleOrchestrator:
        return SubtitleOrchestrator(
            WebSettings(
                state_dir=root / "state",
                media_root=media_root,
                admin_password="admin-password",
                session_secret="a" * 32,
                stt_base_url="http://stt.test",
                stt_token="stt-token",
                lm_base_url="http://lm.test/v1",
                lm_token="lm-token",
                lm_model="model",
            )
        )

    def test_zero_duration_means_process_to_end(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"not-read-in-this-test")
            orchestrator = SubtitleOrchestrator(
                WebSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    admin_password="admin-password",
                    session_secret="a" * 32,
                    stt_base_url="http://stt.test",
                    stt_token="stt-token",
                    lm_base_url="http://lm.test/v1",
                    lm_token="lm-token",
                    lm_model="model",
                )
            )
            try:
                job = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={"duration_seconds": "0"},
                )
            finally:
                orchestrator.stop()

            self.assertIsNone(job.options["duration_seconds"])
            self.assertEqual(job.options["backend"], "kotoba")
            self.assertEqual(job.options["chunk_length_seconds"], 60)
            self.assertTrue(job.options["noise_filter"])

    def test_rejects_invalid_backend_options_before_audio_work(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                with self.assertRaisesRegex(
                    ValueError, "requires noise_filter=true"
                ):
                    orchestrator.create_job(
                        "movie.mkv",
                        force_overwrite=False,
                        options={
                            "backend": "hybrid",
                            "noise_filter": False,
                        },
                    )
                with self.assertRaisesRegex(
                    ValueError, "hybrid_rescue requires"
                ):
                    orchestrator.create_job(
                        "movie.mkv",
                        force_overwrite=False,
                        options={
                            "backend": "whisperx",
                            "hybrid_rescue": {},
                        },
                    )
            finally:
                orchestrator.stop()

    def test_updates_and_reloads_remote_servers_without_restart(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            original = self.make_orchestrator(root, media_root)
            try:
                original_stt_client = original.stt_client
                saved = original.update_remote_servers(
                    RemoteServerSettings(
                        stt_base_url="http://new-stt.test/",
                        stt_token="new-stt-token",
                        lm_base_url="http://new-lm.test/v1/",
                        lm_token="new-lm-token",
                        lm_model="new-model",
                    )
                )

                self.assertIsNot(original.stt_client, original_stt_client)
                self.assertEqual(
                    original.stt_client.base_url,
                    "http://new-stt.test",
                )
                self.assertEqual(
                    original.lm_client.base_url,
                    "http://new-lm.test/v1",
                )
                self.assertEqual(original.lm_client.model, "new-model")
                self.assertEqual(saved.lm_model, "new-model")
            finally:
                original.stop()

            reloaded = self.make_orchestrator(root, media_root)
            try:
                self.assertEqual(
                    reloaded.stt_client.base_url,
                    "http://new-stt.test",
                )
                self.assertEqual(reloaded.stt_client.token, "new-stt-token")
                self.assertEqual(reloaded.lm_client.model, "new-model")
            finally:
                reloaded.stop()

    def test_translation_worker_limit_controls_file_dispatch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            for index in range(3):
                (media_root / f"movie-{index}.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                orchestrator.update_remote_servers(
                    RemoteServerSettings(
                        stt_base_url="http://stt.test",
                        stt_token="",
                        lm_base_url="http://lm.test/v1",
                        lm_token="",
                        lm_model="model",
                        translation_workers=2,
                    )
                )
                jobs = orchestrator.create_jobs(
                    [f"movie-{index}.mkv" for index in range(3)],
                    force_overwrite=False,
                    options={},
                )
                for job in jobs:
                    orchestrator.store.update(job.id, status="transcribed")
                orchestrator._translation_executor.submit = Mock()

                dispatched = orchestrator._dispatch_translations()
                running = orchestrator.store.ids_with_status(
                    "translation_running"
                )
                waiting = orchestrator.store.ids_with_status("transcribed")
                orchestrator.update_remote_servers(
                    RemoteServerSettings(
                        stt_base_url="http://stt.test",
                        stt_token="",
                        lm_base_url="http://lm.test/v1",
                        lm_token="",
                        lm_model="model",
                        translation_workers=1,
                    )
                )
                after_decrease = orchestrator._dispatch_translations()
            finally:
                orchestrator.stop()

            self.assertEqual(dispatched, 2)
            self.assertEqual(len(running), 2)
            self.assertEqual(len(waiting), 1)
            self.assertEqual(after_decrease, 0)

    def test_requires_web_server_settings_before_creating_job(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = SubtitleOrchestrator(
                WebSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    admin_password="",
                    session_secret="",
                    stt_base_url="",
                    stt_token="",
                    lm_base_url="",
                    lm_token="",
                    lm_model="",
                )
            )
            try:
                with self.assertRaisesRegex(ValueError, "서버 설정"):
                    orchestrator.create_job(
                        "movie.mkv",
                        force_overwrite=False,
                        options={},
                    )
            finally:
                orchestrator.stop()

    def test_transcribe_job_stops_after_audio_extraction_and_transcription(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={},
                    operation="transcribe",
                )

                def fake_extract(_source, target, _options):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"wave")

                with patch(
                    "stt_to_subtitle.orchestrator.extract_audio",
                    side_effect=fake_extract,
                ), patch(
                    "stt_to_subtitle.orchestrator.wav_duration_seconds",
                    return_value=121.0,
                ):
                    orchestrator._extract(job)
                extracted = orchestrator.store.get(job.id)
                orchestrator.stt_client.transcribe = Mock(
                    return_value={
                        "schema_version": 1,
                        "job_id": "remote-job",
                        "noise_filter": {
                            "enabled": True,
                            "removed_count": None,
                        },
                        "segments": [
                            {
                                "id": "segment-000001",
                                "start": 0,
                                "end": 1,
                                "speaker": "SPEAKER_00",
                                "text": "こんにちは",
                            }
                        ],
                    }
                )
                orchestrator._transcribe(extracted)
                completed = orchestrator.store.get(job.id)
            finally:
                orchestrator.stop()

            self.assertEqual(extracted.status, "audio_ready")
            self.assertEqual(extracted.chunks_total_estimate, 3)
            self.assertEqual(completed.status, "transcription_completed")
            self.assertEqual(completed.operation, "transcribe")
            self.assertTrue(Path(completed.audio_path).is_file())
            self.assertTrue(Path(completed.transcript_path).is_file())
            self.assertIsNone(completed.translation_path)
            self.assertFalse(completed.can_pause_translation)

    def test_transcription_reuses_persisted_legacy_audio_job_in_place(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            initial = self.make_orchestrator(root, media_root)
            try:
                legacy = initial.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={"start_seconds": "12"},
                    operation="extract",
                )
                audio_path = (
                    root / "state" / "jobs" / legacy.id / "audio.16k.wav"
                )
                audio_path.parent.mkdir(parents=True)
                audio_path.write_bytes(b"wave")
                initial.store.update(
                    legacy.id,
                    status="audio_completed",
                    audio_path=str(audio_path),
                    audio_sha256="digest",
                )
            finally:
                initial.stop()

            reloaded = self.make_orchestrator(root, media_root)
            try:
                resumed = reloaded.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={
                        "start_seconds": "99",
                        "chunk_length_seconds": "30",
                    },
                    operation="transcribe",
                )
                jobs = reloaded.store.list_jobs(limit=None)
            finally:
                reloaded.stop()

            self.assertEqual(resumed.id, legacy.id)
            self.assertEqual(resumed.operation, "transcribe")
            self.assertEqual(resumed.status, "audio_ready")
            self.assertEqual(resumed.audio_path, str(audio_path))
            self.assertEqual(resumed.audio_sha256, "digest")
            self.assertEqual(resumed.options["start_seconds"], 12.0)
            self.assertEqual(resumed.options["chunk_length_seconds"], 30)
            self.assertEqual([job.id for job in jobs], [legacy.id])

    def test_transcription_reextracts_missing_legacy_audio_with_same_job_id(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                legacy = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={},
                    operation="extract",
                )
                orchestrator.store.update(
                    legacy.id,
                    status="audio_completed",
                    audio_path=str(root / "missing.wav"),
                    audio_sha256="stale-digest",
                )

                resumed = orchestrator.reprocess(
                    legacy.id,
                    "transcribe",
                )
                jobs = orchestrator.store.list_jobs(limit=None)
            finally:
                orchestrator.stop()

            self.assertEqual(resumed.id, legacy.id)
            self.assertEqual(resumed.status, "queued")
            self.assertEqual(resumed.operation, "transcribe")
            self.assertIsNone(resumed.audio_path)
            self.assertIsNone(resumed.audio_sha256)
            self.assertEqual([job.id for job in jobs], [legacy.id])

    def test_translation_reuses_validated_transcript_without_stt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            (media_root / "missing.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                transcribed = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={"start_seconds": "12"},
                    operation="transcribe",
                )
                transcript_path = (
                    root / "state" / "jobs" / transcribed.id / "transcript.json"
                )
                transcript_path.parent.mkdir(parents=True)
                transcript_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "job_id": "remote-job",
                            "segments": [
                                {
                                    "id": "segment-000001",
                                    "start": 0,
                                    "end": 1,
                                    "speaker": "SPEAKER_00",
                                    "text": "こんにちは",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                orchestrator.store.update(
                    transcribed.id,
                    status="transcription_completed",
                    transcript_path=str(transcript_path),
                )

                reused = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=True,
                    options={"chunk_length_seconds": "30"},
                    operation="translate",
                    prompt_category_id="variety",
                )
                with self.assertRaisesRegex(ValueError, "먼저 전사를"):
                    orchestrator.create_job(
                        "missing.mkv",
                        force_overwrite=True,
                        options={},
                        operation="translate",
                        prompt_category_id="jav",
                    )
                original = orchestrator.store.get(transcribed.id)
            finally:
                orchestrator.stop()

            self.assertNotEqual(reused.id, transcribed.id)
            self.assertEqual(reused.status, "transcribed")
            self.assertEqual(reused.operation, "translate")
            self.assertEqual(reused.options["start_seconds"], 12.0)
            self.assertEqual(
                reused.options["translation_prompt"]["category_id"],
                "variety",
            )
            self.assertNotEqual(reused.transcript_path, str(transcript_path))
            self.assertEqual(
                json.loads(
                    Path(reused.transcript_path).read_text(encoding="utf-8")
                ),
                json.loads(transcript_path.read_text(encoding="utf-8")),
            )
            self.assertEqual(original.status, "transcription_completed")

    def test_pauses_waiting_translation_and_resumes_checkpoint(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    job.id,
                    status="transcribed",
                    translation_chunks_total=4,
                    translation_chunks_completed=2,
                )
                paused = orchestrator.pause_translation(job.id)
                resumed = orchestrator.resume_translation(job.id)
            finally:
                orchestrator.stop()

            self.assertEqual(paused.status, "translation_paused")
            self.assertTrue(paused.translation_pause_requested)
            self.assertEqual(resumed.status, "transcribed")
            self.assertFalse(resumed.translation_pause_requested)
            self.assertEqual(resumed.translation_chunks_completed, 2)

    def test_running_translation_pauses_after_persisted_logical_chunk(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                artifact_dir = root / "state" / "jobs" / job.id
                artifact_dir.mkdir(parents=True)
                transcript_path = artifact_dir / "transcript.json"
                transcript_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "job_id": "remote-job",
                            "segments": [
                                {
                                    "id": "segment-000001",
                                    "start": 0,
                                    "end": 1,
                                    "speaker": "SPEAKER_00",
                                    "text": "こんにちは",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                orchestrator.store.update(
                    job.id,
                    status="translation_running",
                    transcript_path=str(transcript_path),
                    translation_pause_requested=1,
                )
                translation_client = Mock()

                def pause_after_checkpoint(_segments, **kwargs):
                    items = [{"id": "segment-000001", "text": "안녕하세요"}]
                    kwargs["on_progress"](0, 2)
                    kwargs["on_batch"](items)
                    kwargs["on_progress"](1, 2)
                    raise TranslationPaused()

                translation_client.translate = Mock(
                    side_effect=pause_after_checkpoint
                )
                orchestrator._make_translation_client = Mock(
                    return_value=translation_client
                )

                orchestrator._run_stage(
                    job.id,
                    "translation",
                    orchestrator._translate,
                )
                paused = orchestrator.store.get(job.id)
                checkpoint = json.loads(
                    Path(paused.translation_path).read_text(encoding="utf-8")
                )
            finally:
                orchestrator.stop()

            self.assertEqual(paused.status, "translation_paused")
            self.assertEqual(paused.translation_chunks_completed, 1)
            self.assertEqual(paused.translation_chunks_total, 2)
            self.assertEqual(
                checkpoint["translations"],
                [{"id": "segment-000001", "text": "안녕하세요"}],
            )

    def test_pauses_all_current_and_future_translation_stages(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                queued = orchestrator.store.create(
                    job_id="queued",
                    source_rel="queued.mkv",
                    force_overwrite=False,
                    options={},
                )
                waiting = orchestrator.store.create(
                    job_id="waiting",
                    source_rel="waiting.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(waiting.id, status="transcribed")
                running = orchestrator.store.create(
                    job_id="running",
                    source_rel="running.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    running.id,
                    status="translation_running",
                )
                extraction = orchestrator.store.create(
                    job_id="extract",
                    source_rel="extract.mkv",
                    force_overwrite=False,
                    options={},
                    operation="extract",
                )

                paused_count = orchestrator.pause_all_translations()

                queued = orchestrator.store.get(queued.id)
                waiting = orchestrator.store.get(waiting.id)
                running = orchestrator.store.get(running.id)
                extraction = orchestrator.store.get(extraction.id)
            finally:
                orchestrator.stop()

            self.assertEqual(paused_count, 3)
            self.assertEqual(queued.status, "queued")
            self.assertTrue(queued.translation_pause_requested)
            self.assertEqual(waiting.status, "translation_paused")
            self.assertTrue(waiting.translation_pause_requested)
            self.assertEqual(running.status, "translation_running")
            self.assertTrue(running.translation_pause_requested)
            self.assertFalse(extraction.translation_pause_requested)

    def test_bulk_pause_stops_after_transcription_before_translation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                audio_path = root / "state" / "jobs" / job.id / "audio.wav"
                audio_path.parent.mkdir(parents=True)
                audio_path.write_bytes(b"audio")
                orchestrator.store.update(
                    job.id,
                    status="transcription_running",
                    audio_path=str(audio_path),
                    translation_pause_requested=1,
                )
                orchestrator.stt_client.transcribe = Mock(
                    return_value={
                        "schema_version": 1,
                        "job_id": "remote-job",
                        "segments": [],
                    }
                )

                orchestrator._transcribe(orchestrator.store.get(job.id))
                paused = orchestrator.store.get(job.id)
            finally:
                orchestrator.stop()

            self.assertEqual(paused.status, "translation_paused")
            self.assertTrue(paused.translation_pause_requested)
            self.assertIsNotNone(paused.transcript_path)

    def test_stops_all_waiting_and_running_jobs_at_safe_points(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                waiting = orchestrator.store.create(
                    job_id="waiting",
                    source_rel="waiting.mkv",
                    force_overwrite=False,
                    options={},
                )
                running = orchestrator.store.create(
                    job_id="running",
                    source_rel="running.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    running.id,
                    status="transcription_running",
                )
                paused = orchestrator.store.create(
                    job_id="paused",
                    source_rel="paused.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    paused.id,
                    status="translation_paused",
                    translation_pause_requested=1,
                )

                stopped_count = orchestrator.stop_all_jobs()
                waiting_after_request = orchestrator.store.get(waiting.id)
                running_after_request = orchestrator.store.get(running.id)
                operation = Mock()
                orchestrator._run_stage(
                    running.id,
                    "transcription",
                    operation,
                )
                running_after_stop = orchestrator.store.get(running.id)
                paused_after_request = orchestrator.store.get(paused.id)
            finally:
                orchestrator.stop()

            self.assertEqual(stopped_count, 2)
            self.assertEqual(waiting_after_request.status, "blocked")
            self.assertIn("전체 작업", waiting_after_request.error)
            self.assertTrue(running_after_request.job_stop_requested)
            operation.assert_not_called()
            self.assertEqual(running_after_stop.status, "blocked")
            self.assertFalse(running_after_stop.job_stop_requested)
            self.assertEqual(paused_after_request.status, "translation_paused")

    def test_retries_all_blocked_and_failed_jobs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                blocked = orchestrator.store.create(
                    job_id="blocked",
                    source_rel="blocked.mkv",
                    force_overwrite=False,
                    options={},
                )
                audio_path = root / "state" / "jobs" / blocked.id / "audio.wav"
                audio_path.parent.mkdir(parents=True)
                audio_path.write_bytes(b"audio")
                orchestrator.store.update(
                    blocked.id,
                    status="blocked",
                    audio_path=str(audio_path),
                    blocked_stage="transcription",
                    error="stopped",
                    job_stop_requested=1,
                )
                failed = orchestrator.store.create(
                    job_id="failed",
                    source_rel="failed.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    failed.id,
                    status="failed",
                    blocked_stage="audio extraction",
                    error="failed",
                )
                queued = orchestrator.store.create(
                    job_id="queued",
                    source_rel="queued.mkv",
                    force_overwrite=False,
                    options={},
                )

                retried_count = orchestrator.retry_all_jobs()
                blocked = orchestrator.store.get(blocked.id)
                failed = orchestrator.store.get(failed.id)
                queued = orchestrator.store.get(queued.id)
            finally:
                orchestrator.stop()

            self.assertEqual(retried_count, 2)
            self.assertEqual(blocked.status, "audio_ready")
            self.assertIsNone(blocked.blocked_stage)
            self.assertIsNone(blocked.error)
            self.assertFalse(blocked.job_stop_requested)
            self.assertEqual(failed.status, "queued")
            self.assertIsNone(failed.blocked_stage)
            self.assertIsNone(failed.error)
            self.assertEqual(queued.status, "queued")

    def test_creates_one_job_for_each_selected_media_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "one.mkv").write_bytes(b"media")
            (media_root / "two.mp4").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                jobs = orchestrator.create_jobs(
                    ["one.mkv", "two.mp4", "one.mkv"],
                    force_overwrite=False,
                    options={"duration_seconds": "0"},
                )
            finally:
                orchestrator.stop()

            self.assertEqual(
                [job.source_rel for job in jobs],
                ["one.mkv", "two.mp4"],
            )
            self.assertTrue(
                all(job.options["duration_seconds"] is None for job in jobs)
            )

    def test_folder_expansion_skips_completed_requested_stage(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            folder = media_root / "season"
            folder.mkdir(parents=True)
            for name in (
                "pending.mkv",
                "audio-complete.mkv",
                "transcribed.mkv",
                "completed.mkv",
            ):
                (folder / name).write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                for job_id, source_rel, status in (
                    (
                        "audio-complete-job",
                        "season/audio-complete.mkv",
                        "audio_completed",
                    ),
                    (
                        "transcribed-job",
                        "season/transcribed.mkv",
                        "transcription_completed",
                    ),
                    (
                        "completed-job",
                        "season/completed.mkv",
                        "completed",
                    ),
                ):
                    job = orchestrator.store.create(
                        job_id=job_id,
                        source_rel=source_rel,
                        force_overwrite=False,
                        options={},
                    )
                    orchestrator.store.update(job.id, status=status)

                selected, skipped = orchestrator.expand_job_sources(
                    [],
                    ["season"],
                    force_overwrite=False,
                    operation="transcribe",
                )
            finally:
                orchestrator.stop()

            self.assertEqual(
                set(selected),
                {"season/audio-complete.mkv", "season/pending.mkv"},
            )
            self.assertEqual(skipped, 2)

    def test_batch_is_prevalidated_before_creating_any_job(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "one.mkv").write_bytes(b"media")
            (media_root / "two.mp4").write_bytes(b"media")
            (media_root / "two.ko.srt").write_text(
                "existing",
                encoding="utf-8",
            )
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                with self.assertRaises(FileExistsError):
                    orchestrator.create_jobs(
                        ["one.mkv", "two.mp4"],
                        force_overwrite=False,
                        options={},
                    )
                jobs = orchestrator.store.list_jobs()
            finally:
                orchestrator.stop()

            self.assertEqual(jobs, [])

    def test_batch_is_rejected_when_styled_subtitle_exists(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            (media_root / "movie.ko.ass").write_text(
                "existing",
                encoding="utf-8",
            )
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                with self.assertRaises(FileExistsError):
                    orchestrator.create_job(
                        "movie.mkv",
                        force_overwrite=False,
                        options={},
                    )
            finally:
                orchestrator.stop()

    def test_transcription_translation_and_rendering_keep_service_boundaries(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "movie.mkv"
            source.write_bytes(b"not-read-in-this-test")
            orchestrator = SubtitleOrchestrator(
                WebSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    admin_password="admin-password",
                    session_secret="a" * 32,
                    stt_base_url="http://stt.test",
                    stt_token="stt-token",
                    lm_base_url="http://lm.test/v1",
                    lm_token="lm-token",
                    lm_model="model",
                )
            )
            try:
                job = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={
                        "start_seconds": "10",
                        "backend": "hybrid",
                    },
                )
                audio_path = root / "state" / "jobs" / job.id / "audio.wav"
                audio_path.parent.mkdir(parents=True)
                audio_path.write_bytes(b"fake-audio")
                orchestrator.store.update(
                    job.id,
                    status="audio_ready",
                    audio_path=str(audio_path),
                    audio_sha256="abc",
                )
                def transcribe_with_progress(*_args, **kwargs):
                    kwargs["on_progress"](
                        {
                            "created": 20,
                            "completed": 10,
                            "in_progress": 10,
                        }
                    )
                    return {
                        "schema_version": 1,
                        "job_id": "remote-job",
                        "noise_filter": {
                            "enabled": True,
                            "trigger_level": 7.0,
                            "removed_count": 2,
                            "removed_spans": [],
                        },
                        "segments": [
                            {
                                "id": "segment-000001",
                                "start": 1,
                                "end": 2,
                                "speaker": "SPEAKER_00",
                                "text": "こんにちは",
                            }
                        ],
                    }

                orchestrator.stt_client.transcribe = Mock(
                    side_effect=transcribe_with_progress
                )
                orchestrator._transcribe(orchestrator.store.get(job.id))
                progress_messages = [
                    event["message"]
                    for event in orchestrator.store.events(job.id)
                ]

                translation_client = Mock()
                translation_client.translate = Mock(
                    return_value=[
                        {"id": "segment-000001", "text": "안녕하세요"}
                    ]
                )
                orchestrator._make_translation_client = Mock(
                    return_value=translation_client
                )
                orchestrator._translate(orchestrator.store.get(job.id))
                orchestrator._render(orchestrator.store.get(job.id))
                completed_job = orchestrator.store.get(job.id)
            finally:
                orchestrator.stop()

            subtitle = source.with_name("movie.ko.srt").read_text(encoding="utf-8")
            self.assertIn("00:00:11,000 --> 00:00:12,000", subtitle)
            self.assertIn("안녕하세요", subtitle)
            self.assertNotIn("SPEAKER_00", subtitle)
            self.assertNotIn(
                "transcription chunks: created 20, completed 10, in progress 10",
                progress_messages,
            )
            self.assertIn(
                "noise filter removed 2 non-speech diarization span(s)",
                progress_messages,
            )
            sent_options = (
                orchestrator.stt_client.transcribe.call_args.kwargs["options"]
            )
            self.assertEqual(sent_options["chunk_length_seconds"], 15)
            self.assertTrue(sent_options["noise_filter"])
            self.assertEqual(sent_options["backend"], "hybrid")
            self.assertEqual(
                sent_options["subtitle_segmentation"],
                {
                    "max_gap_sec": 0.8,
                    "max_duration_sec": 8.0,
                    "max_chars": 36,
                    "split_on_speaker_change": True,
                    "prefer_punctuation_boundary": True,
                },
            )
            self.assertEqual(sent_options["repetition_policy"], "flag")
            self.assertEqual(sent_options["repetition_min_count"], 8)
            self.assertEqual(
                sent_options["hybrid_rescue"],
                {
                    "window_padding_sec": 5.0,
                    "max_word_duration_sec": 8.0,
                    "short_segment_duration_sec": 0.2,
                    "short_segment_cluster_window_sec": 5.0,
                    "short_segment_cluster_count": 3,
                    "speaker_debounce_sec": 0.1,
                    "kotoba_chunk_length_seconds": 15,
                    "whisperx_chunk_length_seconds": 30,
                },
            )
            self.assertEqual(completed_job.status, "completed")
            self.assertEqual(
                Path(completed_job.transcript_path).name,
                "movie_translate.json",
            )
            self.assertEqual(
                Path(completed_job.translation_path).name,
                "movie_result_ko.json",
            )
            self.assertEqual(
                Path(completed_job.ass_path).name,
                "movie.ko.ass",
            )
            styled = source.with_name("movie.ko.ass").read_text(
                encoding="utf-8"
            )
            self.assertIn("[V4+ Styles]", styled)
            self.assertNotIn("화자 1", styled)
            self.assertIn("안녕하세요", styled)

    def test_editing_translation_json_regenerates_the_srt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "movie.mkv"
            source.write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            job = orchestrator.create_job(
                "movie.mkv",
                force_overwrite=False,
                options={},
            )
            artifact_dir = root / "state" / "jobs" / job.id
            artifact_dir.mkdir(parents=True)
            transcript_path = artifact_dir / "movie_translate.json"
            translation_path = artifact_dir / "movie_result_ko.json"
            transcript_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "job_id": "remote-job",
                        "segments": [
                            {
                                "id": "segment-000001",
                                "start": 0,
                                "end": 1,
                                "speaker": "SPEAKER_00",
                                "text": "こんにちは",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            translation_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "completed",
                        "translations": [
                            {
                                "id": "segment-000001",
                                "text": "안녕하세요",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            orchestrator.store.update(
                job.id,
                status="completed",
                transcript_path=str(transcript_path),
                translation_path=str(translation_path),
                srt_path=str(media_root / "movie.ko.srt"),
            )
            (media_root / "movie.ko.srt").write_text(
                "old subtitle",
                encoding="utf-8",
            )
            edited = json.dumps(
                {
                    "schema_version": 1,
                    "status": "completed",
                    "translations": [
                        {
                            "id": "segment-000001",
                            "text": "수정된 번역",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            try:
                orchestrator.save_artifact(job.id, "translation", edited)
            finally:
                orchestrator.stop()

            self.assertIn(
                "수정된 번역",
                (media_root / "movie.ko.srt").read_text(encoding="utf-8"),
            )
            self.assertIn(
                "수정된 번역",
                (media_root / "movie.ko.ass").read_text(encoding="utf-8"),
            )

    def test_restart_translation_preserves_transcript_and_rerenders(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "movie.mkv"
            source.write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            job = orchestrator.create_job(
                "movie.mkv",
                force_overwrite=False,
                options={},
            )
            artifact_dir = root / "state" / "jobs" / job.id
            artifact_dir.mkdir(parents=True)
            transcript_path = artifact_dir / "movie_translate.json"
            translation_path = artifact_dir / "movie_result_ko.json"
            transcript_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "job_id": "remote-job",
                        "segments": [
                            {
                                "id": "segment-000001",
                                "start": 0,
                                "end": 1,
                                "speaker": "SPEAKER_00",
                                "text": "こんにちは",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            translation_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "completed",
                        "transcript_job_id": "remote-job",
                        "translations": [
                            {
                                "id": "segment-000001",
                                "text": "이전 번역",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            srt_path = media_root / "movie.ko.srt"
            ass_path = media_root / "movie.ko.ass"
            srt_path.write_text("old srt", encoding="utf-8")
            ass_path.write_text("old ass", encoding="utf-8")
            orchestrator.store.update(
                job.id,
                status="completed",
                transcript_path=str(transcript_path),
                translation_path=str(translation_path),
                srt_path=str(srt_path),
                ass_path=str(ass_path),
            )
            original_transcript = transcript_path.read_bytes()
            translation_client = Mock()
            translation_client.translate = Mock(
                return_value=[
                    {
                        "id": "segment-000001",
                        "text": "새 번역",
                    }
                ]
            )
            orchestrator._make_translation_client = Mock(
                return_value=translation_client
            )

            try:
                restarted = orchestrator.restart_translation(job.id, "variety")
                checkpoint = json.loads(
                    translation_path.read_text(encoding="utf-8")
                )
                orchestrator._translate(restarted)
                orchestrator._render(orchestrator.store.get(job.id))
                completed = orchestrator.store.get(job.id)
                messages = [
                    event["message"]
                    for event in orchestrator.store.events(job.id)
                ]
            finally:
                orchestrator.stop()

            self.assertEqual(restarted.status, "transcribed")
            self.assertEqual(checkpoint["status"], "partial")
            self.assertEqual(checkpoint["translations"], [])
            self.assertEqual(
                transcript_path.read_bytes(),
                original_transcript,
            )
            self.assertEqual(completed.status, "completed")
            self.assertIn("새 번역", srt_path.read_text(encoding="utf-8"))
            self.assertIn("새 번역", ass_path.read_text(encoding="utf-8"))
            self.assertNotIn("old srt", srt_path.read_text(encoding="utf-8"))
            self.assertEqual(
                translation_client.translate.call_args.kwargs["existing"],
                {},
            )
            self.assertEqual(
                restarted.options["translation_prompt"]["category_id"],
                "variety",
            )
            self.assertEqual(
                translation_client.translate.call_args.kwargs["review_rounds"],
                2,
            )
            self.assertIn(
                "television variety",
                translation_client.translate.call_args.kwargs["system_prompt"],
            )
            self.assertTrue(
                any(
                    "transcript preserved" in message
                    for message in messages
                )
            )

    def test_restart_translation_rejects_an_incomplete_job(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            job = orchestrator.create_job(
                "movie.mkv",
                force_overwrite=False,
                options={},
            )
            try:
                with self.assertRaisesRegex(ValueError, "completed jobs"):
                    orchestrator.restart_translation(job.id)
            finally:
                orchestrator.stop()
