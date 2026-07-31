import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from stt_to_subtitle.nas_config import NASSettings, RemoteServerSettings
from stt_to_subtitle.orchestrator import NASOrchestrator
from stt_to_subtitle.service_clients import TranslationPaused


class NASOrchestratorTests(unittest.TestCase):
    def make_orchestrator(
        self,
        root: Path,
        media_root: Path,
    ) -> NASOrchestrator:
        return NASOrchestrator(
            NASSettings(
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
            orchestrator = NASOrchestrator(
                NASSettings(
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
            self.assertEqual(job.options["chunk_length_seconds"], 60)
            self.assertTrue(job.options["noise_filter"])

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
            orchestrator = NASOrchestrator(
                NASSettings(
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

    def test_audio_only_job_does_not_require_remote_servers(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = NASOrchestrator(
                NASSettings(
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
                job = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={},
                    operation="extract",
                )

                def fake_extract(_source, target, _options):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"wave")

                with patch(
                    "stt_to_subtitle.orchestrator.extract_audio",
                    side_effect=fake_extract,
                ):
                    orchestrator._extract(job)
                completed = orchestrator.store.get(job.id)
            finally:
                orchestrator.stop()

            self.assertEqual(completed.status, "audio_completed")
            self.assertEqual(completed.operation, "extract")
            self.assertTrue(Path(completed.audio_path).is_file())

    def test_translation_reuses_latest_extracted_audio_or_falls_back(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                extracted = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={"start_seconds": "12"},
                    operation="extract",
                )
                audio_path = root / "state" / "jobs" / extracted.id / "audio.wav"
                audio_path.parent.mkdir(parents=True)
                audio_path.write_bytes(b"wave")
                orchestrator.store.update(
                    extracted.id,
                    status="audio_completed",
                    audio_path=str(audio_path),
                    audio_sha256="digest",
                )

                reused = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=True,
                    options={"chunk_length_seconds": "30"},
                    operation="translate",
                )
                audio_path.unlink()
                orchestrator.store.update(extracted.id, status="audio_completed")
                fallback = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=True,
                    options={"chunk_length_seconds": "20"},
                    operation="translate",
                )
            finally:
                orchestrator.stop()

            self.assertEqual(reused.id, extracted.id)
            self.assertEqual(reused.status, "audio_ready")
            self.assertEqual(reused.options["start_seconds"], 12.0)
            self.assertEqual(reused.options["chunk_length_seconds"], 30)
            self.assertEqual(fallback.id, extracted.id)
            self.assertEqual(fallback.status, "queued")
            self.assertIsNone(fallback.audio_path)
            self.assertEqual(fallback.options["chunk_length_seconds"], 20)

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
            orchestrator = NASOrchestrator(
                NASSettings(
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
                    options={"start_seconds": "10"},
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
            self.assertEqual(sent_options["chunk_length_seconds"], 60)
            self.assertTrue(sent_options["noise_filter"])
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
                restarted = orchestrator.restart_translation(job.id)
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
