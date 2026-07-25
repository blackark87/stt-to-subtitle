import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stt_to_subtitle.kotoba import (
    MODEL_ID,
    MODEL_REVISION,
    TranscriptionOptions,
    normalize_segments,
    speaker_transcripts,
    transcribe,
)


class NormalizeSegmentsTests(unittest.TestCase):
    def test_sorts_segments_and_applies_source_offset(self) -> None:
        result = {
            "chunks": [
                {
                    "speaker_id": "SPEAKER_01",
                    "text": " 二番 ",
                    "timestamp": [3.0, 4.5],
                },
                {
                    "speaker_id": "SPEAKER_00",
                    "text": "一番",
                    "timestamp": [0.5, 2.0],
                },
            ]
        }

        segments = normalize_segments(result, offset_seconds=10.0)

        self.assertEqual([item["text"] for item in segments], ["一番", "二番"])
        self.assertEqual(segments[0]["start"], 10.5)
        self.assertEqual(segments[1]["end"], 14.5)

    def test_skips_blank_or_incomplete_chunks(self) -> None:
        result = {
            "chunks": [
                {"text": " ", "timestamp": [0, 1]},
                {"text": "missing time"},
                {"text": "valid", "timestamp": [2, 1]},
            ]
        }

        self.assertEqual(
            normalize_segments(result),
            [
                {
                    "start": 2.0,
                    "end": 2.0,
                    "speaker": "UNKNOWN",
                    "text": "valid",
                }
            ],
        )

    def test_collects_per_speaker_text(self) -> None:
        result = {
            "speaker_ids": ["SPEAKER_00"],
            "text/SPEAKER_00": "こんにちは。",
        }

        self.assertEqual(
            speaker_transcripts(result),
            {"SPEAKER_00": "こんにちは。"},
        )


class TranscriptionOptionsTests(unittest.TestCase):
    def test_exact_speaker_count_cannot_be_combined_with_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            TranscriptionOptions(num_speakers=2, min_speakers=1).validate()


class TranscribeTests(unittest.TestCase):
    def test_loads_pinned_model_on_cpu_and_forwards_speaker_options(self) -> None:
        speech_pipeline = Mock(return_value={"chunks": []})
        pipeline_factory = Mock(return_value=speech_pipeline)
        fake_torch = SimpleNamespace(
            float32="float32",
            set_num_threads=Mock(),
        )
        fake_transformers = SimpleNamespace(pipeline=pipeline_factory)

        with patch.dict(
            sys.modules,
            {"torch": fake_torch, "transformers": fake_transformers},
        ):
            result = transcribe(
                Path("/output/sample.wav"),
                "test-token",
                TranscriptionOptions(
                    batch_size=2,
                    num_speakers=2,
                    threads=8,
                ),
            )

        self.assertEqual(result, {"chunks": []})
        fake_torch.set_num_threads.assert_called_once_with(8)
        pipeline_factory.assert_called_once_with(
            model=MODEL_ID,
            revision=MODEL_REVISION,
            token="test-token",
            torch_dtype="float32",
            device="cpu",
            batch_size=2,
            trust_remote_code=True,
        )
        speech_pipeline.assert_called_once_with(
            "/output/sample.wav",
            chunk_length_s=15,
            add_punctuation=False,
            num_speakers=2,
            min_speakers=None,
            max_speakers=None,
        )
