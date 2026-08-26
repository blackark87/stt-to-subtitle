import unittest

from stt_to_subtitle.translation_comparison import compare_translation_items


class TranslationComparisonTests(unittest.TestCase):
    def test_compares_added_removed_changed_and_unchanged_segments(self) -> None:
        comparison = compare_translation_items(
            [
                {
                    "id": "segment-1",
                    "text": "이전 번역",
                    "segment_index": 0,
                    "source_hash": "source-1",
                },
                {
                    "id": "segment-2",
                    "text": "삭제될 번역",
                    "segment_index": 1,
                    "source_hash": "source-2",
                },
                {
                    "id": "segment-4",
                    "text": "같은 번역",
                    "segment_index": 3,
                    "source_hash": "source-4",
                },
            ],
            [
                {
                    "id": "segment-1",
                    "text": "새 번역",
                    "segment_index": 0,
                    "source_hash": "source-1-changed",
                },
                {
                    "id": "segment-3",
                    "text": "추가된 번역",
                    "segment_index": 2,
                    "source_hash": "source-3",
                },
                {
                    "id": "segment-4",
                    "text": "같은 번역",
                    "segment_index": 3,
                    "source_hash": "source-4",
                },
            ],
            base_source_texts={"segment-1": "古い原文"},
            candidate_source_texts={"segment-1": "新しい原文"},
        )

        self.assertEqual(
            [row["state"] for row in comparison["rows"]],
            ["changed", "removed", "added", "unchanged"],
        )
        self.assertEqual(comparison["total_count"], 4)
        self.assertEqual(comparison["change_count"], 3)
        self.assertEqual(comparison["changed_count"], 1)
        self.assertEqual(comparison["removed_count"], 1)
        self.assertEqual(comparison["added_count"], 1)
        self.assertEqual(comparison["unchanged_count"], 1)
        self.assertEqual(comparison["source_changed_count"], 1)
        first = comparison["rows"][0]
        self.assertTrue(first["source_changed"])
        self.assertEqual(first["base_source_text"], "古い原文")
        self.assertEqual(first["candidate_source_text"], "新しい原文")

    def test_source_change_is_included_when_translation_is_unchanged(self) -> None:
        comparison = compare_translation_items(
            [
                {
                    "id": "segment-1",
                    "text": "같은 번역",
                    "segment_index": 0,
                    "source_hash": "source-v1",
                }
            ],
            [
                {
                    "id": "segment-1",
                    "text": "같은 번역",
                    "segment_index": 0,
                    "source_hash": "source-v2",
                }
            ],
        )

        self.assertEqual(comparison["unchanged_count"], 1)
        self.assertEqual(comparison["source_changed_count"], 1)
        self.assertEqual(comparison["change_count"], 1)
        self.assertTrue(comparison["rows"][0]["has_change"])

    def test_rejects_duplicate_or_empty_translation_items(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid base"):
            compare_translation_items(
                [
                    {"id": "segment-1", "text": "하나"},
                    {"id": "segment-1", "text": "둘"},
                ],
                [],
            )
        with self.assertRaisesRegex(ValueError, "invalid candidate"):
            compare_translation_items(
                [],
                [{"id": "segment-1", "text": ""}],
            )


if __name__ == "__main__":
    unittest.main()
