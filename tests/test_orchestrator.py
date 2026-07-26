import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from stt_to_subtitle.nas_config import NASSettings
from stt_to_subtitle.orchestrator import NASOrchestrator


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

                orchestrator.lm_client.translate = Mock(
                    return_value=[
                        {"id": "segment-000001", "text": "안녕하세요"}
                    ]
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
            self.assertIn(
                "transcription chunks: created 20, completed 10, in progress 10",
                progress_messages,
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
