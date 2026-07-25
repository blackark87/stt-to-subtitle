from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stt_to_subtitle.subtitle import (
    format_srt_timestamp,
    render_srt,
    write_srt_atomic,
)


class SubtitleTests(unittest.TestCase):
    def test_renders_korean_text_without_speaker_labels(self) -> None:
        rendered = render_srt(
            [
                {
                    "id": "segment-000001",
                    "start": 3661.234,
                    "end": 3662.5,
                    "speaker": "SPEAKER_00",
                    "text": "こんにちは",
                }
            ],
            [{"id": "segment-000001", "text": "안녕하세요"}],
        )

        self.assertIn("01:01:01,234 --> 01:01:02,500", rendered)
        self.assertIn("안녕하세요", rendered)
        self.assertNotIn("SPEAKER_00", rendered)

    def test_refuses_existing_srt_without_force(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "sample.ko.srt"
            path.write_text("existing", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                write_srt_atomic(path, [], [], overwrite=False)

            self.assertEqual(path.read_text(encoding="utf-8"), "existing")

    def test_timestamp_never_becomes_negative(self) -> None:
        self.assertEqual(format_srt_timestamp(-1), "00:00:00,000")
