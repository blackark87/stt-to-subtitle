import unittest

from stt_to_subtitle.hybrid_stt import (
    HybridRescueOptions,
    debounce_word_speakers,
    detect_hybrid_issues,
    fuse_hybrid_segments,
    map_fallback_speakers,
    mark_rescued_words,
    merge_issue_windows,
)


class HybridRescueOptionsTests(unittest.TestCase):
    def test_defaults_use_kotoba_15_and_whisperx_30_seconds(self) -> None:
        options = HybridRescueOptions.from_options({})

        self.assertEqual(options.kotoba_chunk_length_seconds, 15)
        self.assertEqual(options.whisperx_chunk_length_seconds, 30)
        self.assertEqual(options.window_padding_sec, 5.0)
        self.assertEqual(options.max_word_duration_sec, 8.0)
        self.assertEqual(options.speaker_debounce_sec, 0.1)

    def test_rejects_unknown_nested_option(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported hybrid_rescue"):
            HybridRescueOptions.from_options(
                {"hybrid_rescue": {"unknown": True}}
            )

    def test_rejects_non_finite_threshold(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            HybridRescueOptions.from_options(
                {"hybrid_rescue": {"max_word_duration_sec": "nan"}}
            )

    def test_rescue_scope_defaults_to_windows_and_accepts_full(self) -> None:
        self.assertEqual(
            HybridRescueOptions.from_options({}).rescue_scope,
            "windows",
        )
        self.assertEqual(
            HybridRescueOptions.from_options(
                {"hybrid_rescue": {"rescue_scope": "full"}}
            ).rescue_scope,
            "full",
        )

    def test_rejects_unknown_rescue_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "rescue_scope must be one of"):
            HybridRescueOptions.from_options(
                {"hybrid_rescue": {"rescue_scope": "partial"}}
            )

    def test_rejects_whisperx_chunks_longer_than_native_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be at most 30"):
            HybridRescueOptions.from_options(
                {
                    "hybrid_rescue": {
                        "whisperx_chunk_length_seconds": 31,
                    }
                }
            )


class HybridDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.options = HybridRescueOptions()

    def test_debounces_only_short_a_b_a_speaker_flash(self) -> None:
        words = [
            {
                "word_id": "word-1",
                "start": 0.0,
                "end": 1.0,
                "speaker": "A",
                "word": "前",
            },
            {
                "word_id": "word-2",
                "start": 1.0,
                "end": 1.2,
                "speaker": "B",
                "word": "中",
                "reason_codes": [],
            },
            {
                "word_id": "word-3",
                "start": 1.2,
                "end": 2.0,
                "speaker": "A",
                "word": "後",
            },
        ]

        normalized, changes = debounce_word_speakers(
            words,
            maximum_flash_duration_sec=0.4,
        )

        self.assertEqual([word["speaker"] for word in normalized], ["A"] * 3)
        self.assertEqual(normalized[1]["original_speaker"], "B")
        self.assertIn(
            "HYBRID_SPEAKER_DEBOUNCE", normalized[1]["reason_codes"]
        )
        self.assertEqual(changes[0]["word_ids"], ["word-2"])
        self.assertEqual(words[1]["speaker"], "B")

    def test_detects_cross_segment_repetition_and_long_word(self) -> None:
        segments = [
            {
                "id": f"segment-{index}",
                "start": index * 0.3,
                "end": index * 0.3 + 0.25,
                "speaker": "A",
                "text": "いえ",
            }
            for index in range(8)
        ]
        words = [
            {
                "word_id": "word-long",
                "start": 10.0,
                "end": 22.9,
                "word": "優",
            },
            {
                "word_id": "word-fallback",
                "start": 30.0,
                "end": 34.0,
                "word": "時刻なし",
                "timestamp_fallback": True,
                "timestamp_source": "parent_segment_fallback",
            },
        ]

        issues = detect_hybrid_issues(
            segments,
            words,
            options=self.options,
            repetition_min_count=8,
        )
        reasons = {
            code
            for issue in issues
            for code in issue["reason_codes"]
        }

        self.assertIn("REPEATED_TRANSCRIPT", reasons)
        self.assertIn("LONG_WORD_ALIGNMENT", reasons)
        self.assertIn("MISSING_WORD_TIMESTAMP", reasons)
        repetition = next(
            issue
            for issue in issues
            if "REPEATED_TRANSCRIPT" in issue["reason_codes"]
        )
        self.assertEqual(repetition["start"], 0.0)
        self.assertGreaterEqual(len(repetition["segment_ids"]), 8)

    def test_preserves_short_speaker_flash_when_words_really_overlap(self) -> None:
        words = [
            {
                "word_id": "word-1",
                "start": 0.0,
                "end": 1.1,
                "speaker": "A",
                "word": "前",
            },
            {
                "word_id": "word-2",
                "start": 1.0,
                "end": 1.2,
                "speaker": "B",
                "word": "겹침",
            },
            {
                "word_id": "word-3",
                "start": 1.2,
                "end": 2.0,
                "speaker": "A",
                "word": "後",
            },
        ]

        normalized, changes = debounce_word_speakers(
            words,
            maximum_flash_duration_sec=0.4,
        )

        self.assertEqual(
            [word["speaker"] for word in normalized], ["A", "B", "A"]
        )
        self.assertEqual(changes, [])

    def test_detects_replacement_character_and_micro_segment_cluster(self) -> None:
        segments = [
            {
                "id": "replacement",
                "start": 1.0,
                "end": 2.0,
                "speaker": "A",
                "text": "壊\ufffd",
            },
            {
                "id": "short-1",
                "start": 5.0,
                "end": 5.1,
                "speaker": "A",
                "text": "あ",
            },
            {
                "id": "short-2",
                "start": 5.5,
                "end": 5.6,
                "speaker": "B",
                "text": "い",
            },
            {
                "id": "short-3",
                "start": 6.0,
                "end": 6.1,
                "speaker": "A",
                "text": "う",
            },
        ]

        issues = detect_hybrid_issues(
            segments,
            [],
            options=self.options,
            repetition_min_count=8,
        )
        reasons = {
            code
            for issue in issues
            for code in issue["reason_codes"]
        }

        self.assertIn("REPLACEMENT_CHARACTER", reasons)
        self.assertIn("MICRO_SEGMENT_CLUSTER", reasons)

    def test_does_not_join_repetition_across_a_large_time_gap(self) -> None:
        segments = [
            {
                "id": f"segment-{index}",
                "start": start,
                "end": start + 0.2,
                "speaker": "A",
                "text": "いえ",
            }
            for index, start in enumerate(
                [0.0, 0.3, 0.6, 0.9, 10.0, 10.3, 10.6, 10.9]
            )
        ]

        issues = detect_hybrid_issues(
            segments,
            [],
            options=self.options,
            repetition_min_count=8,
        )

        self.assertFalse(
            any(
                "REPEATED_TRANSCRIPT" in issue["reason_codes"]
                for issue in issues
            )
        )

    def test_merges_padded_windows_and_clamps_to_audio(self) -> None:
        issues = [
            {
                "issue_id": "issue-1",
                "start": 1.0,
                "end": 2.0,
                "reason_codes": ["A"],
                "segment_ids": ["s1"],
                "word_ids": [],
            },
            {
                "issue_id": "issue-2",
                "start": 7.0,
                "end": 9.0,
                "reason_codes": ["B"],
                "segment_ids": ["s2"],
                "word_ids": [],
            },
        ]

        windows = merge_issue_windows(
            issues,
            padding_seconds=5.0,
            audio_duration=10.0,
        )

        self.assertEqual(len(windows), 1)
        self.assertEqual((windows[0]["start"], windows[0]["end"]), (0.0, 10.0))
        self.assertEqual(windows[0]["reason_codes"], ["A", "B"])

    def test_warning_is_reported_but_does_not_create_rescue_window(self) -> None:
        windows = merge_issue_windows(
            [
                {
                    "issue_id": "warning-1",
                    "start": 3.0,
                    "end": 4.0,
                    "severity": "warning",
                    "reason_codes": ["MICRO_SEGMENT_CLUSTER"],
                }
            ],
            padding_seconds=5.0,
            audio_duration=20.0,
        )

        self.assertEqual(windows, [])


class HybridFusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.primary = [
            {
                "id": "whisperx-1",
                "start": 0.0,
                "end": 10.0,
                "speaker": "WX_A",
                "text": "앞",
                "provider": "whisperx",
            },
            {
                "id": "whisperx-2",
                "start": 10.0,
                "end": 20.0,
                "speaker": "WX_A",
                "text": "반복오류",
                "provider": "whisperx",
            },
            {
                "id": "whisperx-3",
                "start": 20.0,
                "end": 30.0,
                "speaker": "WX_A",
                "text": "뒤",
                "provider": "whisperx",
            },
        ]
        self.fallback = [
            {
                "id": "kotoba-1",
                "start": 10.5,
                "end": 15.0,
                "speaker": "K_A",
                "text": "복구 하나",
            },
            {
                "id": "kotoba-2",
                "start": 15.0,
                "end": 19.5,
                "speaker": "K_A",
                "text": "복구 둘",
            },
        ]
        self.windows = [
            {
                "window_id": "rescue-window-1",
                "start": 12.0,
                "end": 18.0,
                "reason_codes": ["REPEATED_TRANSCRIPT"],
                "issue_ids": ["issue-1"],
                "segment_ids": ["whisperx-2"],
                "word_ids": [],
            }
        ]

    def test_replaces_only_failed_window_and_maps_speaker(self) -> None:
        fused, diagnostics = fuse_hybrid_segments(
            self.primary,
            self.fallback,
            self.windows,
        )

        self.assertEqual(
            [segment["text"] for segment in fused],
            ["앞", "복구 하나", "복구 둘", "뒤"],
        )
        rescued = [
            segment
            for segment in fused
            if segment.get("provider") == "hybrid-rescue-v1"
        ]
        self.assertEqual([segment["speaker"] for segment in rescued], ["WX_A"] * 2)
        self.assertTrue(
            all(
                "HYBRID_KOTOBA_RESCUE" in segment["reason_codes"]
                for segment in rescued
            )
        )
        self.assertEqual(diagnostics["replaced_window_count"], 1)
        self.assertFalse(diagnostics["needs_review"])

    def test_keeps_primary_when_fallback_has_fatal_issue(self) -> None:
        fallback_issues = [
            {
                "issue_id": "fallback-fatal",
                "start": 11.0,
                "end": 12.0,
                "reason_codes": ["REPLACEMENT_CHARACTER"],
            }
        ]

        fused, diagnostics = fuse_hybrid_segments(
            self.primary,
            self.fallback,
            self.windows,
            fallback_issues=fallback_issues,
        )

        self.assertEqual(
            [segment["text"] for segment in fused],
            ["앞", "반복오류", "뒤"],
        )
        self.assertEqual(diagnostics["replaced_window_count"], 0)
        self.assertTrue(diagnostics["needs_review"])

    def test_aligns_to_complete_fallback_segments_without_cutting_text(
        self,
    ) -> None:
        primary = [
            {**self.primary[0], "word_ids": ["word-before"]},
            {**self.primary[1], "word_ids": ["word-failed"]},
            {**self.primary[2], "word_ids": ["word-after"]},
        ]
        primary_words = [
            {
                "word_id": "word-before",
                "start": 0.0,
                "end": 9.0,
                "speaker": "WX_A",
                "word": "앞",
            },
            {
                "word_id": "word-failed",
                "start": 10.0,
                "end": 20.0,
                "speaker": "WX_A",
                "word": "반복오류",
            },
            {
                "word_id": "word-after",
                "start": 21.0,
                "end": 30.0,
                "speaker": "WX_A",
                "word": "뒤",
            },
        ]
        fallback = [
            {
                "id": "kotoba-left",
                "start": 9.0,
                "end": 12.0,
                "speaker": "K_A",
                "text": "왼쪽 경계",
            },
            {
                "id": "kotoba-right",
                "start": 18.0,
                "end": 21.0,
                "speaker": "K_A",
                "text": "오른쪽 경계",
            },
        ]

        fused, diagnostics = fuse_hybrid_segments(
            primary,
            fallback,
            self.windows,
            primary_words=primary_words,
        )

        rescued = [
            segment
            for segment in fused
            if segment.get("provider") == "hybrid-rescue-v1"
        ]
        self.assertEqual(
            [(segment["start"], segment["end"]) for segment in rescued],
            [(9.0, 12.0), (18.0, 21.0)],
        )
        self.assertEqual(
            [(segment["text"]) for segment in rescued],
            ["왼쪽 경계", "오른쪽 경계"],
        )
        same_speaker = sorted(
            (
                segment
                for segment in fused
                if segment["speaker"] == "WX_A"
            ),
            key=lambda segment: (segment["start"], segment["end"]),
        )
        self.assertTrue(
            all(
                left["end"] <= right["start"]
                for left, right in zip(
                    same_speaker,
                    same_speaker[1:],
                )
            )
        )
        self.assertEqual(
            diagnostics["decisions"][0]["superseded_segment_ids"],
            ["whisperx-2", "whisperx-1", "whisperx-3"],
        )
        self.assertEqual(diagnostics["replaced_window_count"], 1)

    def test_word_only_issue_keeps_primary_segment_in_rescue_lineage(
        self,
    ) -> None:
        primary = [
            {
                "id": "whisperx-word-owner",
                "start": 0.0,
                "end": 10.0,
                "speaker": "WX_A",
                "text": "壊れた",
                "word_ids": ["word-long"],
            }
        ]
        fallback = [
            {
                "id": "kotoba-safe",
                "start": 0.0,
                "end": 10.0,
                "speaker": "K_A",
                "text": "安全",
            }
        ]
        windows = [
            {
                "window_id": "rescue-window-1",
                "start": 0.0,
                "end": 10.0,
                "target_start": 2.0,
                "target_end": 8.0,
                "reason_codes": ["LONG_WORD_ALIGNMENT"],
                "issue_ids": ["issue-word"],
                "segment_ids": [],
                "word_ids": ["word-long"],
            }
        ]

        fused, _diagnostics = fuse_hybrid_segments(
            primary,
            fallback,
            windows,
        )

        rescued = next(
            segment
            for segment in fused
            if segment.get("provider") == "hybrid-rescue-v1"
        )
        self.assertIn(
            "whisperx-word-owner", rescued["parent_span_ids"]
        )

    def test_preserves_non_target_cross_speaker_overlap(self) -> None:
        primary = [
            {
                "id": "overlap",
                "start": 9.0,
                "end": 11.0,
                "speaker": "WX_B",
                "text": "실제 동시 발화",
            },
            {
                "id": "failed",
                "start": 10.0,
                "end": 20.0,
                "speaker": "WX_A",
                "text": "반복 오류",
                "word_ids": ["word-failed"],
            },
        ]
        windows = [
            {
                "window_id": "rescue-window-1",
                "start": 5.0,
                "end": 25.0,
                "target_start": 10.0,
                "target_end": 20.0,
                "reason_codes": ["REPEATED_TRANSCRIPT"],
                "issue_ids": ["issue-1"],
                "segment_ids": ["failed"],
                "word_ids": [],
            }
        ]

        fused, diagnostics = fuse_hybrid_segments(
            primary,
            self.fallback,
            windows,
        )

        self.assertIn("실제 동시 발화", [segment["text"] for segment in fused])
        self.assertNotIn("반복 오류", [segment["text"] for segment in fused])
        self.assertEqual(
            diagnostics["decisions"][0]["superseded_segment_ids"],
            ["failed"],
        )

    def test_maps_fallback_speakers_one_to_one(self) -> None:
        primary = [
            {
                "start": 0.0,
                "end": 10.0,
                "speaker": "WX_A",
                "text": "A",
            },
            {
                "start": 10.0,
                "end": 20.0,
                "speaker": "WX_B",
                "text": "B",
            },
        ]
        fallback = [
            {
                "start": 0.0,
                "end": 12.0,
                "speaker": "K_1",
                "text": "one",
            },
            {
                "start": 8.0,
                "end": 20.0,
                "speaker": "K_2",
                "text": "two",
            },
        ]

        mapping = map_fallback_speakers(primary, fallback)

        self.assertEqual(mapping, {"K_1": "WX_A", "K_2": "WX_B"})
        self.assertEqual(len(set(mapping.values())), 2)

    def test_keeps_low_confidence_speaker_as_kotoba_local_label(self) -> None:
        primary = [
            {
                "start": 0.0,
                "end": 5.0,
                "speaker": "WX_A",
                "text": "A",
            },
            {
                "start": 5.0,
                "end": 10.0,
                "speaker": "WX_B",
                "text": "B",
            },
            {
                "start": 10.0,
                "end": 15.0,
                "speaker": "WX_C",
                "text": "C",
            },
        ]
        fallback = [
            {
                "start": 0.0,
                "end": 15.0,
                "speaker": "K_AMBIGUOUS",
                "text": "ambiguous",
            }
        ]

        mapping = map_fallback_speakers(primary, fallback)

        self.assertEqual(
            mapping["K_AMBIGUOUS"], "KOTOBA_K_AMBIGUOUS"
        )

    def test_does_not_use_fallback_segment_past_audio_end(self) -> None:
        fallback = [
            {
                "id": "out-of-bounds",
                "start": 8.0,
                "end": 31.0,
                "speaker": "K_A",
                "text": "범위 밖",
            }
        ]

        fused, diagnostics = fuse_hybrid_segments(
            self.primary,
            fallback,
            self.windows,
            audio_duration=30.0,
        )

        self.assertEqual(
            [segment["text"] for segment in fused],
            ["앞", "반복오류", "뒤"],
        )
        self.assertEqual(
            diagnostics["decisions"][0]["outcome"],
            "needs_review_fallback_out_of_audio_bounds",
        )

    def test_marks_only_rescued_primary_words_as_superseded(self) -> None:
        words = [
            {"word_id": "word-keep", "decision": "keep"},
            {"word_id": "word-replaced", "decision": "keep"},
        ]

        marked = mark_rescued_words(
            words,
            [
                {
                    "window_id": "rescue-window-000001",
                    "outcome": "replaced_with_kotoba",
                    "superseded_word_ids": ["word-replaced"],
                }
            ],
        )

        self.assertEqual(marked[0]["decision"], "keep")
        self.assertEqual(marked[1]["decision"], "superseded")
        self.assertEqual(
            marked[1]["superseded_by_window_ids"],
            ["rescue-window-000001"],
        )


if __name__ == "__main__":
    unittest.main()
