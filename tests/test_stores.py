from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest

from stt_to_subtitle.job_store import JobStore, WorkerLeaseLost
from stt_to_subtitle.transcription_store import TranscriptionStore


class TranscriptionStoreTests(unittest.TestCase):
    def test_rebases_only_uploads_under_the_previous_work_root(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = TranscriptionStore(root / "jobs.sqlite3")
            store.create(
                job_id="moved",
                idempotency_key="moved-key",
                audio_path=Path("/var/lib/stt/incoming/moved.wav"),
                audio_sha256="abc",
                options={},
            )
            store.create(
                job_id="external",
                idempotency_key="external-key",
                audio_path=Path("/media/external.wav"),
                audio_sha256="def",
                options={},
            )

            changed = store.rebase_audio_paths(
                previous_root=Path("/var/lib/stt/incoming"),
                current_root=Path("/var/lib/stt-work"),
            )

            self.assertEqual(changed, 1)
            self.assertEqual(
                store.get("moved").audio_path,
                "/var/lib/stt-work/moved.wav",
            )
            self.assertEqual(
                store.get("external").audio_path,
                "/media/external.wav",
            )

    def test_calls_change_hook_after_transcription_mutations(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptionStore(Path(directory) / "jobs.sqlite3")
            changed_job_ids: list[str] = []
            store.set_change_hook(changed_job_ids.append)

            store.create(
                job_id="job-1",
                idempotency_key="key-1",
                audio_path=Path(directory) / "audio.wav",
                audio_sha256="abc",
                options={},
            )
            store.update("job-1", status="running")
            store.update_chunk_progress("job-1", created=2, completed=1)
            store.requeue("job-1")

            self.assertEqual(changed_job_ids, ["job-1"] * 4)

    def test_marks_running_jobs_failed_after_restart(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptionStore(Path(directory) / "jobs.sqlite3")
            store.create(
                job_id="job-1",
                idempotency_key="key-1",
                audio_path=Path(directory) / "audio.wav",
                audio_sha256="abc",
                options={"chunk_length_seconds": 15},
            )
            store.update("job-1", status="running")
            store.update_chunk_progress(
                "job-1",
                created=20,
                completed=10,
            )

            self.assertEqual(store.fail_interrupted_jobs(), 1)
            recovered = store.get("job-1")
            self.assertEqual(recovered.status, "failed")
            self.assertEqual(recovered.failure_code, "service_restarted")
            self.assertTrue(recovered.retryable)
            self.assertEqual(recovered.failure_scope, "service")
            public_job = recovered.public_dict()
            self.assertEqual(public_job["failure_code"], "service_restarted")
            self.assertTrue(public_job["retryable"])
            self.assertEqual(public_job["failure_scope"], "service")
            self.assertEqual(
                public_job["chunk_progress"],
                {
                    "created": 20,
                    "completed": 10,
                    "in_progress": 10,
                    "report_every": 10,
                },
            )
            self.assertTrue(public_job["created_at"].endswith("+09:00"))
            self.assertTrue(public_job["updated_at"].endswith("+09:00"))

    def test_requeue_resets_chunk_progress(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptionStore(Path(directory) / "jobs.sqlite3")
            store.create(
                job_id="job-1",
                idempotency_key="key-1",
                audio_path=Path(directory) / "audio.wav",
                audio_sha256="abc",
                options={"chunk_length_seconds": 60},
            )
            store.update_chunk_progress(
                "job-1",
                created=100,
                completed=90,
            )
            store.update(
                "job-1",
                status="failed",
                error="temporary failure",
                failure_code="service_restarted",
                retryable=True,
                failure_scope="service",
            )

            store.requeue(
                "job-1",
                options={"chunk_length_seconds": 30},
            )

            job = store.get("job-1")
            self.assertEqual(job.chunks_created, 0)
            self.assertEqual(job.chunks_completed, 0)
            self.assertEqual(job.attempt, 2)
            self.assertEqual(job.options["chunk_length_seconds"], 30)
            self.assertIsNone(job.failure_code)
            self.assertIsNone(job.retryable)
            self.assertIsNone(job.failure_scope)

    def test_cancels_queued_job_immediately_and_running_job_cooperatively(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptionStore(Path(directory) / "jobs.sqlite3")
            for job_id in ("queued", "running"):
                store.create(
                    job_id=job_id,
                    idempotency_key=f"{job_id}-key",
                    audio_path=Path(directory) / f"{job_id}.wav",
                    audio_sha256="abc",
                    options={},
                )
            store.update("running", status="running")

            queued = store.request_cancel("queued")
            running = store.request_cancel("running")

            self.assertEqual(queued.status, "cancelled")
            self.assertIsNotNone(queued.cancel_requested_at)
            self.assertIsNotNone(queued.cancelled_at)
            self.assertEqual(running.status, "cancel_requested")
            self.assertIsNotNone(running.cancel_requested_at)
            self.assertIsNone(running.cancelled_at)

            self.assertTrue(store.mark_cancelled("running"))
            cancelled = store.request_cancel("running")
            self.assertEqual(cancelled.status, "cancelled")
            self.assertIsNotNone(cancelled.cancelled_at)

    def test_restart_confirms_persisted_cancellation_request(self) -> None:
        with TemporaryDirectory() as directory:
            store = TranscriptionStore(Path(directory) / "jobs.sqlite3")
            store.create(
                job_id="job-1",
                idempotency_key="key-1",
                audio_path=Path(directory) / "audio.wav",
                audio_sha256="abc",
                options={},
            )
            store.update("job-1", status="running")
            store.request_cancel("job-1")

            self.assertEqual(store.fail_interrupted_jobs(), 1)
            recovered = store.get("job-1")
            self.assertEqual(recovered.status, "cancelled")
            self.assertIsNone(recovered.error)
            self.assertIsNotNone(recovered.cancelled_at)

    def test_adds_chunk_columns_to_an_existing_database(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.sqlite3"
            now = time.time()
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE transcription_jobs (
                        id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL,
                        audio_path TEXT NOT NULL,
                        audio_sha256 TEXT NOT NULL,
                        options_json TEXT NOT NULL,
                        result_path TEXT,
                        error TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO transcription_jobs VALUES (
                        'job-1', 'key-1', 'queued', '/audio.wav',
                        'abc', '{}', NULL, NULL, ?, ?
                    )
                    """,
                    (now, now),
                )
            connection.close()

            job = TranscriptionStore(database_path).get("job-1")

            self.assertEqual(job.chunks_created, 0)
            self.assertEqual(job.chunks_completed, 0)
            self.assertEqual(job.attempt, 1)
            self.assertIsNone(job.retryable)
            self.assertIsNone(job.failure_scope)


class JobStoreTests(unittest.TestCase):
    def test_requires_explicit_structured_state_for_new_user_stops(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            job = store.create(
                job_id="job-1",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )

            store.update(
                job.id,
                status="blocked",
                blocked_stage="transcription",
                error="사용자 요청으로 작업이 중단되었습니다.",
            )
            blocked = store.get(job.id)
            self.assertEqual(blocked.state, "blocked")
            self.assertEqual(blocked.reason_code, "stt_unavailable")

            store.update(
                job.id,
                status="blocked",
                state="stopped",
                reason_code="user_stop",
            )
            stopped = store.get(job.id)
            self.assertEqual(stopped.state, "stopped")
            self.assertEqual(stopped.reason_code, "user_stop")
            with self.assertRaisesRegex(ValueError, "invalid structured"):
                store.update(job.id, state="attention")

    def test_rebases_only_artifacts_under_the_previous_work_root(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            job = store.create(
                job_id="job-1",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )
            store.update(
                job.id,
                audio_path="/var/lib/stt/jobs/job-1/audio.16k.wav",
                transcript_path="/var/lib/stt/jobs/job-1/transcript.json",
                translation_path="/media/external-result.json",
            )
            store.create_translation_generation(
                generation_id="translation-1",
                job_id=job.id,
                transcript_job_id="remote-1",
                transcript_hash="transcript-hash",
                prompt_hash="prompt-hash",
                endpoint_key="http://lm.test/v1",
                model="model",
                config_hash="config-hash",
                artifact_path=(
                    "/var/lib/stt/jobs/job-1/translation-1.json"
                ),
                origin="automatic",
            )
            store.create_subtitle_generation(
                generation_id="subtitle-1",
                job_id=job.id,
                translation_generation_id="translation-1",
                transcript_hash="transcript-hash",
                translation_hash="translation-hash",
                renderer_version="1",
                render_hash="render-hash",
                srt_artifact_path=(
                    "/var/lib/stt/jobs/job-1/subtitle-1.srt"
                ),
                ass_artifact_path=(
                    "/var/lib/stt/jobs/job-1/subtitle-1.ass"
                ),
                srt_hash="srt-hash",
                ass_hash="ass-hash",
                origin="rendered",
            )

            changed = store.rebase_artifact_paths(
                previous_root=Path("/var/lib/stt/jobs"),
                current_root=Path("/var/lib/stt-work"),
            )

            rebased = store.get(job.id)
            self.assertEqual(changed, 3)
            self.assertEqual(
                rebased.audio_path,
                "/var/lib/stt-work/job-1/audio.16k.wav",
            )
            self.assertEqual(
                rebased.transcript_path,
                "/var/lib/stt-work/job-1/transcript.json",
            )
            self.assertEqual(
                rebased.translation_path,
                "/media/external-result.json",
            )
            translation = store.latest_translation_generation(job.id)
            subtitle = store.get_subtitle_generation("subtitle-1")
            self.assertEqual(
                translation["artifact_path"],
                "/var/lib/stt-work/job-1/translation-1.json",
            )
            self.assertEqual(
                subtitle["srt_artifact_path"],
                "/var/lib/stt-work/job-1/subtitle-1.srt",
            )
            self.assertEqual(
                subtitle["ass_artifact_path"],
                "/var/lib/stt-work/job-1/subtitle-1.ass",
            )

    def test_lists_and_counts_jobs_by_status(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            queued = store.create(
                job_id="queued-job",
                source_rel="queued.mkv",
                force_overwrite=False,
                options={},
            )
            blocked = store.create(
                job_id="blocked-job",
                source_rel="blocked.mkv",
                force_overwrite=False,
                options={},
            )
            store.update(blocked.id, status="blocked")
            completed = store.create(
                job_id="completed-job",
                source_rel="completed.mkv",
                force_overwrite=False,
                options={},
            )
            store.update(completed.id, status="completed")

            attention = store.list_jobs(statuses={"blocked", "failed"})

            self.assertEqual([job.id for job in attention], [blocked.id])
            self.assertEqual(
                store.count_jobs(statuses={"blocked", "failed"}),
                1,
            )
            self.assertEqual(store.list_jobs(statuses=set()), [])
            self.assertEqual(store.count_jobs(statuses=set()), 0)
            self.assertEqual(store.count_jobs(), 3)
            self.assertEqual(queued.status, "queued")
            self.assertEqual(queued.phase, "extraction")
            self.assertEqual(queued.state, "waiting")
            self.assertEqual(store.get(blocked.id).state, "blocked")
            self.assertEqual(store.get(completed.id).state, "done")
            self.assertEqual(
                store.count_jobs(states={"blocked", "failed"}),
                1,
            )

    def test_can_exclude_comparison_only_transcriptions(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            regular = store.create(
                job_id="regular-transcription",
                source_rel="regular.mkv",
                force_overwrite=False,
                options={},
                operation="transcribe",
            )
            store.update(regular.id, status="transcription_completed")
            comparison = store.create(
                job_id="comparison-transcription",
                source_rel="comparison.mkv",
                force_overwrite=False,
                options={"comparison_id": "comparison-1"},
                operation="transcribe",
            )
            store.update(comparison.id, status="transcription_completed")
            translated = store.create(
                job_id="translated-comparison",
                source_rel="translated.mkv",
                force_overwrite=True,
                options={"comparison_id": "comparison-1"},
                operation="full",
            )
            store.update(translated.id, status="completed")

            visible = store.list_jobs(
                limit=None,
                include_comparison_transcriptions=False,
            )

            self.assertEqual(
                [job.id for job in visible],
                [translated.id, regular.id],
            )
            self.assertEqual(
                store.count_jobs(
                    include_comparison_transcriptions=False,
                ),
                2,
            )
            self.assertEqual(
                store.count_jobs(
                    statuses={"transcription_completed"},
                    include_comparison_transcriptions=False,
                ),
                1,
            )
            self.assertEqual(store.count_jobs(), 3)

    def test_calls_change_hook_after_job_mutations(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            changed_job_ids: list[str] = []
            store.set_change_hook(changed_job_ids.append)

            job = store.create(
                job_id="job-1",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )
            store.update(job.id, status="blocked")
            store.add_event(job.id, "warning", "stopped")
            store.delete(job.id)

            self.assertEqual(changed_job_ids, [job.id] * 4)

    def test_seeds_edits_and_archives_prompt_categories(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.sqlite3"
            store = JobStore(database_path)

            self.assertEqual(
                {category.id for category in store.list_prompt_categories()},
                {"jav", "variety"},
            )
            created = store.create_prompt_category(
                name="애니메이션",
                translation_prompt="translate",
                review_prompt="review",
            )
            updated = store.update_prompt_category(
                created.id,
                name="드라마",
                translation_prompt="translate v2",
                review_prompt="review v2",
            )
            archived = store.set_prompt_category_archived(
                created.id,
                archived=True,
            )

            self.assertEqual(updated.name, "드라마")
            self.assertEqual(updated.translation_prompt, "translate v2")
            self.assertTrue(archived.archived)
            self.assertNotIn(
                created.id,
                {
                    category.id
                    for category in store.list_prompt_categories()
                },
            )
            self.assertIn(
                created.id,
                {
                    category.id
                    for category in JobStore(
                        database_path
                    ).list_prompt_categories(include_archived=True)
                },
            )

    def test_manages_path_display_rules_without_reseeding_deleted_default(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.sqlite3"
            store = JobStore(database_path)
            default_rule = store.list_path_display_rules()[0]
            self.assertEqual(
                default_rule.source_pattern,
                "av/japan/{actress}/{content_id}/{filename}",
            )
            self.assertEqual(
                default_rule.display_pattern,
                "av/japan/{actress}/{filename}",
            )

            created = store.create_path_display_rule(
                source_pattern="{actress}/{content_id}/{filename}",
                display_pattern="{actress}/{filename}",
            )
            updated = store.update_path_display_rule(
                created.id,
                source_pattern="shows/{season}/{filename}",
                display_pattern="shows/{filename}",
            )
            store.delete_path_display_rule(default_rule.id)

            self.assertEqual(updated.display_pattern, "shows/{filename}")
            self.assertEqual(
                [rule.id for rule in JobStore(database_path).list_path_display_rules()],
                [created.id],
            )

    def test_corrects_the_legacy_default_path_display_rule(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.sqlite3"
            JobStore(database_path)
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE path_display_rules
                    SET source_pattern = ?, display_pattern = ?
                    WHERE id = 'default-actress-content'
                    """,
                    (
                        "{root}/{collection}/{actress}/{content_id}/{filename}",
                        "{actress}/{filename}",
                    ),
                )
                connection.execute(
                    "DELETE FROM schema_migrations "
                    "WHERE name = 'correct_default_path_display_rule_v2'"
                )

            rule = JobStore(database_path).list_path_display_rules()[0]

            self.assertEqual(
                rule.source_pattern,
                "av/japan/{actress}/{content_id}/{filename}",
            )
            self.assertEqual(
                rule.display_pattern,
                "av/japan/{actress}/{filename}",
            )

    def test_lists_jobs_by_latest_status_change_without_progress_reordering(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            for index in range(3):
                store.create(
                    job_id=f"job-{index}",
                    source_rel=f"movie-{index}.mkv",
                    force_overwrite=False,
                    options={},
                )

            self.assertEqual(store.count_jobs(), 3)
            self.assertEqual(
                [job.id for job in store.list_jobs(limit=2)],
                ["job-2", "job-1"],
            )
            self.assertEqual(
                [job.id for job in store.list_jobs(limit=2, offset=2)],
                ["job-0"],
            )
            store.update("job-0", chunks_completed=1)
            self.assertEqual(
                [job.id for job in store.list_jobs(limit=3)],
                ["job-2", "job-1", "job-0"],
            )
            store.update("job-0", status="blocked")
            self.assertEqual(
                [job.id for job in store.list_jobs(limit=3)],
                ["job-0", "job-2", "job-1"],
            )

    def test_treats_transcription_as_success_and_finds_latest_transcript(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            older = store.create(
                job_id="older",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
                operation="transcribe",
            )
            store.update(
                older.id,
                status="transcription_completed",
                transcript_path="/artifacts/older.json",
            )
            newer = store.create(
                job_id="newer",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
                operation="full",
            )
            store.update(
                newer.id,
                status="completed",
                transcript_path="/artifacts/newer.json",
            )

            self.assertEqual(store.list_open_jobs(), [])
            self.assertEqual(store.count_successful_jobs(), 2)
            self.assertEqual(
                store.latest_transcript_job("movie.mkv").id,
                "newer",
            )

    def test_does_not_dispatch_a_stopped_or_translation_paused_job(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            stopped = store.create(
                job_id="stopped",
                source_rel="stopped.mkv",
                force_overwrite=False,
                options={},
            )
            paused = store.create(
                job_id="paused",
                source_rel="paused.mkv",
                force_overwrite=False,
                options={},
            )
            store.update(stopped.id, job_stop_requested=1)
            store.update(
                paused.id,
                status="transcribed",
                translation_pause_requested=1,
            )

            self.assertFalse(
                store.claim_for_dispatch(stopped.id, "queued", "extracting")
            )
            self.assertFalse(
                store.claim_for_dispatch(
                    paused.id,
                    "transcribed",
                    "translation_running",
                )
            )
            self.assertEqual(store.get(stopped.id).status, "queued")
            self.assertEqual(store.get(paused.id).status, "transcribed")

    def test_claims_refreshes_and_expires_worker_lease(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            job = store.create(
                job_id="leased",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )

            claimed = store.claim_for_dispatch(
                job.id,
                "queued",
                "extracting",
                lease_owner="worker-a",
                lease_seconds=60,
            )
            leased = store.get(job.id)

            self.assertTrue(claimed)
            self.assertEqual(claimed, 1)
            self.assertEqual(leased.lease_owner, "worker-a")
            self.assertEqual(leased.lease_token, 1)
            self.assertGreater(leased.lease_expires_at, time.time())
            self.assertFalse(
                store.claim_recovery_lease(
                    job.id,
                    "extracting",
                    lease_owner="worker-b",
                    lease_seconds=60,
                )
            )
            self.assertFalse(
                store.refresh_job_lease(
                    job.id,
                    lease_owner="worker-b",
                    lease_token=leased.lease_token,
                    lease_seconds=60,
                )
            )
            self.assertTrue(
                store.refresh_job_lease(
                    job.id,
                    lease_owner="worker-a",
                    lease_token=leased.lease_token,
                    lease_seconds=60,
                )
            )
            self.assertEqual(store.recoverable_running_jobs({"extracting"}), [])

            store.update(job.id, lease_expires_at=time.time() - 1)
            self.assertFalse(
                store.refresh_job_lease(
                    job.id,
                    lease_owner="worker-a",
                    lease_token=leased.lease_token,
                    lease_seconds=60,
                )
            )
            self.assertEqual(
                [item.id for item in store.recoverable_running_jobs({"extracting"})],
                [job.id],
            )
            recovered_token = store.claim_recovery_lease(
                job.id,
                "extracting",
                lease_owner="worker-b",
                lease_seconds=60,
            )
            self.assertEqual(recovered_token, 2)
            self.assertFalse(
                store.update_if_lease(
                    job.id,
                    lease_owner="worker-a",
                    lease_token=leased.lease_token,
                    status="audio_ready",
                )
            )
            self.assertTrue(
                store.update_if_lease(
                    job.id,
                    lease_owner="worker-b",
                    lease_token=recovered_token,
                    status="audio_ready",
                )
            )
            completed = store.get(job.id)
            self.assertIsNone(completed.lease_owner)
            self.assertIsNone(completed.lease_expires_at)
            self.assertEqual(completed.lease_token, 2)

    def test_deletes_a_job_and_its_events(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            store.create(
                job_id="job-1",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )
            store.add_event("job-1", "warning", "remote job disappeared")

            self.assertTrue(store.delete("job-1"))
            self.assertIsNone(store.get("job-1"))
            self.assertEqual(store.events("job-1"), [])
            self.assertFalse(store.delete("job-1"))

    def test_persists_remote_server_settings(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.sqlite3"
            store = JobStore(database_path)

            store.save_remote_server_settings(
                stt_base_url="http://stt.test",
                stt_token="stt-token",
                lm_base_url="http://lm.test/v1",
                lm_token="lm-token",
                lm_model="model",
            )

            self.assertEqual(
                JobStore(database_path).get_remote_server_settings(),
                {
                    "stt_base_url": "http://stt.test",
                    "stt_token": "stt-token",
                    "lm_base_url": "http://lm.test/v1",
                    "lm_token": "lm-token",
                    "lm_model": "model",
                    "translation_workers": 1,
                },
            )

    def test_persists_dependency_gate_state_without_credentials(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.sqlite3"
            store = JobStore(database_path)

            store.save_dependency_state(
                "stt",
                state="lost",
                reason_code="stt_unavailable",
                error="connection refused",
            )
            store.save_dependency_state(
                "translation_lm",
                state="offline",
                reason_code="manual_stop",
                error="stopped by operator",
            )

            state = JobStore(database_path).get_dependency_state("stt")
            self.assertEqual(state["dependency"], "stt")
            self.assertEqual(state["state"], "lost")
            self.assertEqual(state["reason_code"], "stt_unavailable")
            self.assertEqual(state["last_error"], "connection refused")
            lm_state = JobStore(database_path).get_dependency_state(
                "translation_lm"
            )
            self.assertEqual(lm_state["state"], "offline")
            self.assertEqual(lm_state["reason_code"], "manual_stop")
            self.assertEqual(lm_state["last_error"], "stopped by operator")

    def test_persists_subtitle_validator_settings(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.sqlite3"
            store = JobStore(database_path)

            store.save_subtitle_validator_settings(
                base_url="https://validator.test/v1",
                token="paid-token",
                model="paid-model",
            )

            self.assertEqual(
                JobStore(database_path).get_subtitle_validator_settings(),
                {
                    "base_url": "https://validator.test/v1",
                    "token": "paid-token",
                    "model": "paid-model",
                },
            )

    def test_versions_and_updates_subtitle_validation(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            store.create(
                job_id="job-1",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )
            validation = store.save_subtitle_validation(
                job_id="job-1",
                source_rel="movie.mkv",
                external_path="movie.srt",
                external_hash="external-v1",
                candidate_path="movie.ko.srt",
                candidate_hash="candidate-v1",
                metrics={"summary": {"time_coverage": 1.0}},
            )
            updated = store.save_subtitle_llm_validation(
                validation["id"],
                result={"severity": "pass", "summary": "통과", "findings": []},
                model="paid-model",
                input_hash="input-v1",
            )

            self.assertEqual(updated["llm"]["severity"], "pass")
            self.assertEqual(
                store.get_subtitle_validation(
                    job_id="job-1",
                    external_hash="external-v1",
                    candidate_hash="candidate-v1",
                )["validator_input_hash"],
                "input-v1",
            )
            self.assertEqual(
                store.get_subtitle_validation_by_id(validation["id"])["id"],
                validation["id"],
            )

            newer = store.save_subtitle_validation(
                job_id="job-1",
                source_rel="movie.mkv",
                external_path="movie.srt",
                external_hash="external-v1",
                candidate_path="movie.ko.srt",
                candidate_hash="candidate-v2",
                metrics={"summary": {"time_coverage": 0.5}},
            )
            self.assertNotEqual(newer["id"], validation["id"])
            self.assertTrue(store.delete("job-1"))
            self.assertIsNone(
                store.get_subtitle_validation_by_id(validation["id"])
            )

    def test_translation_generations_preserve_batches_items_and_history(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            store.create(
                job_id="job-1",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )
            first = store.create_translation_generation(
                generation_id="generation-1",
                job_id="job-1",
                transcript_job_id="transcript-1",
                transcript_hash="transcript-hash",
                prompt_hash="prompt-v1",
                endpoint_key="http://lm.test/v1",
                model="model",
                config_hash="config-v1",
                artifact_path="generation-1.json",
                origin="automatic",
            )
            self.assertEqual(
                store.begin_translation_generation_attempt(first["id"]),
                1,
            )
            store.start_translation_batch(
                first["id"],
                batch_index=0,
                generation_attempt=1,
                items=[
                    {"id": "segment-1", "source_hash": "source-1"}
                ],
            )
            store.fail_translation_batch(
                first["id"],
                batch_index=0,
                error="model unavailable",
            )
            failed_batch = store.translation_batches(first["id"])[0]
            self.assertEqual(failed_batch["state"], "failed")
            self.assertEqual(failed_batch["error"], "model unavailable")
            self.assertEqual(
                store.completed_translation_batch_count(first["id"]),
                0,
            )
            store.save_translation_batch(
                first["id"],
                batch_index=0,
                generation_attempt=1,
                kind="remote",
                items=[
                    {
                        "id": "segment-1",
                        "text": "첫 번역",
                        "source_hash": "source-1",
                        "segment_index": 0,
                    }
                ],
            )
            store.save_translation_batch(
                first["id"],
                batch_index=1,
                generation_attempt=1,
                kind="remote",
                items=[
                    {
                        "id": "segment-2",
                        "text": "둘째 번역",
                        "source_hash": "source-2",
                        "segment_index": 1,
                    }
                ],
            )
            completed = store.complete_translation_generation(
                first["id"],
                ["segment-1", "segment-2"],
            )
            second = store.create_translation_generation(
                generation_id="generation-2",
                job_id="job-1",
                transcript_job_id="transcript-1",
                transcript_hash="transcript-hash",
                prompt_hash="prompt-v2",
                endpoint_key="http://lm.test/v1",
                model="model",
                config_hash="config-v2",
                artifact_path="generation-2.json",
                origin="restart",
                force_new=True,
            )

            self.assertEqual(
                completed,
                [
                    {"id": "segment-1", "text": "첫 번역"},
                    {"id": "segment-2", "text": "둘째 번역"},
                ],
            )
            self.assertEqual(
                store.completed_translation_batch_count(first["id"]),
                2,
            )
            self.assertEqual(store.next_translation_batch_index(first["id"]), 2)
            self.assertEqual(
                [item["id"] for item in store.translation_items(first["id"])],
                ["segment-1", "segment-2"],
            )
            self.assertEqual(second["generation_number"], 2)
            self.assertEqual(second["supersedes_generation_id"], first["id"])
            self.assertEqual(
                [
                    item["state"]
                    for item in store.list_translation_generations("job-1")
                ],
                ["completed", "partial"],
            )

    def test_translation_generation_rejects_an_incomplete_item_set(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            store.create(
                job_id="job-1",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )
            generation = store.create_translation_generation(
                generation_id="generation-1",
                job_id="job-1",
                transcript_job_id="transcript-1",
                transcript_hash="transcript-hash",
                prompt_hash="prompt-hash",
                endpoint_key="http://lm.test/v1",
                model="model",
                config_hash="config-hash",
                artifact_path="generation-1.json",
                origin="automatic",
            )

            with self.assertRaisesRegex(ValueError, "do not match"):
                store.complete_translation_generation(
                    generation["id"],
                    ["segment-1"],
                )

    def test_versions_and_publishes_subtitle_pairs(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            store.create(
                job_id="job-1",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )
            first = store.create_subtitle_generation(
                generation_id="subtitle-1",
                job_id="job-1",
                translation_generation_id=None,
                transcript_hash="transcript-v1",
                translation_hash="translation-v1",
                renderer_version="1",
                render_hash="render-v1",
                srt_artifact_path="subtitle-1.srt",
                ass_artifact_path="subtitle-1.ass",
                srt_hash="srt-v1",
                ass_hash="ass-v1",
                origin="legacy",
            )
            store.publish_subtitle_generation(
                first["id"],
                srt_path="movie.ko.srt",
                ass_path="movie.ko.ass",
            )
            second = store.create_subtitle_generation(
                generation_id="subtitle-2",
                job_id="job-1",
                translation_generation_id=None,
                transcript_hash="transcript-v1",
                translation_hash="translation-v2",
                renderer_version="1",
                render_hash="render-v2",
                srt_artifact_path="subtitle-2.srt",
                ass_artifact_path="subtitle-2.ass",
                srt_hash="srt-v2",
                ass_hash="ass-v2",
                origin="rendered",
            )
            published = store.publish_subtitle_generation(
                second["id"],
                srt_path="movie.ko.srt",
                ass_path="movie.ko.ass",
            )
            generations = store.list_subtitle_generations("job-1")

            self.assertEqual(second["generation_number"], 2)
            self.assertEqual(
                second["supersedes_generation_id"],
                first["id"],
            )
            self.assertEqual(
                [item["is_published"] for item in generations],
                [False, True],
            )
            self.assertTrue(published["is_published"])
            self.assertEqual(
                store.published_subtitle_generation("job-1")["id"],
                second["id"],
            )
            publications = store.list_subtitle_publications()
            self.assertEqual(len(publications), 1)
            self.assertEqual(publications[0]["source_rel"], "movie.mkv")
            self.assertEqual(publications[0]["id"], second["id"])
            self.assertEqual(store.get("job-1").srt_path, "movie.ko.srt")

            store.create(
                job_id="job-2",
                source_rel="movie.mkv",
                force_overwrite=True,
                options={},
            )
            third = store.create_subtitle_generation(
                generation_id="subtitle-3",
                job_id="job-2",
                translation_generation_id=None,
                transcript_hash="transcript-v2",
                translation_hash="translation-v3",
                renderer_version="1",
                render_hash="render-v3",
                srt_artifact_path="subtitle-3.srt",
                ass_artifact_path="subtitle-3.ass",
                srt_hash="srt-v3",
                ass_hash="ass-v3",
                origin="rendered",
            )
            store.publish_subtitle_generation(
                third["id"],
                srt_path="movie.ko.srt",
                ass_path="movie.ko.ass",
            )

            self.assertEqual(
                [
                    item["is_published"]
                    for item in store.list_subtitle_generations("job-1")
                ],
                [False, False],
            )
            self.assertEqual(
                store.published_subtitle_generation("job-1")["id"],
                third["id"],
            )

    def test_superseded_worker_cannot_publish_subtitles(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            job = store.create(
                job_id="render-job",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )
            first_token = store.claim_for_dispatch(
                job.id,
                "queued",
                "rendering",
                lease_owner="worker-a",
                lease_seconds=60,
            )
            generation = store.create_subtitle_generation(
                generation_id="subtitle-fenced",
                job_id=job.id,
                translation_generation_id=None,
                transcript_hash="transcript-v1",
                translation_hash="translation-v1",
                renderer_version="1",
                render_hash="render-v1",
                srt_artifact_path="subtitle.srt",
                ass_artifact_path="subtitle.ass",
                srt_hash="srt-v1",
                ass_hash="ass-v1",
                origin="rendered",
            )
            store.update(job.id, lease_expires_at=time.time() - 1)
            second_token = store.claim_recovery_lease(
                job.id,
                "rendering",
                lease_owner="worker-b",
                lease_seconds=60,
            )

            with self.assertRaises(WorkerLeaseLost):
                store.publish_subtitle_generation(
                    generation["id"],
                    srt_path="movie.ko.srt",
                    ass_path="movie.ko.ass",
                    lease_owner="worker-a",
                    lease_token=first_token,
                )

            fenced = store.get(job.id)
            self.assertEqual(second_token, first_token + 1)
            self.assertEqual(fenced.status, "rendering")
            self.assertEqual(fenced.lease_owner, "worker-b")
            self.assertIsNone(store.published_subtitle_generation(job.id))

    def test_persists_chunk_progress_for_the_job_panel(self) -> None:
        with TemporaryDirectory() as directory:
            store = JobStore(Path(directory) / "jobs.sqlite3")
            store.create(
                job_id="job-1",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )

            store.update(
                "job-1",
                chunks_created=21,
                chunks_completed=20,
                chunks_total_estimate=24,
                chunk_progress_every=10,
            )

            job = store.get("job-1")
            self.assertEqual(job.chunks_created, 21)
            self.assertEqual(job.chunks_completed, 20)
            self.assertEqual(job.chunks_in_progress, 1)
            self.assertEqual(job.chunks_total_estimate, 24)
            self.assertEqual(job.transcription_chunks_total, 24)
            self.assertTrue(job.transcription_total_is_estimated)
            self.assertEqual(job.transcription_chunks_remaining, 4)
            self.assertEqual(job.chunk_progress_every, 10)

    def test_adds_progress_columns_to_an_existing_web_database(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.sqlite3"
            now = time.time()
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    CREATE TABLE jobs (
                        id TEXT PRIMARY KEY,
                        source_rel TEXT NOT NULL,
                        status TEXT NOT NULL,
                        force_overwrite INTEGER NOT NULL,
                        options_json TEXT NOT NULL,
                        audio_path TEXT,
                        audio_sha256 TEXT,
                        stt_job_id TEXT,
                        transcript_path TEXT,
                        translation_path TEXT,
                        srt_path TEXT,
                        blocked_stage TEXT,
                        error TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO jobs VALUES (
                        'job-1', 'movie.mkv', 'queued', 0, '{}',
                        NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?
                    )
                    """,
                    (now, now),
                )
                connection.execute(
                    """
                    INSERT INTO jobs VALUES (
                        'job-2', 'stopped.mkv', 'blocked', 0, '{}',
                        NULL, NULL, NULL, NULL, NULL, NULL, 'translation',
                        '사용자 요청으로 작업이 중단되었습니다.', ?, ?
                    )
                    """,
                    (now, now),
                )
            connection.close()

            job = JobStore(database_path).get("job-1")

            self.assertEqual(job.chunks_created, 0)
            self.assertEqual(job.chunks_completed, 0)
            self.assertEqual(job.chunks_total_estimate, 0)
            self.assertEqual(job.chunk_progress_every, 10)
            self.assertEqual(job.operation, "full")
            self.assertEqual(job.translation_chunks_total, 0)
            self.assertEqual(job.translation_chunks_completed, 0)
            self.assertFalse(job.translation_pause_requested)
            self.assertFalse(job.job_stop_requested)
            self.assertIsNone(job.ass_path)
            self.assertEqual(job.phase, "extraction")
            self.assertEqual(job.state, "waiting")
            self.assertEqual(job.attempt, 1)
            self.assertIsNone(job.lease_owner)
            self.assertIsNone(job.lease_expires_at)
            self.assertEqual(job.lease_token, 0)
            stopped = JobStore(database_path).get("job-2")
            self.assertEqual(stopped.phase, "translation")
            self.assertEqual(stopped.state, "stopped")
            self.assertEqual(stopped.reason_code, "user_stop")
