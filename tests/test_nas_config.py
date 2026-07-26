import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stt_to_subtitle.nas_config import MediaLibrary, NASSettings


class MediaLibraryTests(unittest.TestCase):
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
