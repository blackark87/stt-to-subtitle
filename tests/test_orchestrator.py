import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from stt_to_subtitle.web_config import (
    RemoteServerSettings,
    SubtitleValidatorSettings,
    WebSettings,
)
from stt_to_subtitle.orchestrator import (
    SubtitleOrchestrator,
    estimate_transcription_chunks,
)
from stt_to_subtitle.job_store import JobStore
from stt_to_subtitle.service_clients import (
    ExternalServiceError,
    RemoteTranscriptionFailed,
    TranslationPaused,
)


class SubtitleOrchestratorTests(unittest.TestCase):
    def test_uses_separate_work_storage_and_rebases_saved_paths(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            work_dir = root / "work"
            media_root = root / "media"
            media_root.mkdir()
            store = JobStore(state_dir / "jobs.sqlite3")
            job = store.create(
                job_id="legacy-job",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )
            store.update(
                job.id,
                audio_path=str(state_dir / "jobs" / job.id / "audio.wav"),
            )

            orchestrator = SubtitleOrchestrator(
                WebSettings(
                    state_dir=state_dir,
                    work_dir=work_dir,
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
                rebased = orchestrator.store.get(job.id)
                self.assertTrue(work_dir.is_dir())
                self.assertEqual(
                    rebased.audio_path,
                    str(work_dir / job.id / "audio.wav"),
                )
            finally:
                orchestrator.stop()

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
        self.assertEqual(
            estimate_transcription_chunks(
                6.0,
                {
                    "backend": "whisperjav",
                    "whisperjav": {
                        "anime_max_group_duration_seconds": 2.0,
                        "qwen_max_group_duration_seconds": 3.0,
                    },
                },
            ),
            5,
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

    def test_manual_lm_gate_dispatches_only_after_explicit_preflight(self) -> None:
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
                    stt_base_url="http://stt.test",
                    stt_token="",
                    lm_base_url="http://lm.test/v1",
                    lm_token="secret",
                    lm_model="model",
                    lm_manual_start=True,
                )
            )
            try:
                job = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(job.id, status="transcribed")
                orchestrator._translation_executor.submit = Mock()

                self.assertEqual(orchestrator._dispatch_translations(), 0)
                with patch(
                    "stt_to_subtitle.orchestrator.list_openai_compatible_models",
                    return_value=["model"],
                ) as models:
                    resumed = orchestrator.activate_translation_lm()
                dispatched = orchestrator._dispatch_translations()
                persisted_gate = orchestrator.store.get_dependency_state(
                    "translation_lm"
                )
            finally:
                orchestrator.stop()

            self.assertEqual(resumed, 0)
            self.assertEqual(dispatched, 1)
            self.assertEqual(orchestrator.lm_gate_state, "ready")
            self.assertEqual(persisted_gate["state"], "ready")
            self.assertIsNone(persisted_gate["reason_code"])
            models.assert_called_once_with(
                "http://lm.test/v1",
                "secret",
                attempts=1,
            )

    def test_manual_lm_gate_starts_closed_after_process_restart(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            settings = WebSettings(
                state_dir=root / "state",
                media_root=media_root,
                admin_password="",
                session_secret="",
                stt_base_url="http://stt.test",
                stt_token="",
                lm_base_url="http://lm.test/v1",
                lm_token="secret",
                lm_model="model",
                lm_manual_start=True,
            )
            first = SubtitleOrchestrator(settings)
            try:
                with patch(
                    "stt_to_subtitle.orchestrator.list_openai_compatible_models",
                    return_value=["model"],
                ):
                    first.activate_translation_lm()
                self.assertEqual(first.lm_gate_state, "ready")
            finally:
                first.stop()

            with patch(
                "stt_to_subtitle.orchestrator.list_openai_compatible_models"
            ) as models:
                restarted = SubtitleOrchestrator(settings)
            try:
                persisted = restarted.store.get_dependency_state(
                    "translation_lm"
                )
                self.assertEqual(restarted.lm_gate_state, "offline")
                self.assertEqual(persisted["state"], "offline")
                self.assertEqual(
                    persisted["reason_code"],
                    "manual_start_required",
                )
                models.assert_not_called()
            finally:
                restarted.stop()

    def test_stt_gate_stops_queue_cascade_until_explicit_preflight(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                running = orchestrator.store.create(
                    job_id="stt-running",
                    source_rel="running.mkv",
                    force_overwrite=False,
                    options={},
                )
                waiting = orchestrator.store.create(
                    job_id="stt-waiting",
                    source_rel="waiting.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    running.id,
                    status="transcription_running",
                )
                orchestrator.store.update(waiting.id, status="audio_ready")
                orchestrator._stt_executor.submit = Mock()

                orchestrator._run_stage(
                    running.id,
                    "transcription",
                    Mock(side_effect=ExternalServiceError("connection refused")),
                )
                orchestrator._scheduler_tick()

                self.assertEqual(orchestrator.stt_gate_state, "lost")
                self.assertEqual(
                    orchestrator.store.get(waiting.id).status,
                    "audio_ready",
                )
                orchestrator._stt_executor.submit.assert_not_called()
                persisted = orchestrator.store.get_dependency_state("stt")
                self.assertEqual(persisted["state"], "lost")
                self.assertEqual(
                    persisted["reason_code"],
                    "stt_unavailable",
                )

                orchestrator.stt_client.check_readiness = Mock(
                    return_value={"status": "ready"}
                )
                resumed = orchestrator.activate_transcription_stt()
                self.assertEqual(resumed, 1)
                self.assertEqual(orchestrator.stt_gate_state, "ready")
                orchestrator.stt_client.check_readiness.assert_called_once_with()
            finally:
                orchestrator.stop()

            reloaded = self.make_orchestrator(root, media_root)
            try:
                self.assertEqual(reloaded.stt_gate_state, "ready")
            finally:
                reloaded.stop()

    def test_paid_subtitle_validation_is_cached_by_payload_and_model(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                orchestrator.store.create(
                    job_id="job-1",
                    source_rel="movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                validation = orchestrator.store.save_subtitle_validation(
                    job_id="job-1",
                    source_rel="movie.mkv",
                    external_path="movie.srt",
                    external_hash="external",
                    candidate_path="movie.ko.srt",
                    candidate_hash="candidate",
                    metrics={
                        "summary": {"time_coverage": 1.0},
                        "issues": [],
                        "alignments": [],
                    },
                )
                orchestrator.update_subtitle_validator(
                    SubtitleValidatorSettings(
                        base_url="https://validator.test/v1",
                        token="paid-token",
                        model="paid-model",
                    )
                )
                result = {
                    "severity": "pass",
                    "severity_label": "통과",
                    "summary": "통과",
                    "findings": [],
                }
                with patch(
                    "stt_to_subtitle.orchestrator.SubtitleValidationClient"
                ) as client:
                    client.return_value.validate.return_value = result
                    first, first_cached = orchestrator.validate_subtitles_with_llm(
                        validation["id"]
                    )
                    second, second_cached = orchestrator.validate_subtitles_with_llm(
                        validation["id"]
                    )
            finally:
                orchestrator.stop()

            self.assertFalse(first_cached)
            self.assertTrue(second_cached)
            self.assertEqual(first["llm"], result)
            self.assertEqual(second["llm"], result)
            client.return_value.validate.assert_called_once()

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
                with self.assertRaisesRegex(
                    ValueError, "must be at most 30"
                ):
                    orchestrator.create_job(
                        "movie.mkv",
                        force_overwrite=False,
                        options={
                            "backend": "whisperx",
                            "chunk_length_seconds": 31,
                        },
                    )
            finally:
                orchestrator.stop()

    def test_keeps_an_optional_batch_size_for_whisperx_backends(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                for index, backend in enumerate(("whisperx", "hybrid")):
                    with self.subTest(backend=backend):
                        job = orchestrator.create_job(
                            "movie.mkv",
                            force_overwrite=True,
                            options={
                                "backend": backend,
                                "batch_size": 16,
                                "start_seconds": index,
                            },
                        )

                        self.assertEqual(job.options["batch_size"], 16)
            finally:
                orchestrator.stop()

    def test_omits_batch_size_when_the_request_leaves_it_unset(self) -> None:
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
                    options={"backend": "hybrid"},
                )

                self.assertNotIn("batch_size", job.options)
            finally:
                orchestrator.stop()

    def test_rejects_batch_size_that_the_stt_service_would_refuse(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                for backend in ("kotoba", "whisperjav"):
                    with self.subTest(backend=backend):
                        with self.assertRaisesRegex(
                            ValueError, "batch_size requires backend"
                        ):
                            orchestrator.create_job(
                                "movie.mkv",
                                force_overwrite=True,
                                options={
                                    "backend": backend,
                                    "batch_size": 8,
                                },
                            )
                for value in (0, 65):
                    with self.subTest(value=value):
                        with self.assertRaisesRegex(
                            ValueError, "between 1 and 64"
                        ):
                            orchestrator.create_job(
                                "movie.mkv",
                                force_overwrite=True,
                                options={
                                    "backend": "whisperx",
                                    "batch_size": value,
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

    def test_translation_workers_are_reserved_for_one_file_at_a_time(
        self,
    ) -> None:
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

                first_dispatched = orchestrator._dispatch_translations()
                running = orchestrator.store.ids_with_status(
                    "translation_running"
                )
                waiting = orchestrator.store.ids_with_status("transcribed")
                second_dispatched = orchestrator._dispatch_translations()
                orchestrator.store.update(running[0], status="translated")
                third_dispatched = orchestrator._dispatch_translations()
                next_running = orchestrator.store.ids_with_status(
                    "translation_running"
                )
                next_waiting = orchestrator.store.ids_with_status(
                    "transcribed"
                )
            finally:
                orchestrator.stop()

            self.assertEqual(first_dispatched, 1)
            self.assertEqual(len(running), 1)
            self.assertEqual(len(waiting), 2)
            self.assertEqual(second_dispatched, 0)
            self.assertEqual(third_dispatched, 1)
            self.assertEqual(len(next_running), 1)
            self.assertEqual(len(next_waiting), 1)
            self.assertEqual(
                orchestrator._translation_executor.submit.call_count,
                2,
            )

    def test_running_stage_refreshes_and_releases_its_worker_lease(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.store.create(
                    job_id="lease-heartbeat",
                    source_rel="movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.claim_for_dispatch(
                    job.id,
                    "queued",
                    "extracting",
                    lease_owner=orchestrator._worker_id,
                    lease_seconds=60,
                )
                refreshed = threading.Event()
                refresh_lease = orchestrator.store.refresh_job_lease

                def record_refresh(*args, **kwargs):
                    result = refresh_lease(*args, **kwargs)
                    refreshed.set()
                    return result

                def complete_stage(_job):
                    self.assertTrue(refreshed.wait(timeout=1))
                    orchestrator.store.update(job.id, status="audio_ready")

                with patch(
                    "stt_to_subtitle.orchestrator."
                    "JOB_LEASE_HEARTBEAT_SECONDS",
                    0.001,
                ), patch.object(
                    orchestrator.store,
                    "refresh_job_lease",
                    side_effect=record_refresh,
                ) as heartbeat:
                    orchestrator._run_stage(
                        job.id,
                        "audio extraction",
                        complete_stage,
                    )
                completed = orchestrator.store.get(job.id)
            finally:
                orchestrator.stop()

            heartbeat.assert_called()
            self.assertEqual(completed.status, "audio_ready")
            self.assertIsNone(completed.lease_owner)
            self.assertIsNone(completed.lease_expires_at)

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

    def test_translation_continues_completed_transcription_job(self) -> None:
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
                all_jobs = orchestrator.store.list_jobs(limit=None)
            finally:
                orchestrator.stop()

            self.assertEqual(reused.id, transcribed.id)
            self.assertEqual(reused.status, "transcribed")
            self.assertEqual(reused.operation, "full")
            self.assertEqual(reused.options["start_seconds"], 12.0)
            self.assertEqual(
                reused.options["translation_prompt"]["category_id"],
                "variety",
            )
            self.assertEqual(reused.transcript_path, str(transcript_path))
            self.assertEqual(
                json.loads(
                    Path(reused.transcript_path).read_text(encoding="utf-8")
                ),
                json.loads(transcript_path.read_text(encoding="utf-8")),
            )
            self.assertEqual(original.status, "transcribed")
            self.assertEqual(original.id, reused.id)
            self.assertEqual([job.id for job in all_jobs], [transcribed.id])

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
                    kwargs["on_batch_started"](
                        0,
                        ["segment-000001"],
                    )
                    kwargs["on_logical_batch"](0, items)
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
                generations = orchestrator.store.list_translation_generations(
                    job.id
                )
                generation_items = orchestrator.store.translation_items(
                    generations[0]["id"]
                )
                generation_batches = orchestrator.store.translation_batches(
                    generations[0]["id"]
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
            self.assertEqual(generations[0]["state"], "paused")
            self.assertEqual(generations[0]["attempt"], 1)
            self.assertEqual(generation_items[0]["text"], "안녕하세요")
            self.assertEqual(len(generation_batches), 1)
            self.assertEqual(generation_batches[0]["state"], "completed")
            self.assertTrue(Path(generations[0]["artifact_path"]).is_file())

    def test_translation_uses_configured_workers_for_one_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                orchestrator.update_remote_servers(
                    RemoteServerSettings(
                        stt_base_url="http://stt.test",
                        stt_token="",
                        lm_base_url="http://lm.test/v1",
                        lm_token="",
                        lm_model="model",
                        translation_workers=3,
                    )
                )
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
                )
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
            finally:
                orchestrator.stop()

            self.assertEqual(
                translation_client.translate.call_args.kwargs["max_workers"],
                3,
            )

    def test_remote_transcription_contract_error_is_failed_not_blocked(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.store.create(
                    job_id="invalid-remote-output",
                    source_rel="movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    job.id,
                    status="transcription_running",
                )

                orchestrator._run_stage(
                    job.id,
                    "transcription",
                    Mock(
                        side_effect=RemoteTranscriptionFailed(
                            "segments must be a list",
                            failure_code="model_output_invalid",
                        )
                    ),
                )
                failed = orchestrator.store.get(job.id)
            finally:
                orchestrator.stop()

            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.state, "failed")
            self.assertEqual(failed.reason_code, "model_output_invalid")
            self.assertEqual(failed.blocked_stage, "transcription")

    def test_remote_transcription_auth_error_blocks_configuration(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.store.create(
                    job_id="stt-auth-error",
                    source_rel="movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    job.id,
                    status="transcription_running",
                )

                orchestrator._run_stage(
                    job.id,
                    "transcription",
                    Mock(
                        side_effect=RemoteTranscriptionFailed(
                            "authentication failed",
                            failure_code="auth_required",
                            retryable=False,
                            failure_scope="configuration",
                        )
                    ),
                )
                blocked = orchestrator.store.get(job.id)
                gate = orchestrator.store.get_dependency_state("stt")
            finally:
                orchestrator.stop()

            self.assertEqual(blocked.status, "blocked")
            self.assertEqual(blocked.state, "blocked")
            self.assertEqual(blocked.reason_code, "auth_required")
            self.assertEqual(orchestrator.stt_gate_state, "lost")
            self.assertEqual(gate["reason_code"], "auth_required")

    def test_translation_persists_a_failed_logical_batch(self) -> None:
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
                )
                translation_client = Mock()

                def fail_batch(_segments, **kwargs):
                    segment_ids = ["segment-000001"]
                    kwargs["on_batch_started"](0, segment_ids)
                    kwargs["on_batch_failed"](
                        0,
                        segment_ids,
                        "server offline",
                    )
                    raise ExternalServiceError("server offline")

                translation_client.translate = Mock(side_effect=fail_batch)
                orchestrator._make_translation_client = Mock(
                    return_value=translation_client
                )

                orchestrator._run_stage(
                    job.id,
                    "translation",
                    orchestrator._translate,
                )
                blocked = orchestrator.store.get(job.id)
                generation = (
                    orchestrator.store.latest_translation_generation(job.id)
                )
                batches = orchestrator.store.translation_batches(
                    generation["id"]
                )
            finally:
                orchestrator.stop()

            self.assertEqual(blocked.status, "blocked")
            self.assertEqual(generation["state"], "blocked")
            self.assertEqual(batches[0]["state"], "failed")
            self.assertEqual(batches[0]["error"], "server offline")

    def test_translation_rebuilds_missing_json_from_database_items(self) -> None:
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
            artifact_dir = root / "state" / "jobs" / job.id
            artifact_dir.mkdir(parents=True)
            transcript_path = artifact_dir / "movie_translate.json"
            transcript_payload = {
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
            transcript_path.write_text(
                json.dumps(transcript_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            orchestrator.store.update(
                job.id,
                status="translation_running",
                transcript_path=str(transcript_path),
            )
            running = orchestrator.store.get(job.id)
            prompt_snapshot = running.options["translation_prompt"]
            generation = orchestrator._create_translation_generation(
                running,
                transcript_payload,
                prompt_snapshot,
                orchestrator.remote_servers,
                origin="automatic",
            )
            orchestrator.store.save_translation_batch(
                generation["id"],
                batch_index=0,
                generation_attempt=0,
                kind="remote",
                items=orchestrator._translation_item_records(
                    transcript_payload["segments"],
                    [{"id": "segment-000001", "text": "DB 번역"}],
                ),
            )
            translation_client = Mock()

            def finish_from_existing(_segments, **kwargs):
                self.assertEqual(
                    kwargs["existing"],
                    {"segment-000001": "DB 번역"},
                )
                return [{"id": "segment-000001", "text": "DB 번역"}]

            translation_client.translate = Mock(side_effect=finish_from_existing)
            orchestrator._make_translation_client = Mock(
                return_value=translation_client
            )
            try:
                orchestrator._translate(running)
                completed = orchestrator.store.get(job.id)
                snapshot = json.loads(
                    Path(completed.translation_path).read_text(encoding="utf-8")
                )
                stored_generation = (
                    orchestrator.store.latest_translation_generation(job.id)
                )
            finally:
                orchestrator.stop()

            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["translations"][0]["text"], "DB 번역")
            self.assertEqual(stored_generation["state"], "completed")
            self.assertEqual(stored_generation["attempt"], 1)

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
                    stt_job_id="remote-running",
                )
                orchestrator.stt_client.cancel_job = Mock(
                    return_value={"status": "cancel_requested"}
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
            self.assertEqual(waiting_after_request.state, "stopped")
            self.assertEqual(waiting_after_request.reason_code, "user_stop")
            self.assertIn("전체 작업", waiting_after_request.error)
            self.assertTrue(running_after_request.job_stop_requested)
            operation.assert_not_called()
            self.assertEqual(running_after_stop.status, "blocked")
            self.assertEqual(running_after_stop.state, "stopped")
            self.assertEqual(running_after_stop.reason_code, "user_stop")
            self.assertFalse(running_after_stop.job_stop_requested)
            self.assertEqual(paused_after_request.status, "translation_paused")
            orchestrator.stt_client.cancel_job.assert_called_once_with(
                "remote-running"
            )

    def test_startup_reconciles_each_running_stage_from_its_checkpoint(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                audio_path = root / "audio.wav"
                audio_path.write_bytes(b"audio")
                transcript_path = root / "transcript.json"
                transcript_payload = {
                    "schema_version": 1,
                    "job_id": "remote-job",
                    "segments": [
                        {
                            "id": "segment-000001",
                            "start": 0.0,
                            "end": 1.0,
                            "speaker": "SPEAKER_00",
                            "text": "こんにちは",
                        }
                    ],
                }
                transcript_path.write_text(
                    json.dumps(transcript_payload, ensure_ascii=False),
                    encoding="utf-8",
                )
                translation_path = root / "translation.json"
                translation_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "job_id": "translation-job",
                            "translations": [
                                {
                                    "id": "segment-000001",
                                    "text": "안녕하세요",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                extracting = orchestrator.store.create(
                    job_id="restart-extracting",
                    source_rel="extracting.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(extracting.id, status="extracting")
                remote = orchestrator.store.create(
                    job_id="restart-remote",
                    source_rel="remote.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    remote.id,
                    status="transcription_running",
                    audio_path=str(audio_path),
                    stt_job_id="remote-stt-job",
                )
                local_transcription = orchestrator.store.create(
                    job_id="restart-local-transcription",
                    source_rel="local.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    local_transcription.id,
                    status="transcription_running",
                    audio_path=str(audio_path),
                )
                translating = orchestrator.store.create(
                    job_id="restart-translating",
                    source_rel="translating.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    translating.id,
                    status="translation_running",
                    transcript_path=str(transcript_path),
                )
                rendering = orchestrator.store.create(
                    job_id="restart-rendering",
                    source_rel="rendering.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    rendering.id,
                    status="rendering",
                    transcript_path=str(transcript_path),
                    translation_path=str(translation_path),
                )
                stopping = orchestrator.store.create(
                    job_id="restart-stopping",
                    source_rel="stopping.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    stopping.id,
                    status="rendering",
                    job_stop_requested=1,
                )
                remote_stopping = orchestrator.store.create(
                    job_id="restart-remote-stopping",
                    source_rel="remote-stopping.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    remote_stopping.id,
                    status="transcription_running",
                    stt_job_id="remote-stopping-job",
                    job_stop_requested=1,
                )
                actively_owned = orchestrator.store.create(
                    job_id="active-other-worker",
                    source_rel="active.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.claim_for_dispatch(
                    actively_owned.id,
                    "queued",
                    "extracting",
                    lease_owner="other-worker",
                    lease_seconds=60,
                )
                orchestrator._stt_executor.submit = Mock()

                recovered = orchestrator._reconcile_interrupted_jobs()

                self.assertEqual(recovered, 7)
                self.assertEqual(
                    orchestrator.store.get(extracting.id).status,
                    "queued",
                )
                self.assertEqual(
                    orchestrator.store.get(remote.id).status,
                    "transcription_running",
                )
                self.assertEqual(
                    orchestrator.store.get(local_transcription.id).status,
                    "audio_ready",
                )
                self.assertEqual(
                    orchestrator.store.get(translating.id).status,
                    "transcribed",
                )
                self.assertEqual(
                    orchestrator.store.get(rendering.id).status,
                    "translated",
                )
                stopped = orchestrator.store.get(stopping.id)
                self.assertEqual(stopped.state, "stopped")
                self.assertEqual(stopped.phase, "render")
                self.assertEqual(
                    orchestrator.store.get(remote_stopping.id).status,
                    "transcription_running",
                )
                self.assertEqual(orchestrator._stt_executor.submit.call_count, 2)
                active_after_reconcile = orchestrator.store.get(
                    actively_owned.id
                )
                self.assertEqual(active_after_reconcile.status, "extracting")
                self.assertEqual(
                    active_after_reconcile.lease_owner,
                    "other-worker",
                )
                self.assertEqual(
                    orchestrator.store.get(remote.id).lease_owner,
                    orchestrator._worker_id,
                )
                orchestrator._stt_executor.submit.assert_any_call(
                    orchestrator._run_stage,
                    remote.id,
                    "transcription",
                    orchestrator._transcribe,
                )
                orchestrator._stt_executor.submit.assert_any_call(
                    orchestrator._run_stage,
                    remote_stopping.id,
                    "transcription",
                    orchestrator._cancel_interrupted_transcription,
                )
            finally:
                orchestrator.stop()

    def test_stops_only_selected_jobs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                waiting = orchestrator.store.create(
                    job_id="selected-waiting",
                    source_rel="selected-waiting.mkv",
                    force_overwrite=False,
                    options={},
                )
                running = orchestrator.store.create(
                    job_id="selected-running",
                    source_rel="selected-running.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    running.id,
                    status="transcription_running",
                )
                untouched = orchestrator.store.create(
                    job_id="untouched",
                    source_rel="untouched.mkv",
                    force_overwrite=False,
                    options={},
                )

                stopped_count = orchestrator.stop_jobs(
                    [waiting.id, running.id, waiting.id, "missing"]
                )
                waiting = orchestrator.store.get(waiting.id)
                running = orchestrator.store.get(running.id)
                untouched = orchestrator.store.get(untouched.id)
            finally:
                orchestrator.stop()

            self.assertEqual(stopped_count, 2)
            self.assertEqual(waiting.status, "blocked")
            self.assertEqual(waiting.state, "stopped")
            self.assertEqual(waiting.reason_code, "user_stop")
            self.assertEqual(
                waiting.error,
                "사용자 요청으로 작업이 중단되었습니다.",
            )
            self.assertTrue(running.job_stop_requested)
            self.assertEqual(untouched.status, "queued")

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
            self.assertEqual(blocked.state, "waiting")
            self.assertEqual(blocked.attempt, 2)
            self.assertIsNone(blocked.reason_code)
            self.assertIsNone(blocked.blocked_stage)
            self.assertIsNone(blocked.error)
            self.assertFalse(blocked.job_stop_requested)
            self.assertEqual(failed.status, "queued")
            self.assertEqual(failed.state, "waiting")
            self.assertEqual(failed.attempt, 2)
            self.assertIsNone(failed.blocked_stage)
            self.assertIsNone(failed.error)
            self.assertEqual(queued.status, "queued")

    def test_retries_only_selected_blocked_and_failed_jobs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                blocked = orchestrator.store.create(
                    job_id="selected-blocked",
                    source_rel="selected-blocked.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    blocked.id,
                    status="blocked",
                    blocked_stage="audio extraction",
                    error="stopped",
                )
                failed = orchestrator.store.create(
                    job_id="selected-failed",
                    source_rel="selected-failed.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    failed.id,
                    status="failed",
                    blocked_stage="translation",
                    error="failed",
                )
                untouched = orchestrator.store.create(
                    job_id="untouched-blocked",
                    source_rel="untouched-blocked.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(untouched.id, status="blocked")
                queued = orchestrator.store.create(
                    job_id="already-queued",
                    source_rel="already-queued.mkv",
                    force_overwrite=False,
                    options={},
                )

                retried_count = orchestrator.retry_jobs(
                    [
                        blocked.id,
                        failed.id,
                        blocked.id,
                        queued.id,
                        "missing",
                    ]
                )
                blocked = orchestrator.store.get(blocked.id)
                failed = orchestrator.store.get(failed.id)
                untouched = orchestrator.store.get(untouched.id)
                queued = orchestrator.store.get(queued.id)
            finally:
                orchestrator.stop()

            self.assertEqual(retried_count, 2)
            self.assertEqual(blocked.status, "queued")
            self.assertEqual(failed.status, "queued")
            self.assertEqual(untouched.status, "blocked")
            self.assertEqual(queued.status, "queued")

    def test_retry_reduces_legacy_whisperx_chunk_to_native_window(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                legacy = orchestrator.store.create(
                    job_id="legacy-whisperx",
                    source_rel="movie.mkv",
                    force_overwrite=False,
                    options={
                        "backend": "whisperx",
                        "chunk_length_seconds": 60,
                    },
                    operation="transcribe",
                )
                orchestrator.store.update(
                    legacy.id,
                    status="blocked",
                    blocked_stage="transcription",
                    error="invalid input shape",
                )

                retried = orchestrator.retry(legacy.id)
                events = orchestrator.store.events(legacy.id)
            finally:
                orchestrator.stop()

            self.assertEqual(retried.status, "queued")
            self.assertEqual(retried.options["chunk_length_seconds"], 30)
            self.assertTrue(
                any(
                    "reduced to 30 seconds" in event["message"]
                    for event in events
                )
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

    def test_comparison_selection_ignores_completed_subtitles(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mp4").write_bytes(b"media")
            (media_root / "movie.ko.srt").write_text(
                "existing subtitle",
                encoding="utf-8",
            )
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                completed = orchestrator.store.create(
                    job_id="completed",
                    source_rel="movie.mp4",
                    force_overwrite=False,
                    options={},
                    operation="full",
                )
                orchestrator.store.update(completed.id, status="completed")

                selected, skipped = orchestrator.expand_job_sources(
                    ["movie.mp4"],
                    [],
                    force_overwrite=False,
                    operation="compare",
                )
            finally:
                orchestrator.stop()

            self.assertEqual(selected, ["movie.mp4"])
            self.assertEqual(skipped, 0)

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
                        "batch_size": 12,
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
            self.assertEqual(sent_options["batch_size"], 12)
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
                    "rescue_scope": "windows",
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
                generations = orchestrator.store.list_translation_generations(
                    job.id
                )
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
            self.assertEqual(
                [generation["origin"] for generation in generations],
                ["legacy", "manual"],
            )
            self.assertEqual(
                orchestrator.store.translation_items(generations[0]["id"])[0][
                    "text"
                ],
                "안녕하세요",
            )
            self.assertEqual(
                orchestrator.store.translation_items(generations[1]["id"])[0][
                    "text"
                ],
                "수정된 번역",
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
                generations = orchestrator.store.list_translation_generations(
                    job.id
                )
                generation_snapshots = [
                    json.loads(
                        Path(generation["artifact_path"]).read_text(
                            encoding="utf-8"
                        )
                    )
                    for generation in generations
                ]
                subtitle_generations = (
                    orchestrator.store.list_subtitle_generations(job.id)
                )
                generated_srt = srt_path.read_text(encoding="utf-8")
                orchestrator.publish_subtitle_generation(
                    job.id,
                    subtitle_generations[0]["id"],
                )
                rolled_back_srt = srt_path.read_text(encoding="utf-8")
                rolled_back_publication = (
                    orchestrator.store.published_subtitle_generation(job.id)
                )
                orchestrator.publish_subtitle_generation(
                    job.id,
                    subtitle_generations[1]["id"],
                )
                republished_srt = srt_path.read_text(encoding="utf-8")
                manifest_path = (
                    orchestrator._subtitle_publication_manifest_path(
                        job.source_rel
                    )
                )
                publication_manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                orchestrator.store.publish_subtitle_generation(
                    subtitle_generations[0]["id"],
                    srt_path=str(srt_path),
                    ass_path=str(ass_path),
                )
                manifest_repaired = (
                    orchestrator._reconcile_subtitle_publications()
                )
                reconciled_publication = (
                    orchestrator.store.published_subtitle_generation(job.id)
                )
                manifest_path.unlink()
                srt_path.write_text("partial replacement", encoding="utf-8")
                pair_repaired = (
                    orchestrator._reconcile_subtitle_publications()
                )
                repaired_srt = srt_path.read_text(encoding="utf-8")
                repaired_ass = ass_path.read_text(encoding="utf-8")
                Path(
                    subtitle_generations[0]["srt_artifact_path"]
                ).write_text("tampered", encoding="utf-8")
                with self.assertRaisesRegex(
                    ValueError,
                    "generation file is invalid",
                ):
                    orchestrator.publish_subtitle_generation(
                        job.id,
                        subtitle_generations[0]["id"],
                    )
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
            self.assertIn("새 번역", generated_srt)
            self.assertIn("새 번역", ass_path.read_text(encoding="utf-8"))
            self.assertNotIn("old srt", generated_srt)
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
            self.assertEqual(len(generations), 2)
            self.assertEqual(
                [generation["state"] for generation in generations],
                ["completed", "completed"],
            )
            self.assertEqual(
                generation_snapshots[0]["translations"][0]["text"],
                "이전 번역",
            )
            self.assertEqual(
                generation_snapshots[1]["translations"][0]["text"],
                "새 번역",
            )
            self.assertEqual(
                [item["origin"] for item in subtitle_generations],
                ["legacy", "rendered"],
            )
            self.assertFalse(subtitle_generations[0]["is_published"])
            self.assertTrue(subtitle_generations[1]["is_published"])
            self.assertEqual(rolled_back_srt, "old srt")
            self.assertEqual(
                rolled_back_publication["id"],
                subtitle_generations[0]["id"],
            )
            self.assertEqual(republished_srt, generated_srt)
            self.assertEqual(
                publication_manifest["subtitle_generation_id"],
                subtitle_generations[1]["id"],
            )
            self.assertEqual(manifest_repaired, 1)
            self.assertEqual(
                reconciled_publication["id"],
                subtitle_generations[1]["id"],
            )
            self.assertEqual(pair_repaired, 1)
            self.assertEqual(repaired_srt, generated_srt)
            self.assertIn("새 번역", repaired_ass)
            self.assertTrue(manifest_path.is_file())

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


class _RecordingExecutor:
    """Capture dispatches without running the stage operation."""

    def __init__(self) -> None:
        self.submitted: list[tuple[str, str]] = []

    def submit(self, _run_stage, job_id, stage, _operation):  # noqa: ANN001
        self.submitted.append((stage, job_id))

    def shutdown(self, **_kwargs) -> None:
        return None


class SchedulerDispatchTests(unittest.TestCase):
    def make_orchestrator(
        self,
        root: Path,
        media_root: Path,
        *,
        audio_workers: int = 1,
    ) -> SubtitleOrchestrator:
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
                audio_workers=audio_workers,
            )
        )
        orchestrator._audio_executor = _RecordingExecutor()
        orchestrator._render_executor = _RecordingExecutor()
        orchestrator._stt_executor = _RecordingExecutor()
        orchestrator._translation_executor = _RecordingExecutor()
        return orchestrator

    def test_render_does_not_block_the_next_audio_extraction(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "one.mkv").write_bytes(b"media")
            (media_root / "two.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                ready = orchestrator.create_job(
                    "one.mkv",
                    force_overwrite=False,
                    options={},
                )
                queued = orchestrator.create_job(
                    "two.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(ready.id, status="translated")

                orchestrator._scheduler_tick()

                self.assertEqual(
                    orchestrator._render_executor.submitted,
                    [("render", ready.id)],
                )
                self.assertEqual(
                    orchestrator._audio_executor.submitted,
                    [("audio extraction", queued.id)],
                )
            finally:
                orchestrator.stop()

    def test_extraction_keeps_running_while_a_render_is_in_flight(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "one.mkv").write_bytes(b"media")
            (media_root / "two.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                rendering = orchestrator.create_job(
                    "one.mkv",
                    force_overwrite=False,
                    options={},
                )
                queued = orchestrator.create_job(
                    "two.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(rendering.id, status="rendering")

                orchestrator._scheduler_tick()

                self.assertEqual(orchestrator._render_executor.submitted, [])
                self.assertEqual(
                    orchestrator._audio_executor.submitted,
                    [("audio extraction", queued.id)],
                )
            finally:
                orchestrator.stop()

    def test_audio_workers_bound_the_concurrent_extractions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            for name in ("one.mkv", "two.mkv", "three.mkv"):
                (media_root / name).write_bytes(b"media")
            orchestrator = self.make_orchestrator(
                root,
                media_root,
                audio_workers=2,
            )
            try:
                orchestrator.create_jobs(
                    ["one.mkv", "two.mkv", "three.mkv"],
                    force_overwrite=False,
                    options={},
                )

                orchestrator._scheduler_tick()

                self.assertEqual(
                    len(orchestrator._audio_executor.submitted),
                    2,
                )
            finally:
                orchestrator.stop()
