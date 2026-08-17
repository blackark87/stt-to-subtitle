import unittest

from stt_to_subtitle.whisperx_worker import (
    WhisperXSegmentationOptions,
    extract_whisperx_words,
    install_whisperx_sentence_splitter_fallback,
    normalize_whisperx_segments,
    rebuild_whisperx_segments,
)


class WhisperXWorkerTests(unittest.TestCase):
    def test_uses_whole_segment_when_punkt_data_is_unavailable(self) -> None:
        other_resource = object()

        class AlignmentModule:
            @staticmethod
            def nltk_load(resource: str) -> object:
                if resource.startswith("tokenizers/punkt_tab/"):
                    raise LookupError("punkt_tab is unavailable")
                return other_resource

        install_whisperx_sentence_splitter_fallback(AlignmentModule)

        tokenizer = AlignmentModule.nltk_load(
            "tokenizers/punkt_tab/english.pickle"
        )
        text = "一文目です。二文目です。"

        self.assertEqual(
            list(tokenizer.span_tokenize(text)),
            [(0, len(text))],
        )
        self.assertIs(
            AlignmentModule.nltk_load("tokenizers/other/resource"),
            other_resource,
        )

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

    def test_preserves_word_speaker_score_and_parent_segment(self) -> None:
        words = extract_whisperx_words(
            [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "speaker": "SPEAKER_00",
                    "words": [
                        {
                            "word": "はい",
                            "start": 0.1,
                            "end": 0.4,
                            "score": 0.9,
                            "speaker": "SPEAKER_01",
                        }
                    ],
                }
            ]
        )

        self.assertEqual(words[0]["word_id"], "word-000001")
        self.assertEqual(words[0]["speaker"], "SPEAKER_01")
        self.assertEqual(words[0]["score"], 0.9)
        self.assertFalse(words[0]["timestamp_fallback"])
        self.assertEqual(words[0]["timestamp_source"], "word_alignment")
        self.assertEqual(
            words[0]["parent_span_ids"],
            ["whisperx-segment-000001"],
        )

    def test_marks_parent_segment_timestamp_fallback(self) -> None:
        words = extract_whisperx_words(
            [
                {
                    "start": 1.0,
                    "end": 5.0,
                    "speaker": "SPEAKER_00",
                    "words": [{"word": "時刻なし"}],
                }
            ]
        )

        self.assertEqual((words[0]["start"], words[0]["end"]), (1.0, 5.0))
        self.assertTrue(words[0]["timestamp_fallback"])
        self.assertEqual(
            words[0]["timestamp_source"], "parent_segment_fallback"
        )

    def test_rebuild_splits_when_speaker_changes(self) -> None:
        words = [
            {
                "word_id": "word-000001",
                "word": "はい",
                "start": 0.0,
                "end": 0.4,
                "speaker": "A",
            },
            {
                "word_id": "word-000002",
                "word": "そう",
                "start": 0.5,
                "end": 0.9,
                "speaker": "B",
            },
        ]

        segments = rebuild_whisperx_segments(
            words,
            WhisperXSegmentationOptions(prefer_punctuation_boundary=False),
        )

        self.assertEqual([item["text"] for item in segments], ["はい", "そう"])
        self.assertEqual(segments[0]["word_ids"], ["word-000001"])

    def test_rebuild_honors_gap_duration_and_character_limits(self) -> None:
        words = [
            {
                "word_id": f"word-{index:06d}",
                "word": text,
                "start": start,
                "end": end,
                "speaker": "A",
            }
            for index, (text, start, end) in enumerate(
                [
                    ("一二", 0.0, 0.5),
                    ("三四", 0.6, 1.1),
                    ("五六", 2.0, 2.5),
                ],
                start=1,
            )
        ]

        by_gap = rebuild_whisperx_segments(
            words,
            WhisperXSegmentationOptions(
                max_gap_sec=0.5,
                prefer_punctuation_boundary=False,
            ),
        )
        by_duration = rebuild_whisperx_segments(
            words[:2],
            WhisperXSegmentationOptions(
                max_duration_sec=1.0,
                prefer_punctuation_boundary=False,
            ),
        )
        by_chars = rebuild_whisperx_segments(
            words[:2],
            WhisperXSegmentationOptions(
                max_chars=3,
                prefer_punctuation_boundary=False,
            ),
        )

        self.assertEqual(len(by_gap), 2)
        self.assertEqual(len(by_duration), 2)
        self.assertEqual(len(by_chars), 2)


if __name__ == "__main__":
    unittest.main()
