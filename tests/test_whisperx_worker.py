import unittest

from stt_to_subtitle.whisperx_worker import (
    WhisperXSegmentationOptions,
    _transcribe_with_oom_backoff,
    extract_whisperx_words,
    install_whisperx_sentence_splitter_fallback,
    normalize_whisperx_segments,
    rebuild_whisperx_segments,
)


class _RecordingModel:
    """Stub WhisperX model that fails with CUDA OOM below a batch threshold."""

    def __init__(self, *, succeeds_at: int, error: Exception | None = None):
        self.succeeds_at = succeeds_at
        self.error = error
        self.batch_sizes: list[int] = []

    def transcribe(self, audio, *, batch_size, language, chunk_size):
        self.batch_sizes.append(batch_size)
        if batch_size > self.succeeds_at:
            raise self.error or RuntimeError("CUDA out of memory. Tried ...")
        return {"segments": [], "batch_size": batch_size}


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

    def test_rebuild_preserves_parent_cue_across_speaker_changes(self) -> None:
        words = [
            {
                "word_id": "word-000001",
                "parent_span_ids": ["cue-000001"],
                "word": "行きま",
                "start": 0.0,
                "end": 0.5,
                "speaker": "A",
            },
            {
                "word_id": "word-000002",
                "parent_span_ids": ["cue-000001"],
                "word": "した。",
                "start": 0.5,
                "end": 1.0,
                "speaker": "B",
            },
            {
                "word_id": "word-000003",
                "parent_span_ids": ["cue-000002"],
                "word": "次です。",
                "start": 1.1,
                "end": 1.6,
                "speaker": "B",
            },
        ]

        segments = rebuild_whisperx_segments(
            words,
            WhisperXSegmentationOptions(
                split_on_speaker_change=False,
                split_on_parent_change=True,
                prefer_punctuation_boundary=True,
                punctuation_boundary_mode="sentence",
            ),
        )

        self.assertEqual(
            [item["text"] for item in segments],
            ["行きました。", "次です。"],
        )
        self.assertEqual(segments[0]["speaker"], "MULTIPLE")

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

    def test_sentence_punctuation_mode_ignores_commas_and_ellipses(self) -> None:
        words = [
            {
                "word_id": f"word-{index:06d}",
                "word": text,
                "start": float(index),
                "end": float(index) + 0.5,
                "speaker": "A",
            }
            for index, text in enumerate(
                ("まだ、", "続く…", "ここで終わる。", "次の文"),
                start=1,
            )
        ]

        segments = rebuild_whisperx_segments(
            words,
            WhisperXSegmentationOptions(
                prefer_punctuation_boundary=True,
                punctuation_boundary_mode="sentence",
            ),
        )

        self.assertEqual(
            [segment["text"] for segment in segments],
            ["まだ、続く…ここで終わる。", "次の文"],
        )

    def test_continuity_profile_avoids_forced_mid_utterance_splits(self) -> None:
        words = [
            {
                "word_id": "word-000001",
                "word": "これは長い",
                "start": 0.0,
                "end": 2.8,
                "speaker": "A",
            },
            {
                "word_id": "word-000002",
                "word": "発話として誤判定されても",
                "start": 3.0,
                "end": 6.0,
                "speaker": "B",
            },
            {
                "word_id": "word-000003",
                "word": "途中では切らない",
                "start": 6.2,
                "end": 9.2,
                "speaker": "A",
            },
        ]

        current = rebuild_whisperx_segments(
            words,
            WhisperXSegmentationOptions(
                split_on_speaker_change=True,
                max_gap_sec=0.8,
                max_duration_sec=8.0,
                max_chars=36,
                prefer_punctuation_boundary=True,
            ),
        )
        continuity = rebuild_whisperx_segments(
            words,
            WhisperXSegmentationOptions(
                split_on_speaker_change=False,
                max_gap_sec=1.5,
                max_duration_sec=10.0,
                max_chars=48,
                prefer_punctuation_boundary=False,
            ),
        )

        self.assertEqual(len(current), 3)
        self.assertEqual(len(continuity), 1)
        self.assertEqual(
            continuity[0]["text"],
            "これは長い発話として誤判定されても途中では切らない",
        )
        self.assertEqual(continuity[0]["speaker"], "MULTIPLE")


class WhisperXBatchBackoffTests(unittest.TestCase):
    def _transcribe(self, model: _RecordingModel, batch_size: int):
        return _transcribe_with_oom_backoff(
            model,
            object(),
            batch_size=batch_size,
            language="ja",
            chunk_size=30,
        )

    def test_keeps_the_requested_batch_size_when_memory_suffices(self) -> None:
        model = _RecordingModel(succeeds_at=16)

        result, effective = self._transcribe(model, 8)

        self.assertEqual(effective, 8)
        self.assertEqual(result["batch_size"], 8)
        self.assertEqual(model.batch_sizes, [8])

    def test_halves_the_batch_size_until_the_transcription_fits(self) -> None:
        model = _RecordingModel(succeeds_at=4)

        result, effective = self._transcribe(model, 16)

        self.assertEqual(effective, 4)
        self.assertEqual(result["batch_size"], 4)
        self.assertEqual(model.batch_sizes, [16, 8, 4])

    def test_raises_when_a_single_item_batch_still_runs_out_of_memory(
        self,
    ) -> None:
        model = _RecordingModel(succeeds_at=0)

        with self.assertRaisesRegex(RuntimeError, "out of memory"):
            self._transcribe(model, 2)

        self.assertEqual(model.batch_sizes, [2, 1])

    def test_retries_ctranslate2_allocation_failures(self) -> None:
        for message in (
            "cuBLAS failed with status CUBLAS_STATUS_ALLOC_FAILED",
            "parallel_for failed: cudaErrorInvalidDevice: invalid device ordinal",
        ):
            with self.subTest(message=message):
                model = _RecordingModel(
                    succeeds_at=2,
                    error=RuntimeError(message),
                )

                _, effective = self._transcribe(model, 8)

                self.assertEqual(effective, 2)
                self.assertEqual(model.batch_sizes, [8, 4, 2])

    def test_reraises_unrelated_runtime_errors_without_retrying(self) -> None:
        model = _RecordingModel(
            succeeds_at=0,
            error=RuntimeError("alignment model is missing"),
        )

        with self.assertRaisesRegex(RuntimeError, "alignment model"):
            self._transcribe(model, 8)

        self.assertEqual(model.batch_sizes, [8])


if __name__ == "__main__":
    unittest.main()
