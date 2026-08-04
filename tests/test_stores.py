from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
import unittest

from stt_to_subtitle.nas_store import NASStore
from stt_to_subtitle.transcription_store import TranscriptionStore


class TranscriptionStoreTests(unittest.TestCase):
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
            self.assertEqual(store.get("job-1").status, "failed")
            public_job = store.get("job-1").public_dict()
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
                options={},
            )
            store.update_chunk_progress(
                "job-1",
                created=100,
                completed=90,
            )

            store.requeue("job-1")

            job = store.get("job-1")
            self.assertEqual(job.chunks_created, 0)
            self.assertEqual(job.chunks_completed, 0)

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

            job = TranscriptionStore(database_path).get("job-1")

            self.assertEqual(job.chunks_created, 0)
            self.assertEqual(job.chunks_completed, 0)


class NASStoreTests(unittest.TestCase):
    def test_seeds_edits_and_archives_prompt_categories(self) -> None:
        with TemporaryDirectory() as directory:
            database_path = Path(directory) / "jobs.sqlite3"
            store = NASStore(database_path)

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
                    for category in NASStore(
                        database_path
                    ).list_prompt_categories(include_archived=True)
                },
            )

    def test_lists_all_jobs_in_one_paginated_creation_order(self) -> None:
        with TemporaryDirectory() as directory:
            store = NASStore(Path(directory) / "jobs.sqlite3")
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

    def test_does_not_dispatch_a_stopped_or_translation_paused_job(self) -> None:
        with TemporaryDirectory() as directory:
            store = NASStore(Path(directory) / "jobs.sqlite3")
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

    def test_deletes_a_job_and_its_events(self) -> None:
        with TemporaryDirectory() as directory:
            store = NASStore(Path(directory) / "jobs.sqlite3")
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
            store = NASStore(database_path)

            store.save_remote_server_settings(
                stt_base_url="http://stt.test",
                stt_token="stt-token",
                lm_base_url="http://lm.test/v1",
                lm_token="lm-token",
                lm_model="model",
            )

            self.assertEqual(
                NASStore(database_path).get_remote_server_settings(),
                {
                    "stt_base_url": "http://stt.test",
                    "stt_token": "stt-token",
                    "lm_base_url": "http://lm.test/v1",
                    "lm_token": "lm-token",
                    "lm_model": "model",
                    "translation_workers": 1,
                },
            )

    def test_recovers_running_stage_as_manually_retryable(self) -> None:
        with TemporaryDirectory() as directory:
            store = NASStore(Path(directory) / "jobs.sqlite3")
            store.create(
                job_id="job-1",
                source_rel="movie.mkv",
                force_overwrite=False,
                options={},
            )
            store.update("job-1", status="translation_running")

            self.assertEqual(store.recover_interrupted(), 1)
            job = store.get("job-1")
            self.assertEqual(job.status, "blocked")
            self.assertEqual(job.blocked_stage, "translation")

    def test_persists_chunk_progress_for_the_job_panel(self) -> None:
        with TemporaryDirectory() as directory:
            store = NASStore(Path(directory) / "jobs.sqlite3")
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
                chunk_progress_every=10,
            )

            job = store.get("job-1")
            self.assertEqual(job.chunks_created, 21)
            self.assertEqual(job.chunks_completed, 20)
            self.assertEqual(job.chunks_in_progress, 1)
            self.assertEqual(job.chunk_progress_every, 10)

    def test_adds_progress_columns_to_an_existing_nas_database(self) -> None:
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

            job = NASStore(database_path).get("job-1")

            self.assertEqual(job.chunks_created, 0)
            self.assertEqual(job.chunks_completed, 0)
            self.assertEqual(job.chunk_progress_every, 10)
            self.assertEqual(job.operation, "full")
            self.assertEqual(job.translation_chunks_total, 0)
            self.assertEqual(job.translation_chunks_completed, 0)
            self.assertFalse(job.translation_pause_requested)
            self.assertFalse(job.job_stop_requested)
            self.assertIsNone(job.ass_path)
