from pathlib import Path
from tempfile import TemporaryDirectory
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

            self.assertEqual(store.fail_interrupted_jobs(), 1)
            self.assertEqual(store.get("job-1").status, "failed")


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
