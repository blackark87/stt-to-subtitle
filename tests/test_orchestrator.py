import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import ANY, Mock, patch

from stt_to_subtitle.backend_config import (
    BackendSettings,
    RemoteServerSettings,
    SubtitleValidatorSettings,
)
from stt_to_subtitle.orchestrator import (
    SubtitleOrchestrator,
    _transcription_model_revision,
    estimate_transcription_chunks,
)
from stt_to_subtitle.job_store import JobStore
from stt_to_subtitle.files import sha256_file
from stt_to_subtitle.service_clients import (
    ExternalServiceError,
    RemoteTranscriptionFailed,
    TranslationDeferred,
    TranslationPaused,
)


def configure_translation_models(
    orchestrator: SubtitleOrchestrator,
) -> SubtitleOrchestrator:
    orchestrator._translation_routing.stores["draft"].save_models(
        "builtin",
        ["draft-model"],
    )
    orchestrator._translation_routing.stores["review"].save_models(
        "builtin",
        ["review-model"],
    )
    orchestrator._refresh_translation_circuit_from_routing()
    return orchestrator


class SubtitleOrchestratorTests(unittest.TestCase):
    def test_selecting_server_model_refreshes_translation_circuit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = SubtitleOrchestrator(
                BackendSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    stt_base_url="http://stt.test",
                    stt_token="",
                    translation_builtin_base_url="http://translation.test/v1",
                )
            )
            try:
                orchestrator._translation_routing.stores["draft"].save_models(
                    "builtin",
                    ["first", "second"],
                )
                self.assertEqual(orchestrator.translation_circuit_state, "offline")

                server = orchestrator.update_translation_server_model(
                    "draft",
                    "builtin",
                    "second",
                )

                self.assertEqual(server["selected_model"], "second")
                self.assertEqual(orchestrator.translation_circuit_state, "ready")
            finally:
                orchestrator.stop()

    def test_compacts_nested_transcription_model_metadata(self) -> None:
        revision = _transcription_model_revision(
            {
                "model": {
                    "id": "whisperjav-domain-ensemble",
                    "revision": "ensemble-revision",
                    "pass1": {
                        "id": "litagin/anime-whisper",
                        "revision": "pass1-revision",
                    },
                    "aligner": {
                        "id": "Qwen/Qwen3-ForcedAligner-0.6B",
                        "revision": "aligner-revision",
                    },
                }
            },
            fallback="whisperjav",
        )

        self.assertEqual(revision, "ensemble-revision")

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
                BackendSettings(
                    state_dir=state_dir,
                    work_dir=work_dir,
                    media_root=media_root,
                    stt_base_url="",
                    stt_token="",
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
        return configure_translation_models(SubtitleOrchestrator(
            BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://stt.test",
                stt_token="stt-token",
                translation_builtin_base_url="http://translation.test/v1",
            )
        ))

    def test_translation_dispatches_without_server_start_action(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = SubtitleOrchestrator(
                BackendSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    stt_base_url="http://stt.test",
                    stt_token="",
                    translation_builtin_base_url="http://translation.test/v1",
                )
            )
            configure_translation_models(orchestrator)
            try:
                job = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(job.id, status="transcribed")
                orchestrator._translation_executor.submit = Mock()

                dispatched = orchestrator._dispatch_translations()
                persisted_circuit = orchestrator.store.get_dependency_state(
                    "translation_lm"
                )
            finally:
                orchestrator.stop()

            self.assertEqual(dispatched, 1)
            self.assertEqual(orchestrator.translation_circuit_state, "ready")
            self.assertEqual(persisted_circuit["state"], "ready")
            orchestrator._translation_executor.submit.assert_called_once()

    def test_shared_host_translation_waits_while_builtin_stt_runs(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = SubtitleOrchestrator(
                BackendSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    stt_base_url="http://stt.test",
                    stt_token="",
                    translation_builtin_base_url=(
                        "http://shared-accelerator.test/v1"
                    ),
                    translation_stt_hard_breaker_hosts=(
                        "shared-accelerator.test",
                    ),
                )
            )
            configure_translation_models(orchestrator)
            try:
                translating = orchestrator.store.create(
                    job_id="translation-waiting-for-stt",
                    source_rel="translation.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    translating.id,
                    status="transcribed",
                )
                transcribing = orchestrator.store.create(
                    job_id="builtin-stt-running",
                    source_rel="transcription.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    transcribing.id,
                    status="transcription_running",
                    stt_runtime_id="builtin",
                )
                orchestrator._translation_executor.submit = Mock()

                dispatched = orchestrator._dispatch_translations()
                server = orchestrator.translation_groups_view()[0][
                    "servers"
                ][0]
            finally:
                orchestrator.stop()

            self.assertEqual(dispatched, 0)
            self.assertEqual(server["status"], "suspended")
            orchestrator._translation_executor.submit.assert_not_called()

    def test_builtin_stt_stage_holds_hard_breaker_around_operation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = SubtitleOrchestrator(
                BackendSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    stt_base_url="http://stt.test",
                    stt_token="",
                    translation_builtin_base_url="http://shared.test/v1",
                    translation_stt_hard_breaker_hosts=("shared.test",),
                )
            )
            configure_translation_models(orchestrator)
            calls: list[str] = []
            try:
                job = orchestrator.store.create(
                    job_id="guarded-transcription",
                    source_rel="transcription.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    job.id,
                    status="transcription_running",
                    stt_runtime_id="builtin",
                )
                orchestrator._translation_routing.engage_stt_hard_breaker = Mock(
                    side_effect=lambda: (
                        calls.append("engage")
                        or {"enabled": True, "unloaded_models": []}
                    )
                )
                orchestrator._translation_routing.release_stt_hard_breaker = Mock(
                    side_effect=lambda: calls.append("release")
                )

                orchestrator._run_stage(
                    job.id,
                    "transcription",
                    lambda _job: calls.append("transcribe"),
                )
            finally:
                orchestrator.stop()

            self.assertEqual(calls, ["engage", "transcribe", "release"])

    def test_translation_hard_breaker_defer_keeps_circuit_ready(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.store.create(
                    job_id="deferred-translation",
                    source_rel="translation.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    job.id,
                    status="translation_running",
                )

                orchestrator._run_stage(
                    job.id,
                    "translation",
                    Mock(side_effect=TranslationDeferred("STT is running")),
                )
                deferred = orchestrator.store.get(job.id)
            finally:
                orchestrator.stop()

            self.assertEqual(deferred.status, "transcribed")
            self.assertEqual(deferred.state, "waiting")
            self.assertEqual(orchestrator.translation_circuit_state, "ready")

    def test_translation_failure_circuit_reopens_on_job_retry(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            settings = BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://stt.test",
                stt_token="",
                translation_builtin_base_url="http://translation.test/v1",
            )
            first = configure_translation_models(SubtitleOrchestrator(settings))
            try:
                job = first.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                transcript = root / "state" / "jobs" / job.id / "transcript.json"
                transcript.parent.mkdir(parents=True, exist_ok=True)
                transcript.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "job_id": job.id,
                            "segments": [
                                {
                                    "id": "segment-000001",
                                    "start": 0,
                                    "end": 1,
                                    "speaker": "SPEAKER_00",
                                    "text": "text",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                first.store.update(
                    job.id,
                    status="blocked",
                    blocked_stage="translation",
                    transcript_path=str(transcript),
                    error="server unavailable",
                )
                first._set_translation_circuit(
                    "lost",
                    reason_code="lm_unavailable",
                    error="server unavailable",
                )
            finally:
                first.stop()

            restarted = SubtitleOrchestrator(settings)
            try:
                restarted._translation_executor.submit = Mock()
                persisted = restarted.store.get_dependency_state(
                    "translation_lm"
                )
                self.assertEqual(restarted.translation_circuit_state, "lost")
                self.assertEqual(persisted["state"], "lost")
                self.assertEqual(restarted._dispatch_translations(), 0)
                restarted.update_remote_servers(
                    RemoteServerSettings(
                        stt_base_url="http://new-stt.test",
                        stt_token="",
                    )
                )
                self.assertEqual(restarted.translation_circuit_state, "lost")

                retried = restarted.retry(job.id)
                dispatched = restarted._dispatch_translations()

                self.assertEqual(retried.status, "transcribed")
                self.assertEqual(
                    restarted.translation_circuit_state,
                    "ready",
                )
                self.assertEqual(dispatched, 1)
                restarted._translation_executor.submit.assert_called_once()
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
                    orchestrator.store.get(running.id).status,
                    "audio_ready",
                )
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
                    return_value={
                        "status": "ready",
                        "queue": {
                            "queued": 4,
                            "running": 1,
                            "cancel_requested": 2,
                        },
                    }
                )
                resumed = orchestrator.activate_transcription_stt()
                self.assertEqual(resumed, 0)
                self.assertEqual(orchestrator.stt_gate_state, "ready")
                orchestrator.stt_client.check_readiness.assert_called_once_with()
                queue_measurements = {
                    measurement["labels"]["state"]: measurement["last_value"]
                    for measurement in (
                        orchestrator.store.operational_measurements()
                    )
                    if measurement["metric"] == "remote_stt.queue.jobs"
                }
                self.assertEqual(
                    queue_measurements,
                    {
                        "queued": 4.0,
                        "running": 1.0,
                        "cancel_requested": 2.0,
                    },
                )
                readiness_measurements = [
                    measurement
                    for measurement in (
                        orchestrator.store.operational_measurements()
                    )
                    if measurement["metric"]
                    == "dependency.readiness_checks"
                ]
                self.assertTrue(
                    any(
                        measurement["labels"]
                        == {"dependency": "stt", "outcome": "ready"}
                        for measurement in readiness_measurements
                    )
                )
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
                validation_measurements = {
                    measurement["labels"]["outcome"]: measurement[
                        "sample_count"
                    ]
                    for measurement in (
                        orchestrator.store.operational_measurements()
                    )
                    if measurement["metric"] == "subtitle.validation.runs"
                    and measurement["labels"]["mode"] == "llm"
                }
            finally:
                orchestrator.stop()

            self.assertFalse(first_cached)
            self.assertTrue(second_cached)
            self.assertEqual(first["llm"], result)
            self.assertEqual(second["llm"], result)
            client.return_value.validate.assert_called_once()
            self.assertEqual(
                validation_measurements,
                {"completed": 1, "cache_hit": 1},
            )

    def test_zero_duration_means_process_to_end(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"not-read-in-this-test")
            orchestrator = configure_translation_models(SubtitleOrchestrator(
                BackendSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    stt_base_url="http://stt.test",
                    stt_token="stt-token",
                    translation_builtin_base_url="http://translation.test/v1",
                )
            ))
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
                    )
                )

                self.assertIsNot(original.stt_client, original_stt_client)
                self.assertEqual(
                    original.stt_client.base_url,
                    "http://new-stt.test",
                )
                self.assertEqual(saved.stt_base_url, "http://new-stt.test")
            finally:
                original.stop()

            reloaded = self.make_orchestrator(root, media_root)
            try:
                self.assertEqual(
                    reloaded.stt_client.base_url,
                    "http://new-stt.test",
                )
                self.assertEqual(reloaded.stt_client.token, "new-stt-token")
            finally:
                reloaded.stop()

    def test_migrates_legacy_all_in_one_runtime_service_url(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            store = JobStore(root / "state" / "jobs.sqlite3")
            store.save_remote_server_settings(
                stt_base_url="http://stt:8100",
                stt_token="",
            )

            orchestrator = SubtitleOrchestrator(
                BackendSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    stt_base_url="http://runtime:8100",
                    stt_token="",
                )
            )
            try:
                self.assertEqual(
                    orchestrator.stt_client.base_url,
                    "http://runtime:8100",
                )
                self.assertEqual(
                    orchestrator.store.get_remote_server_settings()[
                        "stt_base_url"
                    ],
                    "http://runtime:8100",
                )
            finally:
                orchestrator.stop()

    def test_dispatches_transcriptions_across_builtin_and_external_runtimes(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                endpoint = orchestrator.store.create_runtime_endpoint(
                    name="GPU Runtime 02",
                    base_url="http://runtime-02.test:8100",
                    token="token-02",
                    enabled=True,
                    capacity=1,
                )
                orchestrator._install_runtime_endpoint(endpoint)
                orchestrator._set_runtime_health(
                    endpoint.id,
                    "ready",
                    readiness={"status": "ready", "queue": {}},
                )
                jobs = [
                    orchestrator.store.create(
                        job_id=f"pooled-{index}",
                        source_rel=f"movie-{index}.mkv",
                        force_overwrite=False,
                        options={},
                        status="audio_ready",
                    )
                    for index in range(2)
                ]
                orchestrator._stt_executor.submit = Mock()

                dispatched = orchestrator._dispatch_transcriptions()
                assignments = {
                    orchestrator.store.get(job.id).stt_runtime_id
                    for job in jobs
                }

                self.assertEqual(dispatched, 2)
                self.assertEqual(
                    assignments,
                    {"builtin", endpoint.id},
                )
                self.assertEqual(
                    orchestrator._stt_executor.submit.call_count,
                    2,
                )
            finally:
                orchestrator.stop()

    def test_requeues_external_runtime_disconnect_to_available_runtime(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                endpoint = orchestrator.store.create_runtime_endpoint(
                    name="GPU Runtime 02",
                    base_url="http://runtime-02.test:8100",
                    token="token-02",
                    enabled=True,
                    capacity=1,
                )
                orchestrator._install_runtime_endpoint(endpoint)
                orchestrator._set_runtime_health(
                    endpoint.id,
                    "ready",
                    readiness={"status": "ready", "queue": {}},
                )
                job = orchestrator.store.create(
                    job_id="runtime-failover",
                    source_rel="movie.mkv",
                    force_overwrite=False,
                    options={},
                    status="audio_ready",
                )
                orchestrator.store.update(
                    job.id,
                    status="transcription_running",
                    stt_runtime_id=endpoint.id,
                    stt_job_id="remote-job-02",
                    chunks_created=20,
                    chunks_completed=10,
                    transcription_stage="primary_transcription",
                    transcription_stage_index=2,
                    transcription_stage_total=7,
                )

                orchestrator._run_stage(
                    job.id,
                    "transcription",
                    Mock(side_effect=ExternalServiceError("connection refused")),
                )
                requeued = orchestrator.store.get(job.id)

                self.assertEqual(requeued.status, "audio_ready")
                self.assertEqual(requeued.state, "waiting")
                self.assertEqual(requeued.attempt, 2)
                self.assertIsNone(requeued.stt_runtime_id)
                self.assertIsNone(requeued.stt_job_id)
                self.assertEqual(requeued.chunks_created, 0)
                self.assertEqual(requeued.chunks_completed, 0)
                self.assertIsNone(requeued.transcription_stage)
                self.assertEqual(orchestrator.stt_gate_state, "ready")
                self.assertEqual(
                    orchestrator._runtime_view(endpoint.id)["status"],
                    "unavailable",
                )
                failover_event = next(
                    event
                    for event in orchestrator.store.events(job.id)
                    if event["event_code"]
                    == "transcription.runtime_failover"
                )
                self.assertEqual(failover_event["from_state"], "running")
                self.assertEqual(failover_event["to_state"], "waiting")
                self.assertEqual(
                    failover_event["payload"]["failed_runtime_id"],
                    endpoint.id,
                )

                orchestrator._stt_executor.submit = Mock()
                self.assertEqual(orchestrator._dispatch_transcriptions(), 1)
                reassigned = orchestrator.store.get(job.id)
                self.assertEqual(reassigned.status, "transcription_running")
                self.assertEqual(reassigned.stt_runtime_id, "builtin")
            finally:
                orchestrator.stop()

    def test_periodically_reprobes_unavailable_external_runtime(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                endpoint = orchestrator.store.create_runtime_endpoint(
                    name="GPU Runtime 02",
                    base_url="http://runtime-02.test:8100",
                    token="token-02",
                    enabled=True,
                    capacity=1,
                )
                orchestrator._install_runtime_endpoint(endpoint)
                orchestrator._set_runtime_health(
                    endpoint.id,
                    "unavailable",
                    message="connection refused",
                )
                orchestrator._runtime_health[endpoint.id]["checked_at"] = (
                    time.time() - 31
                )
                orchestrator._runtime_probe_executor.submit = Mock()

                orchestrator._schedule_runtime_reprobes()

                self.assertEqual(
                    orchestrator._runtime_view(endpoint.id)["status"],
                    "checking",
                )
                orchestrator._runtime_probe_executor.submit.assert_called_once_with(
                    orchestrator.probe_runtime_endpoint,
                    endpoint.id,
                )
            finally:
                orchestrator.stop()

    def test_cancels_transcription_on_its_assigned_runtime(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                endpoint = orchestrator.store.create_runtime_endpoint(
                    name="GPU Runtime 02",
                    base_url="http://runtime-02.test:8100",
                    token="token-02",
                    enabled=True,
                    capacity=1,
                )
                external_client = Mock()
                orchestrator._runtime_clients[endpoint.id] = external_client
                orchestrator.stt_client.cancel_job = Mock()
                job = orchestrator.store.create(
                    job_id="external-running",
                    source_rel="movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    job.id,
                    status="transcription_running",
                    stt_job_id="remote-job-02",
                    stt_runtime_id=endpoint.id,
                )

                stopped = orchestrator.stop_jobs([job.id])

                self.assertEqual(stopped, 1)
                external_client.cancel_job.assert_called_once_with(
                    "remote-job-02"
                )
                orchestrator.stt_client.cancel_job.assert_not_called()
            finally:
                orchestrator.stop()

    def test_translation_executor_reserves_one_file_at_a_time(
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
                orchestrator._translation_routing.stores["draft"].update(
                    "builtin",
                    name="기본 서버",
                    base_url="http://translation.test/v1",
                    token="",
                    enabled=True,
                    capacity=3,
                )
                orchestrator.update_remote_servers(
                    RemoteServerSettings(
                        stt_base_url="http://stt.test",
                        stt_token="",
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

    def test_superseded_worker_cannot_persist_a_stage_result(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.store.create(
                    job_id="lease-fenced",
                    source_rel="movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                lease_token = orchestrator.store.claim_for_dispatch(
                    job.id,
                    "queued",
                    "extracting",
                    lease_owner=orchestrator._worker_id,
                    lease_seconds=60,
                )
                replacement_token: list[int] = []

                def supersede(stage_job):
                    orchestrator.store.update(
                        job.id,
                        lease_expires_at=time.time() - 1,
                    )
                    claimed = orchestrator.store.claim_recovery_lease(
                        job.id,
                        "extracting",
                        lease_owner="replacement-worker",
                        lease_seconds=60,
                    )
                    self.assertIsNotNone(claimed)
                    replacement_token.append(claimed)
                    orchestrator._require_stage_update(
                        stage_job,
                        status="audio_ready",
                    )

                orchestrator._run_stage(
                    job.id,
                    "audio extraction",
                    supersede,
                    lease_token,
                )
                fenced = orchestrator.store.get(job.id)
                fencing_measurements = [
                    measurement
                    for measurement in (
                        orchestrator.store.operational_measurements()
                    )
                    if measurement["metric"]
                    == "lease.fencing_rejections"
                ]
            finally:
                orchestrator.stop()

            self.assertEqual(replacement_token, [lease_token + 1])
            self.assertEqual(fenced.status, "extracting")
            self.assertEqual(fenced.lease_owner, "replacement-worker")
            self.assertEqual(fenced.lease_token, replacement_token[0])
            self.assertEqual(len(fencing_measurements), 1)
            self.assertEqual(
                fencing_measurements[0]["labels"],
                {
                    "detection": "worker_result",
                    "stage": "audio extraction",
                },
            )

    def test_shutdown_drains_an_active_stage_within_grace_period(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            release = threading.Event()
            started = threading.Event()
            job = orchestrator.store.create(
                job_id="shutdown-drain",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )
            lease_token = orchestrator.store.claim_for_dispatch(
                job.id,
                "queued",
                "extracting",
                lease_owner=orchestrator._worker_id,
                lease_seconds=60,
            )

            def complete_after_release(stage_job):
                started.set()
                self.assertTrue(release.wait(timeout=1))
                orchestrator._require_stage_update(
                    stage_job,
                    status="audio_ready",
                )

            orchestrator._submit_stage(
                orchestrator._audio_executor,
                job.id,
                "audio extraction",
                complete_after_release,
                lease_token,
            )
            self.assertTrue(started.wait(timeout=1))
            timer = threading.Timer(0.05, release.set)
            timer.start()
            try:
                orchestrator.stop(grace_seconds=1)
                completed = orchestrator.store.get(job.id)
            finally:
                release.set()
                timer.cancel()

            self.assertEqual(completed.status, "audio_ready")
            self.assertIsNone(completed.lease_owner)

    def test_requires_web_server_settings_before_creating_job(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = SubtitleOrchestrator(
                BackendSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    stt_base_url="",
                    stt_token="",
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
                audio_revision = orchestrator.store.get_audio_revision(
                    completed.audio_revision_id
                )
                transcript_revisions = (
                    orchestrator.store.transcript_revisions(job.id)
                )
            finally:
                orchestrator.stop()

            self.assertEqual(extracted.status, "audio_ready")
            self.assertEqual(extracted.chunks_total_estimate, 3)
            self.assertEqual(completed.status, "transcription_completed")
            self.assertEqual(completed.operation, "transcribe")
            self.assertTrue(Path(completed.audio_path).is_file())
            self.assertTrue(Path(completed.transcript_path).is_file())
            self.assertIsNotNone(audio_revision)
            self.assertEqual(
                audio_revision["artifact_path"],
                completed.audio_path,
            )
            self.assertEqual(len(transcript_revisions), 1)
            self.assertEqual(
                transcript_revisions[0]["id"],
                completed.transcript_revision_id,
            )
            self.assertEqual(
                transcript_revisions[0]["audio_revision_id"],
                completed.audio_revision_id,
            )
            self.assertIsNone(completed.translation_path)
            self.assertFalse(completed.can_pause_translation)

    def test_audio_extraction_reuses_a_valid_immutable_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                first = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={},
                    operation="extract",
                )
                extract = Mock()

                def fake_extract(_source, target, _options):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(b"wave")

                extract.side_effect = fake_extract
                with patch(
                    "stt_to_subtitle.orchestrator.extract_audio",
                    extract,
                ), patch(
                    "stt_to_subtitle.orchestrator.wav_duration_seconds",
                    return_value=30.0,
                ):
                    orchestrator._extract(first)
                    first_completed = orchestrator.store.get(first.id)
                    second = orchestrator.store.create(
                        job_id="second-extraction",
                        source_rel="movie.mkv",
                        force_overwrite=False,
                        options=first.options,
                        operation="extract",
                    )
                    orchestrator._extract(second)
                    second_completed = orchestrator.store.get(second.id)
            finally:
                orchestrator.stop()

            extract.assert_called_once()
            self.assertEqual(
                second_completed.audio_revision_id,
                first_completed.audio_revision_id,
            )
            self.assertEqual(
                second_completed.audio_path,
                first_completed.audio_path,
            )

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

    def test_selected_translation_target_stage_controls_review_pass(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            orchestrator.update_translation_endpoint_routing(
                "review",
                "builtin",
                {"enabled": True, "batch_preferred": False},
            )
            jobs = {}
            try:
                for target_stage in ("draft", "review"):
                    source_rel = f"{target_stage}.mkv"
                    (media_root / source_rel).write_bytes(b"media")
                    job = orchestrator.store.create(
                        job_id=f"{target_stage}-job",
                        source_rel=source_rel,
                        force_overwrite=False,
                        options={},
                        operation="transcribe",
                    )
                    transcript_path = (
                        root / "state" / "jobs" / job.id / "transcript.json"
                    )
                    transcript_path.parent.mkdir(parents=True)
                    transcript_path.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "job_id": f"remote-{target_stage}",
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
                        status="transcription_completed",
                        transcript_path=str(transcript_path),
                    )
                    jobs[target_stage] = job

                draft = orchestrator.create_selected_translation_jobs(
                    [jobs["draft"].id],
                    prompt_category_id="jav",
                    target_stage="draft",
                )[0]
                review = orchestrator.create_selected_translation_jobs(
                    [jobs["review"].id],
                    prompt_category_id="variety",
                    target_stage="review",
                )[0]
                with self.assertRaisesRegex(ValueError, "지원하지 않는"):
                    orchestrator.create_selected_translation_jobs(
                        [],
                        prompt_category_id="jav",
                        target_stage="unknown",
                    )
            finally:
                orchestrator.stop()

            draft_prompt = draft.options["translation_prompt"]
            review_prompt = review.options["translation_prompt"]
            self.assertEqual(draft.status, "transcribed")
            self.assertEqual(draft_prompt["target_stage"], "draft")
            self.assertEqual(draft_prompt["review_rounds"], 0)
            self.assertEqual(draft_prompt["translation_mode"], "draft_only")
            self.assertEqual(review.status, "transcribed")
            self.assertEqual(review_prompt["target_stage"], "review")
            self.assertEqual(review_prompt["review_rounds"], 1)
            self.assertEqual(
                review_prompt["translation_mode"],
                "draft_and_review",
            )

    def test_selected_second_pass_reuses_completed_first_pass_translation(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            orchestrator.update_translation_endpoint_routing(
                "review",
                "builtin",
                {"enabled": True, "batch_preferred": False},
            )
            job = orchestrator.create_job(
                "movie.mkv",
                force_overwrite=False,
                options={},
            )
            artifact_dir = root / "state" / "jobs" / job.id
            artifact_dir.mkdir(parents=True)
            transcript_path = artifact_dir / "transcript.json"
            translation_path = artifact_dir / "translation.json"
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
                                "text": "기존 1차 번역",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            orchestrator.store.update(
                job.id,
                status="completed",
                operation="full",
                transcript_path=str(transcript_path),
                translation_path=str(translation_path),
            )

            translation_client = Mock()
            translation_client.translate = Mock(
                return_value=[
                    {"id": "segment-000001", "text": "교정된 번역"}
                ]
            )
            orchestrator._make_translation_client = Mock(
                return_value=translation_client
            )
            try:
                queued = orchestrator.create_selected_translation_jobs(
                    [job.id],
                    prompt_category_id="jav",
                    translation_mode="review_existing",
                )[0]
                orchestrator.store.update(
                    queued.id,
                    status="translation_running",
                )
                running = orchestrator.store.get(queued.id)
                orchestrator._translate(running)
                generations = orchestrator.store.list_translation_generations(
                    job.id
                )
            finally:
                orchestrator.stop()

            call = translation_client.translate.call_args.kwargs
            self.assertFalse(call["draft_pass"])
            self.assertEqual(call["review_rounds"], 1)
            self.assertEqual(
                call["draft_translations"],
                {"segment-000001": "기존 1차 번역"},
            )
            self.assertEqual(call["existing"], {})
            self.assertEqual(
                queued.options["translation_prompt"]["translation_mode"],
                "review_existing",
            )
            self.assertEqual(len(generations), 2)
            self.assertEqual(
                [generation["state"] for generation in generations],
                ["completed", "completed"],
            )

    def test_selects_a_historical_prompt_revision_for_a_new_job(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                original = orchestrator.store.get_prompt_category("variety")
                updated = orchestrator.store.update_prompt_category(
                    "variety",
                    name=original.name,
                    translation_prompt="current translation prompt",
                    review_prompt="current review prompt",
                )
                revisions = orchestrator.store.list_prompt_revisions("variety")
                historical = revisions[0]
                selection = f"variety@{historical['id']}"
                choices = orchestrator.prompt_revision_choices()

                job = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=False,
                    options={},
                    operation="full",
                    prompt_category_id=selection,
                )

                snapshot = job.options["translation_prompt"]
                self.assertEqual(updated.prompt_revision_number, 2)
                self.assertEqual(snapshot["revision_id"], historical["id"])
                self.assertEqual(snapshot["revision_number"], 1)
                self.assertEqual(
                    snapshot["translation_prompt"],
                    historical["translation_prompt"],
                )
                self.assertIn(
                    selection,
                    [choice["id"] for choice in choices],
                )
                self.assertIn("variety", [choice["id"] for choice in choices])
                with self.assertRaisesRegex(ValueError, "사용할 수 있는"):
                    orchestrator._prompt_snapshot(f"jav@{historical['id']}")
            finally:
                orchestrator.stop()

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

    def test_translation_uses_group_capacity_for_one_file(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                orchestrator._translation_routing.stores["draft"].update(
                    "builtin",
                    name="기본 서버",
                    base_url="http://translation.test/v1",
                    token="",
                    enabled=True,
                    capacity=3,
                )
                orchestrator.update_remote_servers(
                    RemoteServerSettings(
                        stt_base_url="http://stt.test",
                        stt_token="",
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
                orchestrator.store.record_audio_revision(
                    revision_id="audio-1",
                    job_id=job.id,
                    source_rel="movie.mkv",
                    source_hash="source-hash",
                    extraction_hash="extraction-hash",
                    artifact_path=str(artifact_dir / "audio.wav"),
                    content_hash="audio-hash",
                    duration_seconds=16 * 60,
                    status="audio_ready",
                    chunks_total_estimate=1,
                )
                orchestrator.store.update(
                    job.id,
                    status="translation_running",
                    transcript_path=str(transcript_path),
                )
                observer_holder = {}
                translation_client = Mock()

                def translate_with_metrics(*_args, **_kwargs):
                    observer = observer_holder["observer"]
                    observer(
                        {
                            "service": "translation_lm",
                            "operation": "translation",
                            "outcome": "success",
                            "attempt": 1,
                            "elapsed_seconds": 2.0,
                        }
                    )
                    observer(
                        {
                            "service": "translation_lm",
                            "operation": "review",
                            "outcome": "success",
                            "attempt": 1,
                            "elapsed_seconds": 3.0,
                        }
                    )
                    return [
                        {"id": "segment-000001", "text": "안녕하세요"}
                    ]

                translation_client.translate = Mock(
                    side_effect=translate_with_metrics
                )

                def make_translation_client(*, request_observer):
                    observer_holder["observer"] = request_observer
                    return translation_client

                orchestrator._make_translation_client = Mock(
                    side_effect=make_translation_client
                )

                orchestrator._translate(orchestrator.store.get(job.id))
                pass_measurements = [
                    measurement
                    for measurement in (
                        orchestrator.store.operational_measurements()
                    )
                    if measurement["metric"]
                    == "translation.pass.active_seconds"
                ]
                pass_events = [
                    event
                    for event in orchestrator.store.events(job.id)
                    if event["event_code"] == "translation.pass.measured"
                ]
            finally:
                orchestrator.stop()

            self.assertEqual(
                translation_client.translate.call_args.kwargs["max_workers"],
                3,
            )
            self.assertEqual(len(pass_measurements), 2)
            self.assertEqual(
                {
                    measurement["labels"]["pass"]: measurement["labels"]
                    for measurement in pass_measurements
                },
                {
                    "draft": {
                        "media_duration_bucket_minutes": 15,
                        "outcome": "completed",
                        "pass": "draft",
                    },
                    "review": {
                        "media_duration_bucket_minutes": 15,
                        "outcome": "completed",
                        "pass": "review",
                    },
                },
            )
            self.assertEqual(len(pass_events), 2)
            self.assertEqual(
                {event["payload"]["pass"] for event in pass_events},
                {"draft", "review"},
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
                waiting = orchestrator.store.create(
                    job_id="waiting-translation",
                    source_rel="waiting.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(waiting.id, status="transcribed")
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
                orchestrator._translation_executor.submit = Mock()

                orchestrator._run_stage(
                    job.id,
                    "translation",
                    orchestrator._translate,
                )
                orchestrator._scheduler_tick()
                blocked = orchestrator.store.get(job.id)
                waiting = orchestrator.store.get(waiting.id)
                generation = (
                    orchestrator.store.latest_translation_generation(job.id)
                )
                batches = orchestrator.store.translation_batches(
                    generation["id"]
                )
            finally:
                orchestrator.stop()

            self.assertEqual(blocked.status, "blocked")
            self.assertEqual(orchestrator.translation_circuit_state, "lost")
            self.assertEqual(waiting.status, "transcribed")
            orchestrator._translation_executor.submit.assert_not_called()
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
                checkpoint_measurements = [
                    measurement
                    for measurement in (
                        orchestrator.store.operational_measurements()
                    )
                    if measurement["metric"]
                    == "translation.checkpoint.items"
                ]
            finally:
                orchestrator.stop()

            self.assertEqual(snapshot["status"], "completed")
            self.assertEqual(snapshot["translations"][0]["text"], "DB 번역")
            self.assertEqual(stored_generation["state"], "completed")
            self.assertEqual(stored_generation["attempt"], 1)
            self.assertEqual(len(checkpoint_measurements), 1)
            self.assertEqual(
                checkpoint_measurements[0]["labels"],
                {"outcome": "reused", "source": "generation_store"},
            )
            self.assertEqual(checkpoint_measurements[0]["last_value"], 1.0)

    def test_translation_measures_legacy_checkpoint_reuse_and_invalidation(
        self,
    ) -> None:
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
            transcript_path = artifact_dir / "transcript.json"
            translation_path = artifact_dir / "translation.json"
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
                        "status": "partial",
                        "translations": [
                            {"id": "segment-000001", "text": "안녕하세요"},
                            {"id": "stale-segment", "text": "이전 값"},
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
                translation_path=str(translation_path),
            )
            running = orchestrator.store.get(job.id)
            translation_client = Mock()
            translation_client.translate = Mock(
                return_value=[
                    {"id": "segment-000001", "text": "안녕하세요"}
                ]
            )
            orchestrator._make_translation_client = Mock(
                return_value=translation_client
            )
            try:
                orchestrator._translate(running)
                measurements = {
                    (
                        measurement["labels"]["source"],
                        measurement["labels"]["outcome"],
                    ): measurement["last_value"]
                    for measurement in (
                        orchestrator.store.operational_measurements()
                    )
                    if measurement["metric"]
                    == "translation.checkpoint.items"
                }
            finally:
                orchestrator.stop()

            self.assertEqual(
                translation_client.translate.call_args.kwargs["existing"],
                {"segment-000001": "안녕하세요"},
            )
            self.assertEqual(
                translation_client.translate.call_args.kwargs["review_rounds"],
                0,
            )
            self.assertEqual(
                measurements,
                {
                    ("legacy_json", "reused"): 1.0,
                    ("legacy_json", "invalidated"): 1.0,
                },
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
                self.assertIsNone(
                    orchestrator.store.get(translating.id).lease_owner
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
                    ANY,
                )
                orchestrator._stt_executor.submit.assert_any_call(
                    orchestrator._run_stage,
                    remote_stopping.id,
                    "transcription",
                    orchestrator._cancel_interrupted_transcription,
                    ANY,
                )
            finally:
                orchestrator.stop()

    def test_start_reconciles_translation_ledger_before_scheduler(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            transcript_path = root / "transcript.json"
            transcript_path.write_text(
                json.dumps(
                    {
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
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.store.create(
                    job_id="restart-translation-ledger",
                    source_rel="movie.mkv",
                    force_overwrite=False,
                    options={},
                )
                orchestrator.store.update(
                    job.id,
                    status="translation_running",
                    transcript_path=str(transcript_path),
                )
                generation = orchestrator.store.create_translation_generation(
                    generation_id="generation-restart",
                    job_id=job.id,
                    transcript_job_id="remote-job",
                    transcript_hash="transcript-hash",
                    prompt_hash="prompt-hash",
                    endpoint_key="http://lm.test/v1",
                    model="model",
                    config_hash="config-hash",
                    artifact_path="generation.json",
                    origin="automatic",
                )
                attempt = (
                    orchestrator.store.begin_translation_generation_attempt(
                        generation["id"]
                    )
                )
                orchestrator.store.start_translation_batch(
                    generation["id"],
                    batch_index=0,
                    generation_attempt=attempt,
                    items=[
                        {
                            "id": "segment-000001",
                            "source_hash": "source-1",
                        }
                    ],
                )
                orchestrator._scheduler.start = Mock()

                orchestrator.start()

                recovered_job = orchestrator.store.get(job.id)
                recovered_generation = (
                    orchestrator.store.get_translation_generation(
                        generation["id"]
                    )
                )
                self.assertEqual(recovered_job.status, "transcribed")
                self.assertIsNone(recovered_job.lease_owner)
                self.assertEqual(
                    recovered_generation["state"],
                    "interrupted",
                )
                self.assertEqual(
                    orchestrator.store.translation_batches(
                        generation["id"]
                    )[0]["state"],
                    "interrupted",
                )
                orchestrator._scheduler.start.assert_called_once_with()
            finally:
                orchestrator.stop()

    def test_scheduler_recovers_job_whose_previous_worker_lease_expires(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            audio_path = root / "audio.wav"
            audio_path.write_bytes(b"wav")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.store.create(
                    job_id="expired-runtime-worker",
                    source_rel="movie.mkv",
                    force_overwrite=False,
                    options={},
                    status="audio_ready",
                )
                orchestrator.store.update(job.id, audio_path=str(audio_path))
                orchestrator.store.claim_for_dispatch(
                    job.id,
                    "audio_ready",
                    "transcription_running",
                    lease_owner="stopped-backend",
                    lease_seconds=60,
                    stt_runtime_id="builtin",
                )
                orchestrator.store.update(
                    job.id,
                    stt_job_id="remote-job",
                    lease_expires_at=time.time() - 1,
                )
                orchestrator._stt_executor.submit = Mock()

                orchestrator._scheduler_tick()

                recovered = orchestrator.store.get(job.id)
                self.assertEqual(recovered.status, "transcription_running")
                self.assertEqual(recovered.lease_owner, orchestrator._worker_id)
                orchestrator._stt_executor.submit.assert_called_once_with(
                    orchestrator._run_stage,
                    job.id,
                    "transcription",
                    orchestrator._transcribe,
                    ANY,
                )
                self.assertTrue(
                    any(
                        event["event_code"] == "transcription.reconnected"
                        for event in orchestrator.store.events(job.id)
                    )
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

    def test_retry_clears_terminal_remote_transcription_id(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                failed = orchestrator.store.create(
                    job_id="failed-transcription",
                    source_rel="failed.mkv",
                    force_overwrite=False,
                    options={},
                    operation="transcribe",
                )
                audio_path = (
                    root / "state" / "jobs" / failed.id / "audio.wav"
                )
                audio_path.parent.mkdir(parents=True)
                audio_path.write_bytes(b"audio")
                orchestrator.store.update(
                    failed.id,
                    status="failed",
                    blocked_stage="transcription",
                    audio_path=str(audio_path),
                    stt_job_id="terminal-remote-job",
                    error="worker failed",
                )

                retried = orchestrator.retry(failed.id)
            finally:
                orchestrator.stop()

            self.assertEqual(retried.status, "audio_ready")
            self.assertIsNone(retried.stt_job_id)

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
            self.assertTrue(
                all(
                    job.options["translation_execution_mode"] == "batch"
                    for job in jobs
                )
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
                BackendSettings(
                    state_dir=root / "state",
                    media_root=media_root,
                    stt_base_url="http://stt.test",
                    stt_token="stt-token",
                    translation_builtin_base_url="http://lm.test/v1",
                    translation_builtin_token="lm-token",
                )
            )
            configure_translation_models(orchestrator)
            try:
                orchestrator.update_runtime_endpoint(
                    "builtin",
                    name="기본 Runtime",
                    base_url="http://stt.test",
                    token=None,
                    clear_token=False,
                    enabled=True,
                    capacity=1,
                    kotoba_batch_size=4,
                    whisperx_batch_size=24,
                )
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
                            "stage": "primary_transcription",
                            "stage_index": 1,
                            "stage_total": 7,
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
                progress_events = orchestrator.store.events(job.id)
                progress_messages = [
                    event["message"]
                    for event in progress_events
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
            stage_events = [
                event
                for event in progress_events
                if event["event_code"] == "transcription.stage_changed"
            ]
            self.assertEqual(len(stage_events), 1)
            self.assertEqual(
                stage_events[0]["payload"],
                {
                    "runtime_id": "builtin",
                    "stage": "primary_transcription",
                    "stage_index": 1,
                    "stage_total": 7,
                },
            )
            sent_options = (
                orchestrator.stt_client.transcribe.call_args.kwargs["options"]
            )
            self.assertEqual(sent_options["chunk_length_seconds"], 15)
            self.assertTrue(sent_options["noise_filter"])
            self.assertEqual(sent_options["backend"], "hybrid")
            self.assertEqual(sent_options["batch_size"], 24)
            self.assertEqual(sent_options["kotoba_batch_size"], 4)
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
            self.assertEqual(completed_job.chunks_created, 1)
            self.assertEqual(completed_job.chunks_completed, 1)
            self.assertEqual(completed_job.chunks_total_estimate, 1)
            self.assertEqual(
                completed_job.transcription_stage,
                "primary_transcription",
            )
            self.assertEqual(completed_job.transcription_stage_index, 1)
            self.assertEqual(completed_job.transcription_stage_total, 7)
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

    def test_editing_transcript_creates_an_immutable_revision(self) -> None:
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
                legacy_path = root / "state" / "jobs" / job.id / "legacy.json"
                legacy_path.parent.mkdir(parents=True)
                original = {
                    "schema_version": 1,
                    "job_id": "remote-job",
                    "segments": [
                        {
                            "id": "segment-000001",
                            "start": 0,
                            "end": 1,
                            "speaker": "SPEAKER_00",
                            "text": "원본",
                        }
                    ],
                }
                legacy_path.write_text(
                    json.dumps(original, ensure_ascii=False),
                    encoding="utf-8",
                )
                orchestrator.store.update(
                    job.id,
                    status="transcription_completed",
                    transcript_path=str(legacy_path),
                )
                edited_payload = {
                    **original,
                    "segments": [
                        {
                            **original["segments"][0],
                            "text": "수정본",
                        }
                    ],
                }

                edited_path = orchestrator.save_artifact(
                    job.id,
                    "transcript",
                    json.dumps(edited_payload, ensure_ascii=False),
                )
                refreshed = orchestrator.store.get(job.id)
                revisions = orchestrator.store.transcript_revisions(job.id)
            finally:
                orchestrator.stop()

            self.assertNotEqual(edited_path, legacy_path)
            self.assertIn(
                "원본",
                legacy_path.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "수정본",
                edited_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(refreshed.transcript_path, str(edited_path))
            self.assertEqual(refreshed.transcript_revision_id, revisions[0]["id"])
            self.assertEqual(revisions[0]["origin"], "manual")

    def test_restart_translation_preserves_transcript_and_rerenders(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "movie.mkv"
            source.write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            orchestrator.update_translation_endpoint_routing(
                "review",
                "builtin",
                {"enabled": True, "batch_preferred": False},
            )
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
                1,
            )
            self.assertIn(
                "JAPANESE VARIETY AND TALK-SHOW POLICY",
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

    def test_restart_translation_can_select_a_historical_transcript_revision(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                orchestrator.update_translation_endpoint_routing(
                    "review",
                    "builtin",
                    {"enabled": True, "batch_preferred": False},
                )
                orchestrator.stop()
                job = orchestrator.create_job(
                    "movie.mkv",
                    force_overwrite=True,
                    options={},
                )
                revision_root = (
                    orchestrator.settings.jobs_dir
                    / job.id
                    / "transcript-revisions"
                )
                revision_root.mkdir(parents=True)

                def write_revision(revision_id: str, text: str) -> Path:
                    path = revision_root / revision_id / "transcript.json"
                    path.parent.mkdir()
                    path.write_text(
                        json.dumps(
                            {
                                "schema_version": 1,
                                "job_id": f"remote-{revision_id}",
                                "segments": [
                                    {
                                        "id": "segment-000001",
                                        "start": 0,
                                        "end": 1,
                                        "speaker": "SPEAKER_00",
                                        "text": text,
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    return path

                first_path = write_revision("revision-1", "첫 전사")
                second_path = write_revision("revision-2", "둘째 전사")
                for revision_id, path in (
                    ("revision-1", first_path),
                    ("revision-2", second_path),
                ):
                    orchestrator.store.record_transcript_revision(
                        revision_id=revision_id,
                        job_id=job.id,
                        audio_revision_id=None,
                        remote_job_id=f"remote-{revision_id}",
                        backend="whisperx",
                        model_revision="model-v1",
                        options_hash="options-hash",
                        artifact_path=str(path),
                        content_hash=sha256_file(path),
                        origin="automatic",
                        status=None,
                        chunks_total=1,
                    )
                orchestrator.store.update(job.id, status="completed")
                original_first = first_path.read_bytes()
                first_path.write_bytes(b"tampered")

                with self.assertRaisesRegex(ValueError, "무결성"):
                    orchestrator.restart_translation(
                        job.id,
                        "jav",
                        transcript_revision_id="revision-1",
                    )
                self.assertEqual(
                    orchestrator.store.list_translation_generations(job.id),
                    [],
                )

                first_path.write_bytes(original_first)
                restarted = orchestrator.restart_translation(
                    job.id,
                    "jav",
                    transcript_revision_id="revision-1",
                )
                generation = orchestrator.store.latest_translation_generation(
                    job.id
                )
            finally:
                orchestrator.stop()

            self.assertEqual(restarted.status, "transcribed")
            self.assertEqual(restarted.transcript_revision_id, "revision-1")
            self.assertEqual(restarted.transcript_path, str(first_path))
            self.assertEqual(restarted.stt_job_id, "remote-revision-1")
            self.assertEqual(restarted.chunks_created, 1)
            self.assertEqual(generation["transcript_revision_id"], "revision-1")
            self.assertEqual(
                generation["transcript_hash"],
                sha256_file(first_path),
            )
            self.assertIn(
                "첫 전사",
                Path(restarted.transcript_path).read_text(encoding="utf-8"),
            )

    def test_recovers_subtitle_publication_at_each_file_cutpoint(self) -> None:
        for cutpoint in (
            "generation_recorded",
            "srt_replaced",
            "pair_replaced",
            "manifest_written",
        ):
            with self.subTest(cutpoint=cutpoint), TemporaryDirectory() as directory:
                root = Path(directory)
                media_root = root / "media"
                media_root.mkdir()
                source = media_root / "movie.mkv"
                source.write_bytes(b"media")
                orchestrator = self.make_orchestrator(root, media_root)
                try:
                    job = orchestrator.store.create(
                        job_id=f"render-{cutpoint}",
                        source_rel="movie.mkv",
                        force_overwrite=True,
                        options={},
                        status="translated",
                    )
                    orchestrator.store.update(job.id, status="rendering")
                    generation_id = f"subtitle-{cutpoint}"
                    srt_artifact, ass_artifact = (
                        orchestrator._subtitle_generation_artifact_paths(
                            job.id,
                            generation_id,
                        )
                    )
                    srt_artifact.parent.mkdir(parents=True)
                    srt_artifact.write_text(
                        f"new srt {cutpoint}",
                        encoding="utf-8",
                    )
                    ass_artifact.write_text(
                        f"new ass {cutpoint}",
                        encoding="utf-8",
                    )
                    generation = (
                        orchestrator.store.create_subtitle_generation(
                            generation_id=generation_id,
                            job_id=job.id,
                            translation_generation_id=None,
                            transcript_hash="transcript-hash",
                            translation_hash="translation-hash",
                            renderer_version="1",
                            render_hash=f"render-{cutpoint}",
                            srt_artifact_path=str(srt_artifact),
                            ass_artifact_path=str(ass_artifact),
                            srt_hash=sha256_file(srt_artifact),
                            ass_hash=sha256_file(ass_artifact),
                            origin="rendered",
                        )
                    )
                    srt_path = media_root / "movie.ko.srt"
                    ass_path = media_root / "movie.ko.ass"
                    if cutpoint in {
                        "srt_replaced",
                        "pair_replaced",
                        "manifest_written",
                    }:
                        srt_path.write_bytes(srt_artifact.read_bytes())
                    if cutpoint == "srt_replaced":
                        ass_path.write_text("old ass", encoding="utf-8")
                    if cutpoint in {"pair_replaced", "manifest_written"}:
                        ass_path.write_bytes(ass_artifact.read_bytes())
                    if cutpoint == "manifest_written":
                        orchestrator._write_subtitle_publication_manifest(
                            orchestrator.store.get(job.id),
                            generation,
                            srt_path=srt_path,
                            ass_path=ass_path,
                        )

                    repaired = orchestrator._reconcile_subtitle_publications()

                    recovered_job = orchestrator.store.get(job.id)
                    publication = (
                        orchestrator.store.published_subtitle_generation(
                            job.id
                        )
                    )
                    manifest = json.loads(
                        orchestrator._subtitle_publication_manifest_path(
                            job.source_rel
                        ).read_text(encoding="utf-8")
                    )
                    self.assertEqual(repaired, 1)
                    self.assertEqual(srt_path.read_bytes(), srt_artifact.read_bytes())
                    self.assertEqual(ass_path.read_bytes(), ass_artifact.read_bytes())
                    self.assertEqual(publication["id"], generation_id)
                    self.assertEqual(
                        manifest["subtitle_generation_id"],
                        generation_id,
                    )
                    self.assertEqual(recovered_job.status, "completed")
                    self.assertEqual(recovered_job.phase, "complete")
                    self.assertEqual(recovered_job.state, "done")
                    self.assertIsNone(recovered_job.lease_owner)
                finally:
                    orchestrator.stop()

    @unittest.skipIf(
        os.geteuid() == 0,
        "filesystem permission faults require a non-root test process",
    )
    def test_recovers_subtitle_publication_after_real_permission_fault(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            media_mode = media_root.stat().st_mode & 0o777
            try:
                job = orchestrator.store.create(
                    job_id="render-permission-fault",
                    source_rel="movie.mkv",
                    force_overwrite=True,
                    options={},
                    status="translated",
                )
                orchestrator.store.update(job.id, status="rendering")
                generation_id = "subtitle-permission-fault"
                srt_artifact, ass_artifact = (
                    orchestrator._subtitle_generation_artifact_paths(
                        job.id,
                        generation_id,
                    )
                )
                srt_artifact.parent.mkdir(parents=True)
                srt_artifact.write_text("new srt", encoding="utf-8")
                ass_artifact.write_text("new ass", encoding="utf-8")
                orchestrator.store.create_subtitle_generation(
                    generation_id=generation_id,
                    job_id=job.id,
                    translation_generation_id=None,
                    transcript_hash="transcript-hash",
                    translation_hash="translation-hash",
                    renderer_version="1",
                    render_hash="render-permission-fault",
                    srt_artifact_path=str(srt_artifact),
                    ass_artifact_path=str(ass_artifact),
                    srt_hash=sha256_file(srt_artifact),
                    ass_hash=sha256_file(ass_artifact),
                    origin="rendered",
                )

                media_root.chmod(0o555)
                try:
                    with self.assertLogs(
                        "stt_to_subtitle.orchestrator",
                        level="ERROR",
                    ) as failure_logs:
                        blocked_repair_count = (
                            orchestrator._reconcile_subtitle_publications()
                        )
                finally:
                    media_root.chmod(media_mode)

                blocked_job = orchestrator.store.get(job.id)
                self.assertEqual(blocked_repair_count, 0)
                self.assertTrue(
                    any(
                        "subtitle generation recovery failed" in message
                        for message in failure_logs.output
                    )
                )
                self.assertEqual(blocked_job.status, "rendering")
                self.assertIsNone(blocked_job.lease_owner)
                self.assertFalse((media_root / "movie.ko.srt").exists())
                self.assertFalse((media_root / "movie.ko.ass").exists())
                self.assertIsNone(
                    orchestrator.store.published_subtitle_generation(job.id)
                )

                repaired = orchestrator._reconcile_subtitle_publications()

                recovered_job = orchestrator.store.get(job.id)
                publication = (
                    orchestrator.store.published_subtitle_generation(job.id)
                )
                self.assertEqual(repaired, 1)
                self.assertEqual(recovered_job.status, "completed")
                self.assertEqual(recovered_job.phase, "complete")
                self.assertEqual(recovered_job.state, "done")
                self.assertIsNone(recovered_job.lease_owner)
                self.assertEqual(
                    (media_root / "movie.ko.srt").read_text(encoding="utf-8"),
                    "new srt",
                )
                self.assertEqual(
                    (media_root / "movie.ko.ass").read_text(encoding="utf-8"),
                    "new ass",
                )
                self.assertEqual(publication["id"], generation_id)
            finally:
                media_root.chmod(media_mode)
                orchestrator.stop()

    def test_does_not_recover_render_owned_by_an_active_worker(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "movie.mkv").write_bytes(b"media")
            orchestrator = self.make_orchestrator(root, media_root)
            try:
                job = orchestrator.store.create(
                    job_id="active-render",
                    source_rel="movie.mkv",
                    force_overwrite=True,
                    options={},
                    status="translated",
                )
                orchestrator.store.claim_for_dispatch(
                    job.id,
                    "translated",
                    "rendering",
                    lease_owner="other-worker",
                    lease_seconds=60,
                )
                generation_id = "subtitle-active-render"
                srt_artifact, ass_artifact = (
                    orchestrator._subtitle_generation_artifact_paths(
                        job.id,
                        generation_id,
                    )
                )
                srt_artifact.parent.mkdir(parents=True)
                srt_artifact.write_text("new srt", encoding="utf-8")
                ass_artifact.write_text("new ass", encoding="utf-8")
                orchestrator.store.create_subtitle_generation(
                    generation_id=generation_id,
                    job_id=job.id,
                    translation_generation_id=None,
                    transcript_hash="transcript-hash",
                    translation_hash="translation-hash",
                    renderer_version="1",
                    render_hash="render-hash",
                    srt_artifact_path=str(srt_artifact),
                    ass_artifact_path=str(ass_artifact),
                    srt_hash=sha256_file(srt_artifact),
                    ass_hash=sha256_file(ass_artifact),
                    origin="rendered",
                )

                repaired = orchestrator._reconcile_subtitle_publications()

                active_job = orchestrator.store.get(job.id)
                self.assertEqual(repaired, 0)
                self.assertEqual(active_job.status, "rendering")
                self.assertEqual(active_job.lease_owner, "other-worker")
                self.assertIsNone(
                    orchestrator.store.published_subtitle_generation(job.id)
                )
                self.assertFalse((media_root / "movie.ko.srt").exists())
                self.assertFalse((media_root / "movie.ko.ass").exists())
            finally:
                orchestrator.stop()

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

    def submit(  # noqa: ANN001
        self,
        _run_stage,
        job_id,
        stage,
        _operation,
        _lease_token,
    ):
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
            BackendSettings(
                state_dir=root / "state",
                media_root=media_root,
                stt_base_url="http://stt.test",
                stt_token="stt-token",
                translation_builtin_base_url="http://lm.test/v1",
                translation_builtin_token="lm-token",
                audio_workers=audio_workers,
            )
        )
        configure_translation_models(orchestrator)
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
                orchestrator.store.update(rendering.id, status="translated")
                orchestrator.store.claim_for_dispatch(
                    rendering.id,
                    "translated",
                    "rendering",
                    lease_owner="active-render-worker",
                    lease_seconds=60,
                )

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
