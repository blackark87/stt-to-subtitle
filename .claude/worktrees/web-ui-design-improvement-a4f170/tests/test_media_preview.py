from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stt_to_subtitle.media_preview import (
    guess_media_type,
    iter_file_range,
    parse_byte_range,
    srt_to_webvtt,
)


class MediaPreviewTests(unittest.TestCase):
    def test_uses_stable_video_mime_types(self) -> None:
        self.assertEqual(guess_media_type("movie.mkv"), "video/x-matroska")
        self.assertEqual(guess_media_type("movie.mp4"), "video/mp4")

    def test_parses_open_ended_suffix_and_clamped_ranges(self) -> None:
        self.assertEqual(parse_byte_range("bytes=2-5", 10), (2, 5))
        self.assertEqual(parse_byte_range("bytes=7-", 10), (7, 9))
        self.assertEqual(parse_byte_range("bytes=-4", 10), (6, 9))
        self.assertEqual(parse_byte_range("bytes=8-99", 10), (8, 9))
        self.assertIsNone(parse_byte_range(None, 10))

    def test_rejects_unsupported_or_unsatisfiable_ranges(self) -> None:
        for value in (
            "items=0-1",
            "bytes=0-1,4-5",
            "bytes=10-",
            "bytes=5-2",
            "bytes=-0",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_byte_range(value, 10)

    def test_yields_only_requested_file_bytes(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"0123456789")

            content = b"".join(
                iter_file_range(path, 2, 7, chunk_size=2)
            )

            self.assertEqual(content, b"234567")

    def test_converts_srt_timestamps_and_escapes_cue_markup(self) -> None:
        webvtt = srt_to_webvtt(
            "1\n"
            "00:00:01,250 --> 00:00:03,500\n"
            "안녕하세요 <script>\n"
        )

        self.assertTrue(webvtt.startswith("WEBVTT\n\n"))
        self.assertIn(
            "00:00:01.250 --> 00:00:03.500",
            webvtt,
        )
        self.assertIn("안녕하세요 &lt;script&gt;", webvtt)


if __name__ == "__main__":
    unittest.main()
