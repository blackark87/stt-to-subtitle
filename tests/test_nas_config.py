import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from stt_to_subtitle.nas_config import (
    MediaLibrary,
    NASSettings,
    RemoteServerSettings,
    probe_media_duration,
)


class MediaLibraryTests(unittest.TestCase):
    def test_recursively_lists_selected_folders_without_metadata_or_links(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            season = root / "Series" / "Season 1"
            season.mkdir(parents=True)
            (root / "Series" / "root.mkv").write_bytes(b"media")
            (season / "episode.mp4").write_bytes(b"media")
            metadata = root / "Series" / "@eaDir"
            metadata.mkdir()
            (metadata / "ignored.mp4").write_bytes(b"media")
            (season / "episode-trailer.mp4").write_bytes(b"media")
            outside = root / "Outside"
            outside.mkdir()
            (outside / "outside.mkv").write_bytes(b"media")
            try:
                (root / "Series" / "outside-link").symlink_to(
                    outside,
                    target_is_directory=True,
                )
            except OSError:
                pass

            files = MediaLibrary(root).list_media_recursive(["Series"])

            self.assertEqual(
                files,
                ["Series/root.mkv", "Series/Season 1/episode.mp4"],
            )

    def test_recursive_listing_rejects_an_over_limit_selection(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "folder"
            folder.mkdir()
            (folder / "one.mkv").write_bytes(b"media")
            (folder / "two.mkv").write_bytes(b"media")

            with self.assertRaisesRegex(ValueError, "초과"):
                MediaLibrary(root, maximum_files=1).list_media_recursive(
                    ["folder"]
                )

    def test_browses_only_the_entered_folder_without_recursive_scan(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            season = root / "Series" / "Season 1"
            season.mkdir(parents=True)
            (root / "root-video.mp4").write_bytes(b"media")
            (season / "episode-01.mkv").write_bytes(b"media")
            (season / "episode-01.ko.srt").write_text(
                "subtitle",
                encoding="utf-8",
            )
            (season / "episode-02.mkv").write_bytes(b"media")

            library = MediaLibrary(root)
            with patch("stt_to_subtitle.nas_config.os.walk") as walk:
                root_view = library.browse()
            walk.assert_not_called()
            series_view = library.browse("Series")
            season_view = library.browse("Series/Season 1")

            self.assertEqual(
                [folder["name"] for folder in root_view["folders"]],
                ["Series"],
            )
            self.assertNotIn("video_count", root_view["folders"][0])
            self.assertEqual(
                [file["name"] for file in root_view["files"]],
                ["root-video.mp4"],
            )
            self.assertEqual(
                [folder["name"] for folder in series_view["folders"]],
                ["Season 1"],
            )
            self.assertEqual(
                [item["name"] for item in series_view["breadcrumbs"]],
                ["미디어 루트", "Series"],
            )
            self.assertEqual(series_view["parent_folder"], "")
            self.assertEqual(
                [file["name"] for file in season_view["files"]],
                ["episode-01.mkv", "episode-02.mkv"],
            )
            self.assertTrue(season_view["files"][0]["has_subtitle"])

    def test_excludes_synology_and_desktop_metadata_entries(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            visible = root / "Visible"
            visible.mkdir()
            (visible / "movie.mkv").write_bytes(b"media")
            metadata = root / "@eaDir" / "Visible"
            metadata.mkdir(parents=True)
            (metadata / "thumbnail.mp4").write_bytes(b"metadata")
            recycle = root / "#recycle"
            recycle.mkdir()
            (recycle / "deleted.mkv").write_bytes(b"deleted")
            (root / ".DS_Store").write_bytes(b"metadata")

            library = MediaLibrary(root)
            browser = library.browse()

            self.assertEqual(
                [folder["name"] for folder in browser["folders"]],
                ["Visible"],
            )
            visible_view = library.browse("Visible")
            self.assertEqual(
                [file["name"] for file in visible_view["files"]],
                ["movie.mkv"],
            )
            with self.assertRaisesRegex(ValueError, "metadata"):
                library.browse("@eaDir")
            with self.assertRaisesRegex(ValueError, "metadata"):
                library.resolve_file("@eaDir/Visible/thumbnail.mp4")

    def test_hides_trailer_mp4_files_from_listing(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "movie.mp4",
                "movie-trailer.mp4",
                "MOVIE-TRAILER.MP4",
                "movie-trailer.mkv",
                "movie-trailer-cut.mp4",
            ):
                (root / name).write_bytes(b"media")

            files = MediaLibrary(root).browse()["files"]

            self.assertEqual(
                [file["name"] for file in files],
                [
                    "movie-trailer-cut.mp4",
                    "movie-trailer.mkv",
                    "movie.mp4",
                ],
            )

    def test_probes_and_caches_media_duration(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "movie.mkv"
            media.write_bytes(b"media")
            probed_paths: list[Path] = []

            def probe(path: Path) -> float:
                probed_paths.append(path)
                return 6180.4

            library = MediaLibrary(root, duration_probe=probe)

            first = library.browse()["files"][0]
            second = library.browse()["files"][0]

            self.assertEqual(first["duration_seconds"], 6180.4)
            self.assertEqual(second["duration_seconds"], 6180.4)
            self.assertEqual(probed_paths, [media.resolve()])

    def test_ffprobe_duration_failure_does_not_break_listing(self) -> None:
        with TemporaryDirectory() as directory:
            media = Path(directory) / "movie.mkv"
            media.write_bytes(b"media")
            completed = SimpleNamespace(
                returncode=1,
                stdout="",
            )

            with patch(
                "stt_to_subtitle.nas_config.subprocess.run",
                return_value=completed,
            ):
                duration = probe_media_duration(media)

            self.assertIsNone(duration)

    def test_reads_duration_from_ffprobe_output(self) -> None:
        with TemporaryDirectory() as directory:
            media = Path(directory) / "movie.mkv"
            media.write_bytes(b"media")
            completed = SimpleNamespace(
                returncode=0,
                stdout="6180.4\n",
            )

            with patch(
                "stt_to_subtitle.nas_config.subprocess.run",
                return_value=completed,
            ) as run:
                duration = probe_media_duration(media)

            self.assertEqual(duration, 6180.4)
            self.assertEqual(run.call_args.args[0][-1], str(media))

    def test_lists_supported_media_and_detects_subtitle(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "folder" / "movie.mkv"
            media.parent.mkdir()
            media.write_bytes(b"media")
            media.with_name("movie.ko.srt").write_text("subtitle")

            files = MediaLibrary(root).browse("folder")["files"]

            self.assertEqual(files[0]["path"], "folder/movie.mkv")
            self.assertTrue(files[0]["has_subtitle"])
            self.assertFalse(files[0]["has_nfo"])
            self.assertIsNone(files[0]["poster_path"])
            self.assertNotIn("directory", files[0])

    def test_ass_file_alone_marks_media_as_subtitled(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.mkv").write_bytes(b"media")
            (root / "sample.ko.ass").write_text(
                "[Script Info]\n",
                encoding="utf-8",
            )

            files = MediaLibrary(root).browse()["files"]

            self.assertEqual(len(files), 1)
            self.assertTrue(files[0]["has_subtitle"])

    def test_reads_nfo_title_and_local_poster(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "folder"
            folder.mkdir()
            (folder / "movie.mkv").write_bytes(b"media")
            (folder / "movie.nfo").write_text(
                """
                <movie>
                  <title>테스트 영화</title>
                  <thumb aspect="poster">art/movie-poster.jpg</thumb>
                </movie>
                """,
                encoding="utf-8",
            )
            poster = folder / "art" / "movie-poster.jpg"
            poster.parent.mkdir()
            poster.write_bytes(b"poster")

            library = MediaLibrary(root)
            files = library.browse("folder")["files"]

            self.assertTrue(files[0]["has_nfo"])
            self.assertEqual(files[0]["title"], "테스트 영화")
            self.assertEqual(
                files[0]["poster_path"],
                "folder/art/movie-poster.jpg",
            )
            self.assertEqual(
                library.resolve_poster("folder/art/movie-poster.jpg"),
                poster.resolve(),
            )

    def test_malformed_nfo_does_not_hide_media(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "movie.mkv").write_bytes(b"media")
            (root / "movie.nfo").write_text("<movie>", encoding="utf-8")

            files = MediaLibrary(root).browse()["files"]

            self.assertEqual(len(files), 1)
            self.assertTrue(files[0]["has_nfo"])
            self.assertEqual(files[0]["title"], "movie")
            self.assertIsNone(files[0]["poster_path"])

    def test_ignores_nfo_poster_outside_media_root(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "media"
            root.mkdir()
            (root / "movie.mkv").write_bytes(b"media")
            (root / "movie.nfo").write_text(
                "<movie><poster>../outside.jpg</poster></movie>",
                encoding="utf-8",
            )
            (base / "outside.jpg").write_bytes(b"poster")

            files = MediaLibrary(root).browse()["files"]

            self.assertIsNone(files[0]["poster_path"])

    def test_rejects_parent_traversal(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            outside = Path(directory) / "outside.mkv"
            outside.write_bytes(b"media")

            with self.assertRaisesRegex(ValueError, "escapes"):
                MediaLibrary(root).resolve_file("../outside.mkv")

            with self.assertRaisesRegex(ValueError, "escapes"):
                MediaLibrary(root).browse("../")

    def test_rejects_poster_parent_traversal(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            outside = Path(directory) / "outside.jpg"
            outside.write_bytes(b"poster")

            with self.assertRaisesRegex(ValueError, "escapes"):
                MediaLibrary(root).resolve_poster("../outside.jpg")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink is unavailable")
    def test_rejects_symlink_to_file_outside_root(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            outside = Path(directory) / "outside.mkv"
            outside.write_bytes(b"media")
            (root / "linked.mkv").symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "escapes"):
                MediaLibrary(root).resolve_file("linked.mkv")


class NASSettingsTests(unittest.TestCase):
    def test_reads_openai_compatible_environment_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OPENAI_COMPATIBLE_BASE_URL": "http://translation.test/v1",
                "OPENAI_COMPATIBLE_TOKEN": "token",
                "OPENAI_COMPATIBLE_MODEL": "model",
            },
            clear=True,
        ):
            settings = NASSettings.from_env()

        self.assertEqual(
            settings.lm_base_url,
            "http://translation.test/v1",
        )
        self.assertEqual(settings.lm_token, "token")
        self.assertEqual(settings.lm_model, "model")

    def test_keeps_legacy_lm_studio_environment_fallback(self) -> None:
        with patch.dict(
            os.environ,
            {
                "LM_STUDIO_BASE_URL": "http://legacy.test/v1",
                "LM_STUDIO_TOKEN": "legacy-token",
                "LM_STUDIO_MODEL": "legacy-model",
            },
            clear=True,
        ):
            settings = NASSettings.from_env()

        self.assertEqual(settings.lm_base_url, "http://legacy.test/v1")
        self.assertEqual(settings.lm_token, "legacy-token")
        self.assertEqual(settings.lm_model, "legacy-model")

    def test_allows_server_configuration_after_startup(self) -> None:
        settings = NASSettings(
            state_dir=Path("/state"),
            media_root=Path("/media"),
            admin_password="",
            session_secret="",
            stt_base_url="",
            stt_token="",
            lm_base_url="",
            lm_token="",
            lm_model="",
        )

        settings.validate()

    def test_allows_blank_credentials_on_trusted_network(self) -> None:
        settings = NASSettings(
            state_dir=Path("/state"),
            media_root=Path("/media"),
            admin_password="",
            session_secret="",
            stt_base_url="http://stt.test",
            stt_token="",
            lm_base_url="http://lm.test/v1",
            lm_token="",
            lm_model="model",
        )

        settings.validate()

    def test_web_password_requires_session_secret(self) -> None:
        settings = NASSettings(
            state_dir=Path("/state"),
            media_root=Path("/media"),
            admin_password="password",
            session_secret="",
            stt_base_url="http://stt.test",
            stt_token="",
            lm_base_url="http://lm.test/v1",
            lm_token="",
            lm_model="model",
        )

        with self.assertRaisesRegex(ValueError, "NAS_SESSION_SECRET"):
            settings.validate()

    def test_normalizes_remote_server_urls(self) -> None:
        settings = RemoteServerSettings(
            stt_base_url=" http://stt.test/ ",
            stt_token="stt-token",
            lm_base_url="http://lm.test/v1/",
            lm_token="lm-token",
            lm_model=" model ",
        )

        normalized = settings.normalized()

        self.assertEqual(normalized.stt_base_url, "http://stt.test")
        self.assertEqual(normalized.lm_base_url, "http://lm.test/v1")
        self.assertEqual(normalized.lm_model, "model")

    def test_rejects_invalid_remote_server_url(self) -> None:
        settings = RemoteServerSettings(
            stt_base_url="file:///tmp/stt",
            stt_token="",
            lm_base_url="http://lm.test/v1",
            lm_token="",
            lm_model="model",
        )

        with self.assertRaisesRegex(ValueError, "STT_BASE_URL"):
            settings.normalized()

    def test_translation_worker_count_must_be_between_one_and_eight(self) -> None:
        for workers in (0, 9):
            settings = RemoteServerSettings(
                stt_base_url="http://stt.test",
                stt_token="",
                lm_base_url="http://lm.test/v1",
                lm_token="",
                lm_model="model",
                translation_workers=workers,
            )
            with self.assertRaisesRegex(ValueError, "between 1 and 8"):
                settings.normalized()
