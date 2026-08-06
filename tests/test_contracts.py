import unittest

from stt_to_subtitle.contracts import (
    add_segment_ids,
    validate_transcript,
    validate_translation_items,
)


class ContractTests(unittest.TestCase):
    def test_adds_deterministic_segment_ids(self) -> None:
        segments = add_segment_ids(
            [
                {
                    "start": 0,
                    "end": 1,
                    "speaker": "SPEAKER_00",
                    "text": "こんにちは",
                },
                {
                    "start": 1,
                    "end": 2,
                    "speaker": "SPEAKER_01",
                    "text": "世界",
                },
            ]
        )

        self.assertEqual(
            [segment["id"] for segment in segments],
            ["segment-000001", "segment-000002"],
        )

    def test_validates_transcript_contract(self) -> None:
        segments = validate_transcript(
            {
                "schema_version": 1,
                "segments": [
                    {
                        "id": "segment-000001",
                        "start": 0,
                        "end": 1.5,
                        "speaker": "SPEAKER_00",
                        "text": "こんにちは",
                    }
                ],
            }
        )

        self.assertEqual(segments[0]["end"], 1.5)

    def test_segment_ids_preserve_word_lineage_without_changing_translation_id(
        self,
    ) -> None:
        segment = add_segment_ids(
            [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "speaker": "A",
                    "text": "はい",
                    "word_ids": ["word-000001"],
                    "parent_span_ids": ["word-000001"],
                    "source_segment_id": "kotoba-segment-000001",
                    "rescue_window_id": "rescue-window-000001",
                }
            ]
        )[0]

        self.assertEqual(segment["id"], "segment-000001")
        self.assertEqual(segment["span_id"], "segment-000001")
        self.assertEqual(segment["word_ids"], ["word-000001"])
        self.assertEqual(segment["parent_span_ids"], ["word-000001"])
        self.assertEqual(
            segment["source_segment_id"], "kotoba-segment-000001"
        )
        self.assertEqual(
            segment["rescue_window_id"], "rescue-window-000001"
        )

    def test_translation_ids_must_match_in_order(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly match"):
            validate_translation_items(
                [
                    {"id": "segment-000002", "text": "둘"},
                    {"id": "segment-000001", "text": "하나"},
                ],
                ["segment-000001", "segment-000002"],
            )
