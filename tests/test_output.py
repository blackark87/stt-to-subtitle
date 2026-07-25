import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from stt_to_subtitle.output import format_timestamp, write_outputs


class OutputTests(unittest.TestCase):
    def test_formats_timestamp_beyond_one_hour(self) -> None:
        self.assertEqual(format_timestamp(3661.234), "01:01:01.234")

    def test_writes_utf8_json_and_text(self) -> None:
        with TemporaryDirectory() as directory:
            base = Path(directory) / "sample.stt"
            segments = [
                {
                    "start": 1.0,
                    "end": 2.5,
                    "speaker": "SPEAKER_00",
                    "text": "こんにちは",
                }
            ]

            json_path, text_path = write_outputs(
                base,
                {"source": "/data/sample.mkv"},
                segments,
                {"SPEAKER_00": "こんにちは"},
            )

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(json_path.name, "sample.stt.json")
            self.assertEqual(text_path.name, "sample.stt.txt")
            self.assertEqual(payload["segments"], segments)
            self.assertIn("こんにちは", text_path.read_text(encoding="utf-8"))
