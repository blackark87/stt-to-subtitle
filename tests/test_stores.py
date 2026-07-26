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
