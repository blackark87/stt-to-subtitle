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

    def test_rejects_parent_traversal(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory) / "media"
            root.mkdir()
            outside = Path(directory) / "outside.mkv"
            outside.write_bytes(b"media")

            with self.assertRaisesRegex(ValueError, "escapes"):
                MediaLibrary(root).resolve_file("../outside.mkv")

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
