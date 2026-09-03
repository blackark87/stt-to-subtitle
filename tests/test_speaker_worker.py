import unittest

from stt_to_subtitle.speaker_worker import assign_word_speakers


class SpeakerWorkerTests(unittest.TestCase):
    def test_assigns_maximum_overlap_and_nearest_fallback(self) -> None:
        words, fallback_count = assign_word_speakers(
            [
                {"word": "あ", "start": 0.2, "end": 0.7},
                {"word": "い", "start": 3.0, "end": 3.2},
            ],
            [
                {"speaker": "A", "start": 0.0, "end": 0.4},
                {"speaker": "B", "start": 0.4, "end": 1.0},
                {"speaker": "B", "start": 2.0, "end": 2.5},
            ],
        )

        self.assertEqual(words[0]["speaker"], "B")
        self.assertEqual(words[0]["speaker_source"], "pyannote_word_overlap")
        self.assertEqual(words[1]["speaker"], "B")
        self.assertEqual(
            words[1]["speaker_source"], "pyannote_nearest_fallback"
        )
        self.assertEqual(fallback_count, 1)

    def test_marks_unknown_when_diarization_is_empty(self) -> None:
        words, fallback_count = assign_word_speakers(
            [{"word": "あ", "start": 0.0, "end": 0.5}],
            [],
        )

        self.assertEqual(words[0]["speaker"], "UNKNOWN")
        self.assertEqual(words[0]["speaker_source"], "diarization_unavailable")
        self.assertEqual(fallback_count, 1)

if __name__ == "__main__":
    unittest.main()
