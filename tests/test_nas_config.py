import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stt_to_subtitle.nas_config import MediaLibrary, NASSettings


class MediaLibraryTests(unittest.TestCase):
    def test_lists_supported_media_and_detects_subtitle(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "folder" / "movie.mkv"
            media.parent.mkdir()
            media.write_bytes(b"media")
            media.with_name("movie.ko.srt").write_text("subtitle")

            files = MediaLibrary(root).list_files()

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
            files = library.list_files()

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

            files = MediaLibrary(root).list_files()

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

            files = MediaLibrary(root).list_files()

            self.assertIsNone(files[0]["poster_path"])

    def test_rejects_parent_traversal(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            outside = Path(directory) / "outside.mkv"
            outside.write_bytes(b"media")

            with self.assertRaisesRegex(ValueError, "escapes"):
                MediaLibrary(root).resolve_file("../outside.mkv")

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
