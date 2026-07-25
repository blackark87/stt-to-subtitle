from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from stt_to_subtitle.nas_config import NASSettings
from stt_to_subtitle.orchestrator import NASOrchestrator


class NASOrchestratorTests(unittest.TestCase):
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
                orchestrator.stt_client.transcribe = Mock(
                    return_value={
                        "schema_version": 1,
                        "job_id": "remote-job",
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
                )
                orchestrator._transcribe(orchestrator.store.get(job.id))

                orchestrator.lm_client.translate = Mock(
                    return_value=[
                        {"id": "segment-000001", "text": "안녕하세요"}
                    ]
                )
                orchestrator._translate(orchestrator.store.get(job.id))
                orchestrator._render(orchestrator.store.get(job.id))
            finally:
                orchestrator.stop()

            subtitle = source.with_name("movie.ko.srt").read_text(encoding="utf-8")
            self.assertIn("00:00:11,000 --> 00:00:12,000", subtitle)
            self.assertIn("안녕하세요", subtitle)
            self.assertNotIn("SPEAKER_00", subtitle)
            self.assertEqual(orchestrator.store.get(job.id).status, "completed")
