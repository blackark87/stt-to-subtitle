import unittest

from stt_to_subtitle.whisperx_worker import (
    normalize_whisperx_segments,
)


class WhisperXWorkerTests(unittest.TestCase):
    def test_normalizes_sorts_and_filters_aligned_segments(self) -> None:
        segments = normalize_whisperx_segments(
            [
                {
                    "start": 2.3456,
                    "end": 3.4567,
                    "speaker": "SPEAKER_01",
                    "text": " 後です ",
                },
                {
                    "start": -0.1,
                    "end": 1.2345,
                    "speaker": "SPEAKER_00",
                    "text": "最初です",
                },
                {"start": 1.0, "end": 2.0, "text": "   "},
                {"start": "invalid", "end": 4.0, "text": "除外"},
            ]
        )

        self.assertEqual(
            segments,
            [
                {
                    "start": 0.0,
                    "end": 1.234,
                    "speaker": "SPEAKER_00",
                    "text": "最初です",
                },
                {
                    "start": 2.346,
                    "end": 3.457,
                    "speaker": "SPEAKER_01",
                    "text": "後です",
                },
            ],
        )

    def test_missing_speaker_uses_unknown(self) -> None:
        segments = normalize_whisperx_segments(
            [{"start": 0.0, "end": 1.0, "text": "音声"}]
        )

        self.assertEqual(segments[0]["speaker"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
