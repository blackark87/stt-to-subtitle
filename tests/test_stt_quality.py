from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from stt_to_subtitle.stt_quality import (
    annotate_span_diagnostics,
    find_replacement_chars,
    interval_durations,
    normalize_transcript,
    overlap_duplicate_metrics,
    repetition_diagnostics,
    transcript_similarity,
)
from stt_to_subtitle.stt_trace import StageArtifactRecorder


class STTQualityTests(unittest.TestCase):
    def test_normalization_removes_unicode_punctuation_but_keeps_long_mark(
        self,
    ) -> None:
        self.assertEqual(normalize_transcript(" スーパー、です。 "), "スーパーです")

    def test_similarity_does_not_apply_sequence_matcher_autojunk(self) -> None:
        with patch("stt_to_subtitle.stt_quality.SequenceMatcher") as matcher:
            matcher.return_value.ratio.return_value = 0.75

            result = transcript_similarity("基準", "候補")

        self.assertEqual(result, 0.75)
        self.assertFalse(matcher.call_args.kwargs["autojunk"])

    def test_interval_sum_and_union_distinguish_overlapping_spans(self) -> None:
        duration_sum, duration_union = interval_durations(
            [
                {"start": 0.0, "end": 2.0},
                {"start": 1.0, "end": 3.0},
            ]
        )

        self.assertEqual(duration_sum, 4.0)
        self.assertEqual(duration_union, 3.0)

    def test_span_diagnostics_keep_lineage_and_describe_short_neighbors(self) -> None:
        diagnostics = annotate_span_diagnostics(
            [
                {
                    "span_id": "dia-000001",
                    "start": 0.0,
                    "end": 0.1,
                    "speaker": "A",
                },
                {
                    "span_id": "dia-000002",
                    "start": 0.15,
                    "end": 1.0,
                    "speaker": "A",
                },
            ]
        )

        self.assertEqual(diagnostics[0]["span_id"], "dia-000001")
        self.assertTrue(diagnostics[0]["is_short_span"])
        self.assertEqual(diagnostics[0]["short_span_bucket"], "lt_0_2")
        self.assertEqual(diagnostics[0]["next_gap_sec"], 0.05)
        self.assertTrue(diagnostics[0]["same_speaker_next"])
        self.assertEqual(diagnostics[1]["short_span_bucket"], "gte_0_5")

    def test_flags_large_consecutive_japanese_ngram_repetition(self) -> None:
        diagnostics = repetition_diagnostics(["いえ" * 102])

        self.assertTrue(diagnostics["flagged"])
        self.assertEqual(diagnostics["max_repeated_ngram"], "いえ")
        self.assertEqual(diagnostics["max_repeated_ngram_count"], 102)

    def test_overlap_duplicates_report_pairs_and_connected_components(self) -> None:
        metrics = overlap_duplicate_metrics(
            [
                {
                    "id": "one",
                    "start": 0.0,
                    "end": 2.0,
                    "speaker": "A",
                    "text": "同じ文章",
                },
                {
                    "id": "two",
                    "start": 1.0,
                    "end": 3.0,
                    "speaker": "A",
                    "text": "同じ文章",
                },
                {
                    "id": "three",
                    "start": 1.5,
                    "end": 2.5,
                    "speaker": "B",
                    "text": "同じ文章",
                },
                {
                    "id": "four",
                    "start": 4.0,
                    "end": 5.0,
                    "speaker": "B",
                    "text": "別の文",
                },
            ]
        )

        self.assertEqual(metrics["pair_count"], 3)
        self.assertEqual(metrics["cluster_count"], 1)
        self.assertEqual(metrics["unique_segment_count"], 2)
        self.assertEqual(metrics["same_speaker_pair_count"], 1)
        self.assertEqual(metrics["cross_speaker_pair_count"], 2)

    def test_stage_recorder_keeps_replacement_character_and_first_stage(self) -> None:
        with TemporaryDirectory() as directory:
            recorder = StageArtifactRecorder(Path(directory))
            recorder.record("one.json", "aligned", {"text": "前\ufffd後"})
            recorder.record("two.json", "final", {"text": "前\ufffd後"})

            warning = recorder.encoding_warning()
            saved = json.loads(
                (Path(directory) / "one.json").read_text(encoding="utf-8")
            )

        self.assertEqual(find_replacement_chars("前\ufffd後"), [1])
        self.assertEqual(warning["first_detected_stage"], "aligned")
        self.assertEqual(saved["payload"]["text"], "前\ufffd後")


if __name__ == "__main__":
    unittest.main()
