import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from stt_to_subtitle.kotoba import (
    ChunkProgress,
    DEFAULT_CHUNK_LENGTH_SECONDS,
    MODEL_ID,
    MODEL_REVISION,
    NoiseFilteringSpeakerDiarization,
    TIMESTAMP_POSTPROCESSOR,
    TranscriptionOptions,
    corrected_kotoba_chunk_iter,
    corrected_kotoba_postprocess,
    load_pipeline,
    normalize_segments,
    run_pipeline,
    speaker_transcripts,
    transcribe,
)


class NormalizeSegmentsTests(unittest.TestCase):
    def test_corrected_postprocess_preserves_decoded_end_timestamp(self) -> None:
        class FakeTokenizer:
            def __init__(self) -> None:
                self.received_outputs = []

            def _decode_asr(self, outputs, **_kwargs):
                self.received_outputs.append(outputs)
                return (
                    "発話",
                    {
                        "chunks": [
                            {
                                "text": "発話",
                                "timestamp": [5.8, 7.1],
                            }
                        ]
                    },
                )

        tokenizer = FakeTokenizer()
        speech_pipeline = SimpleNamespace(
            tokenizer=tokenizer,
            feature_extractor=SimpleNamespace(
                sampling_rate=16_000,
                chunk_length=30,
            ),
            model=SimpleNamespace(
                config=SimpleNamespace(max_source_positions=1500)
            ),
            punctuator=None,
        )

        result = corrected_kotoba_postprocess(
            speech_pipeline,
            [
                {
                    "speaker_id": "SPEAKER_00",
                    "speaker_span": [0.0, 55.0],
                    "stride": (240_000, 0, 40_000),
                    "tokens": [1],
                },
                {
                    "speaker_id": "SPEAKER_00",
                    "speaker_span": [0.0, 55.0],
                    "stride": (240_000, 40_000, 0),
                    "tokens": [2],
                },
            ],
            return_timestamps=True,
            return_language=False,
            add_punctuation=False,
        )

        self.assertEqual(
            result["chunks"][0]["timestamp"],
            [5.8, 7.1],
        )
        self.assertNotEqual(result["chunks"][0]["timestamp"][1], 55.0)
        self.assertEqual(
            result["timestamp_postprocessor"],
            TIMESTAMP_POSTPROCESSOR,
        )
        self.assertEqual(len(tokenizer.received_outputs[0]), 2)
        self.assertEqual(
            tokenizer.received_outputs[0][0]["stride"],
            (15.0, 0.0, 2.5),
        )

    def test_corrected_postprocess_keeps_cross_speaker_overlap(self) -> None:
        class FakeTokenizer:
            def _decode_asr(self, outputs, **_kwargs):
                speaker = outputs[0]["speaker_id"]
                return (
                    speaker,
                    {
                        "chunks": [
                            {
                                "text": speaker,
                                "timestamp": [0.2, 1.1],
                            }
                        ]
                    },
                )

        speech_pipeline = SimpleNamespace(
            tokenizer=FakeTokenizer(),
            feature_extractor=SimpleNamespace(
                sampling_rate=16_000,
                chunk_length=30,
            ),
            model=SimpleNamespace(
                config=SimpleNamespace(max_source_positions=1500)
            ),
            punctuator=None,
        )

        result = corrected_kotoba_postprocess(
            speech_pipeline,
            [
                {
                    "speaker_id": "SPEAKER_00",
                    "speaker_span": [10.0, 20.0],
                    "tokens": [1],
                },
                {
                    "speaker_id": "SPEAKER_01",
                    "speaker_span": [10.5, 13.0],
                    "tokens": [2],
                },
            ],
            return_timestamps=True,
            return_language=False,
            add_punctuation=False,
        )

        self.assertEqual(
            [
                (chunk["speaker_id"], chunk["timestamp"])
                for chunk in result["chunks"]
            ],
            [
                ("SPEAKER_00", [10.2, 11.1]),
                ("SPEAKER_01", [10.7, 11.6]),
            ],
        )

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
    def test_defaults_to_sixty_second_chunks_and_noise_filter(self) -> None:
        options = TranscriptionOptions()

        self.assertEqual(
            options.chunk_length_seconds,
            DEFAULT_CHUNK_LENGTH_SECONDS,
        )
        self.assertTrue(options.noise_filter)

    def test_exact_speaker_count_cannot_be_combined_with_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            TranscriptionOptions(num_speakers=2, min_speakers=1).validate()


class CorrectedChunkIteratorTests(unittest.TestCase):
    def test_preserves_input_longer_than_whispers_native_window(self) -> None:
        class FakeAudio:
            def __init__(self, length: int) -> None:
                self.shape = (length,)

            def __getitem__(self, item):
                start = item.start or 0
                stop = min(item.stop or self.shape[0], self.shape[0])
                return FakeAudio(max(0, stop - start))

        class FakeBatch(dict):
            def to(self, **_kwargs):
                return self

        class FakeFeatureExtractor:
            sampling_rate = 16_000
            n_samples = 30 * sampling_rate

            def __init__(self) -> None:
                self.calls = []

            def __call__(self, audio, **kwargs):
                self.calls.append((audio.shape[0], kwargs))
                return FakeBatch(input_features="features")

        extractor = FakeFeatureExtractor()
        chunks = list(
            corrected_kotoba_chunk_iter(
                FakeAudio(60 * 16_000),
                extractor,
                60 * 16_000,
                10 * 16_000,
                10 * 16_000,
            )
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(extractor.calls[0][0], 60 * 16_000)
        self.assertEqual(
            extractor.calls[0][1],
            {
                "sampling_rate": 16_000,
                "return_tensors": "pt",
                "return_attention_mask": True,
                "truncation": False,
                "padding": "longest",
            },
        )
        self.assertEqual(chunks[0]["stride"], (60 * 16_000, 0, 0))


class NoiseFilteringSpeakerDiarizationTests(unittest.TestCase):
    @dataclass(frozen=True)
    class Segment:
        start: float
        end: float

    class Annotation:
        def __init__(self, entries=()) -> None:
            self.entries = list(entries)

        def empty(self):
            return NoiseFilteringSpeakerDiarizationTests.Annotation()

        def itertracks(self, yield_label=False):
            for segment, track, speaker in self.entries:
                if yield_label:
                    yield segment, track, speaker
                else:
                    yield segment, track

        def __setitem__(self, key, speaker):
            segment, track = key
            self.entries.append((segment, track, speaker))

    class Audio:
        shape = (160_000,)

        def __getitem__(self, _key):
            return self

    def test_removes_only_spans_rejected_by_second_voice_detector(self) -> None:
        annotation = self.Annotation(
            [
                (self.Segment(0.0, 2.0), "_", "SPEAKER_00"),
                (self.Segment(3.0, 4.0), "_", "SPEAKER_01"),
            ]
        )
        decisions = iter([True, False])
        detector = Mock(side_effect=lambda *_args: next(decisions))
        wrapper = NoiseFilteringSpeakerDiarization(
            Mock(return_value=annotation),
            detector=detector,
        )

        filtered = wrapper(self.Audio(), sampling_rate=16_000)

        self.assertEqual(
            [
                (entry[0].start, entry[0].end, entry[2])
                for entry in filtered.entries
            ],
            [(0.0, 2.0, "SPEAKER_00")],
        )
        self.assertEqual(wrapper.public_dict()["removed_count"], 1)
        self.assertEqual(
            wrapper.public_dict()["execution_state"],
            "run_removed",
        )
        self.assertEqual(wrapper.public_dict()["candidate_count"], 2)
        self.assertEqual(wrapper.public_dict()["kept_count"], 1)
        self.assertEqual(
            wrapper.public_dict()["removed_spans"],
            [
                {
                    "start": 3.0,
                    "end": 4.0,
                    "speaker": "SPEAKER_01",
                }
            ],
        )

    def test_disabled_filter_returns_original_annotation(self) -> None:
        annotation = self.Annotation(
            [(self.Segment(0.0, 1.0), "_", "SPEAKER_00")]
        )
        detector = Mock(return_value=False)
        wrapper = NoiseFilteringSpeakerDiarization(
            Mock(return_value=annotation),
            detector=detector,
        )
        wrapper.configure(enabled=False, trigger_level=7.0)

        self.assertIs(
            wrapper(self.Audio(), sampling_rate=16_000),
            annotation,
        )
        detector.assert_not_called()
        self.assertEqual(wrapper.public_dict()["execution_state"], "not_run")

    def test_enabled_filter_reports_run_with_no_removal(self) -> None:
        annotation = self.Annotation(
            [(self.Segment(0.0, 1.0), "_", "SPEAKER_00")]
        )
        wrapper = NoiseFilteringSpeakerDiarization(
            Mock(return_value=annotation),
            detector=Mock(return_value=True),
        )

        wrapper(self.Audio(), sampling_rate=16_000)

        report = wrapper.public_dict()
        self.assertEqual(report["execution_state"], "run_no_removal")
        self.assertEqual(report["removed_count"], 0)
        self.assertEqual(report["candidate_count"], 1)


class TranscribeTests(unittest.TestCase):
    def test_debug_mode_writes_observable_kotoba_stage_artifacts(self) -> None:
        annotation = NoiseFilteringSpeakerDiarizationTests.Annotation(
            [
                (
                    NoiseFilteringSpeakerDiarizationTests.Segment(0.0, 1.0),
                    "_",
                    "SPEAKER_00",
                )
            ]
        )
        wrapper = NoiseFilteringSpeakerDiarization(
            Mock(return_value=annotation),
            detector=Mock(return_value=True),
        )

        class FakePipeline:
            def __init__(self):
                self.model_speaker_diarization = wrapper

            def __call__(self, _audio_path, **_kwargs):
                self.model_speaker_diarization(
                    NoiseFilteringSpeakerDiarizationTests.Audio(),
                    sampling_rate=16_000,
                )
                return {
                    "chunks": [
                        {
                            "timestamp": [0.0, 1.0],
                            "speaker_id": "SPEAKER_00",
                            "text": "はい",
                        }
                    ]
                }

        with TemporaryDirectory() as directory:
            result = run_pipeline(
                FakePipeline(),
                Path("/output/sample.wav"),
                TranscriptionOptions(),
                debug_artifact_dir=Path(directory),
            )
            names = {path.name for path in Path(directory).iterdir()}

        self.assertEqual(
            names,
            {
                "01_diarization_raw.json",
                "02_speaker_spans_processed.json",
                "03_noise_filter_candidates.json",
                "04_noise_filter_result.json",
                "05_asr_input_spans.json",
                "06_asr_raw_segments.json",
                "07_final_segments.json",
            },
        )
        self.assertEqual(
            result["noise_filter"]["execution_state"],
            "run_no_removal",
        )

    def test_reports_actual_pipeline_chunk_progress_at_configured_interval(
        self,
    ) -> None:
        class FakePipeline:
            def preprocess(self, _audio_path, **_kwargs):
                for _index in range(23):
                    yield {
                        "input_features": SimpleNamespace(shape=(1, 80, 3000))
                    }

            def _forward(self, model_inputs, **_kwargs):
                return model_inputs

            def __call__(self, audio_path, **kwargs):
                outputs = []
                for model_inputs in self.preprocess(audio_path, **kwargs):
                    outputs.append(self._forward(model_inputs))
                return {"chunks": outputs}

        speech_pipeline = FakePipeline()
        progress: list[ChunkProgress] = []

        run_pipeline(
            speech_pipeline,
            Path("/output/sample.wav"),
            TranscriptionOptions(),
            progress_callback=progress.append,
            progress_every=10,
        )

        self.assertEqual(
            (
                progress[0].created,
                progress[0].completed,
                progress[0].in_progress,
            ),
            (1, 0, 1),
        )
        self.assertEqual(
            (
                progress[-1].created,
                progress[-1].completed,
                progress[-1].in_progress,
                progress[-1].final,
            ),
            (23, 23, 0, True),
        )
        self.assertTrue(
            any(
                item.created == 10 and item.completed == 10
                for item in progress
            )
        )
        self.assertNotIn("preprocess", speech_pipeline.__dict__)
        self.assertNotIn("_forward", speech_pipeline.__dict__)

    def test_loads_whisper_on_mps_and_pyannote_on_cpu(self) -> None:
        pipeline_factory = Mock(return_value=Mock())
        fake_torch = SimpleNamespace(
            float16="float16",
            float32="float32",
            set_num_threads=Mock(),
        )
        fake_transformers = SimpleNamespace(pipeline=pipeline_factory)

        with patch.dict(
            sys.modules,
            {"torch": fake_torch, "transformers": fake_transformers},
        ):
            load_pipeline(
                "test-token",
                batch_size=1,
                device="mps",
                diarization_device="cpu",
            )

        pipeline_factory.assert_called_once_with(
            model=MODEL_ID,
            revision=MODEL_REVISION,
            token="test-token",
            torch_dtype="float16",
            device="mps",
            batch_size=1,
            trust_remote_code=True,
            device_pyannote="cpu",
        )

    def test_loads_whisper_and_pyannote_on_cuda_with_float16(self) -> None:
        pipeline_factory = Mock(return_value=Mock())
        fake_torch = SimpleNamespace(
            float16="float16",
            float32="float32",
            set_num_threads=Mock(),
        )
        fake_transformers = SimpleNamespace(pipeline=pipeline_factory)

        with patch.dict(
            sys.modules,
            {"torch": fake_torch, "transformers": fake_transformers},
        ):
            load_pipeline(
                "test-token",
                batch_size=2,
                device="cuda:0",
                diarization_device="cuda:1",
            )

        pipeline_factory.assert_called_once_with(
            model=MODEL_ID,
            revision=MODEL_REVISION,
            token="test-token",
            torch_dtype="float16",
            device="cuda:0",
            batch_size=2,
            trust_remote_code=True,
            device_pyannote="cuda:1",
        )

    def test_rejects_unsupported_model_device(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "device must be cpu, mps, cuda",
        ):
            load_pipeline("test-token", device="directml")

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
            str(Path("/output/sample.wav")),
            chunk_length_s=60,
            add_punctuation=False,
            num_speakers=2,
            min_speakers=None,
            max_speakers=None,
        )
