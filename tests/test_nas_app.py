from importlib.util import find_spec
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

NAS_WEB_TESTS_AVAILABLE = all(
    find_spec(module) is not None
    for module in ("itsdangerous", "jinja2", "multipart")
)
if NAS_WEB_TESTS_AVAILABLE:
    from fastapi.testclient import TestClient

from stt_to_subtitle.nas_config import NASSettings

if NAS_WEB_TESTS_AVAILABLE:
    from stt_to_subtitle.nas_app import create_app


@unittest.skipUnless(
    NAS_WEB_TESTS_AVAILABLE,
    "NAS web test dependencies are not installed",
)
class NASAppTests(unittest.TestCase):
    def settings(self, root: Path, media_root: Path) -> NASSettings:
        return NASSettings(
            state_dir=root / "state",
            media_root=media_root,
            admin_password="",
            session_secret="",
            stt_base_url="http://stt.test",
            stt_token="",
            lm_base_url="http://lm.test/v1",
            lm_token="",
            lm_model="model",
        )

    def test_dashboard_renders_media_cards_and_local_poster(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            show = media_root / "show"
            show.mkdir(parents=True)
            (show / "episode.mkv").write_bytes(b"media")
            (show / "episode.ko.srt").write_text("subtitle", encoding="utf-8")
            (show / "episode.nfo").write_text(
                """
                <episodedetails>
                  <title>첫 번째 에피소드</title>
                  <thumb aspect="poster">poster.jpg</thumb>
                </episodedetails>
                """,
                encoding="utf-8",
            )
            (show / "poster.jpg").write_bytes(b"poster-bytes")
            (media_root / "plain.mp4").write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                root_response = client.get("/")
                response = client.get("/?folder=show")
                poster = client.get("/media/posters/show/poster.jpg")

            self.assertEqual(root_response.status_code, 200)
            self.assertIn('class="folder-card"', root_response.text)
            self.assertIn("data-folder-link", root_response.text)
            self.assertIn("data-folder-loading", root_response.text)
            self.assertIn("folder-browser.js", root_response.text)
            self.assertIn("show", root_response.text)
            self.assertIn("plain.mp4", root_response.text)
            self.assertIn('class="video-placeholder"', root_response.text)
            self.assertIn("자막 미완료", root_response.text)
            self.assertNotIn("folder-glyph", root_response.text)
            self.assertEqual(response.status_code, 200)
            self.assertIn('name="source_rels"', response.text)
            self.assertIn('name="return_folder" value="show"', response.text)
            self.assertIn("첫 번째 에피소드", response.text)
            self.assertIn("자막 완료", response.text)
            self.assertIn("일본어 구두점 모델 사용", response.text)
            self.assertEqual(poster.status_code, 200)
            self.assertEqual(poster.content, b"poster-bytes")

    def test_batch_submission_queues_all_selected_files(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            (media_root / "one.mkv").write_bytes(b"media")
            (media_root / "two.mp4").write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                response = client.post(
                    "/jobs",
                    data={
                        "source_rels": ["one.mkv", "two.mp4"],
                        "duration_seconds": "0",
                    },
                    follow_redirects=False,
                )
                jobs = client.get("/api/jobs").json()

            self.assertEqual(response.status_code, 303)
            self.assertEqual(response.headers["location"], "/?queued=2")
            self.assertEqual(
                {job["source_rel"] for job in jobs},
                {"one.mkv", "two.mp4"},
            )
            self.assertTrue(
                all(job["created_at"].endswith("+09:00") for job in jobs)
            )
            self.assertTrue(
                all(job["updated_at"].endswith("+09:00") for job in jobs)
            )

    def test_completed_job_streams_video_range_and_webvtt(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "movie.mp4"
            source.write_bytes(b"0123456789")
            subtitle = media_root / "movie.ko.srt"
            subtitle.write_text(
                "1\n"
                "00:00:01,000 --> 00:00:02,500\n"
                "처리 결과\n",
                encoding="utf-8",
            )

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                job = service.create_job(
                    "movie.mp4",
                    force_overwrite=True,
                    options={},
                )
                service.store.update(
                    job.id,
                    status="completed",
                    srt_path=str(subtitle),
                )

                page = client.get(f"/jobs/{job.id}")
                video = client.get(
                    f"/jobs/{job.id}/video",
                    headers={"Range": "bytes=2-5"},
                )
                invalid_range = client.get(
                    f"/jobs/{job.id}/video",
                    headers={"Range": "bytes=99-"},
                )
                captions = client.get(f"/jobs/{job.id}/subtitles.vtt")

            self.assertEqual(page.status_code, 200)
            self.assertIn("KST", page.text)
            self.assertIn('class="result-player"', page.text)
            self.assertIn('data-video-type="video/mp4"', page.text)
            self.assertIn("data-subtitle-src=", page.text)
            self.assertNotIn("<source", page.text)
            self.assertEqual(video.status_code, 206)
            self.assertEqual(video.content, b"2345")
            self.assertEqual(video.headers["accept-ranges"], "bytes")
            self.assertEqual(
                video.headers["content-range"],
                "bytes 2-5/10",
            )
            self.assertEqual(invalid_range.status_code, 416)
            self.assertEqual(
                invalid_range.headers["content-range"],
                "bytes */10",
            )
            self.assertEqual(captions.status_code, 200)
            self.assertIn("text/vtt", captions.headers["content-type"])
            self.assertIn(
                "00:00:01.000 --> 00:00:02.500",
                captions.text,
            )
            self.assertIn("처리 결과", captions.text)

    def test_edits_source_named_json_and_shows_chunk_progress(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            source = media_root / "movie.mp4"
            source.write_bytes(b"media")

            with TestClient(
                create_app(self.settings(root, media_root))
            ) as client:
                service = client.app.state.orchestrator
                service.stop()
                job = service.create_job(
                    "movie.mp4",
                    force_overwrite=True,
                    options={},
                )
                artifact_dir = root / "state" / "jobs" / job.id
                artifact_dir.mkdir(parents=True, exist_ok=True)
                transcript = artifact_dir / "movie_translate.json"
                translation = artifact_dir / "movie_result_ko.json"
                transcript.write_text(
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
                translation.write_text(
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
                service.store.update(
                    job.id,
                    status="completed",
                    transcript_path=str(transcript),
                    translation_path=str(translation),
                    chunks_created=21,
                    chunks_completed=20,
                    chunk_progress_every=10,
                )

                page = client.get(f"/jobs/{job.id}")
                editor = client.get(
                    f"/jobs/{job.id}/artifacts/translation/edit"
                )
                invalid = client.post(
                    f"/jobs/{job.id}/artifacts/translation/edit",
                    data={"content": "{not-json"},
                )
                saved = client.post(
                    f"/jobs/{job.id}/artifacts/translation/edit",
                    data={
                        "content": json.dumps(
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
                    },
                    follow_redirects=False,
                )
                captions = client.get(f"/jobs/{job.id}/subtitles.vtt")
                styled_subtitle = client.get(
                    f"/jobs/{job.id}/subtitle.ass"
                )
                refreshed_page = client.get(f"/jobs/{job.id}")

            self.assertIn("20", page.text)
            self.assertIn("21 생성", page.text)
            self.assertIn("1 진행·대기", page.text)
            self.assertIn("한국어 결과 JSON 편집", page.text)
            self.assertEqual(editor.status_code, 200)
            self.assertIn("movie_result_ko.json", editor.text)
            self.assertIn("json-editor.js", editor.text)
            self.assertEqual(invalid.status_code, 400)
            self.assertIn("JSON syntax error", invalid.text)
            self.assertEqual(saved.status_code, 303)
            self.assertIn(
                "<c.speaker-1>수정된 번역</c>",
                captions.text,
            )
            self.assertNotIn("화자 1", captions.text)
            self.assertEqual(styled_subtitle.status_code, 200)
            self.assertIn("text/x-ssa", styled_subtitle.headers["content-type"])
            self.assertIn("[V4+ Styles]", styled_subtitle.text)
            self.assertIn("스타일 ASS 다운로드", refreshed_page.text)
            self.assertIn(
                "수정된 번역",
                (media_root / "movie.ko.srt").read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
