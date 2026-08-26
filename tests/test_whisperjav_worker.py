import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from stt_to_subtitle.vendor.whisperjav import presets

from stt_to_subtitle.vendor.whisperjav.runner import Cue, EnsembleResult, PassOutcome
from stt_to_subtitle.whisperjav_worker import (
    ALIGNER_MODEL_REVISION,
    ANIME_MODEL_REVISION,
    QWEN_MODEL_REVISION,
    WHISPERJAV_COMMIT,
    WhisperJAVOptions,
    align_cues,
    parse_srt,
    run_whisperjav,
)


class WhisperJAVWorkerTests(unittest.TestCase):
    def test_parses_multiline_srt_and_decimal_comma(self) -> None:
        cues = parse_srt(
            "1\n00:00:01,250 --> 00:00:02,500\nこんにちは\n世界\n\n"
            "2\n00:00:03.000 --> 00:00:04.000\nはい\n"
        )

        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0].start, 1.25)
        self.assertEqual(cues[0].text, "こんにちは\n世界")
        self.assertEqual(cues[1].end, 4.0)

    def test_group_duration_options_reach_the_pass_configs(self) -> None:
        options = WhisperJAVOptions.from_options(
            {
                "whisperjav": {
                    "anime_max_group_duration_seconds": 2.5,
                    "qwen_max_group_duration_seconds": 4.0,
                }
            }
        )
        first = presets.pass1_config(
            "anime-model",
            max_group_duration=options.anime_max_group_duration_seconds,
        )
        second = presets.pass2_config(
            "qwen-model",
            max_group_duration=options.qwen_max_group_duration_seconds,
        )

        self.assertEqual(first.generator_backend, "anime-whisper")
        self.assertEqual(first.model_id, "anime-model")
        self.assertEqual(first.segmenter_kwargs()["max_group_duration_s"], 2.5)
        self.assertEqual(second.generator_backend, "qwen3")
        self.assertEqual(second.model_id, "qwen-model")
        self.assertEqual(second.segmenter_kwargs()["max_group_duration_s"], 4.0)
        self.assertEqual(presets.MERGE_STRATEGY, "pass1_primary")

    def test_short_cue_uses_fallback_without_reaching_forced_aligner(self) -> None:
        aligner = SimpleNamespace(
            load=Mock(),
            unload=Mock(),
            align_batch=Mock(
                return_value=[
                    SimpleNamespace(
                        words=[SimpleNamespace(word="はい", start=0.0, end=0.5)]
                    )
                ]
            ),
        )
        cues = [
            Cue(0.0, 0.01, "短"),
            Cue(1.0, 1.5, "はい"),
        ]

        with patch(
            "stt_to_subtitle.whisperjav_worker._create_forced_aligner",
            return_value=aligner,
        ):
            words, fallback_count = align_cues(
                cues,
                audio=[0.0] * 32000,
                sample_rate=16000,
                aligner_path=Path("/models/aligner"),
            )

        self.assertEqual(fallback_count, 1)
        self.assertEqual(words[0]["timestamp_source"], "cue_fallback")
        self.assertEqual(words[1]["timestamp_source"], "qwen3_forced_alignment")
        self.assertEqual(
            len(aligner.align_batch.call_args.kwargs["audio_paths"]),
            1,
        )

    def test_reports_pinned_models_after_ensemble_and_alignment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "audio.wav"
            audio_path.write_bytes(b"not-read-by-mocks")
            merged = root / "merged.srt"
            merged.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nはい\n",
                encoding="utf-8",
            )
            snapshots = {
                "litagin/anime-whisper": root / "anime",
                "jaykwok/Qwen3-ASR-1.7B-JA-Anime-Galgame": root / "qwen",
                "Qwen/Qwen3-ForcedAligner-0.6B": root / "aligner",
            }
            ensemble = EnsembleResult(
                cues=[Cue(0.0, 1.0, "はい")],
                merged_srt_path=merged,
                pass1=PassOutcome(
                    name="pass1",
                    status="completed",
                    subtitle_count=1,
                    elapsed_seconds=4.0,
                ),
                pass2=PassOutcome(
                    name="pass2",
                    status="completed",
                    subtitle_count=1,
                    elapsed_seconds=6.0,
                ),
                merge_stats={"strategy": "pass1_primary", "merged_count": 1},
                scene_count=3,
                status="completed",
                stage_elapsed={"scene_detect": 1.0, "pass1": 4.0, "pass2": 6.0},
            )

            with patch(
                "stt_to_subtitle.whisperjav_worker._snapshot",
                side_effect=lambda model_id, revision: snapshots[model_id],
            ) as snapshot:
                with patch(
                    "stt_to_subtitle.whisperjav_worker._read_pcm16",
                    return_value=(object(), 16000),
                ):
                    def run_with_progress(*args, **kwargs):
                        callback = kwargs["progress_callback"]
                        callback("scene_detection", 1, 7)
                        callback("primary_transcription", 2, 7)
                        callback("secondary_transcription", 3, 7)
                        callback("transcription_merge", 4, 7)
                        return ensemble

                    with patch(
                        "stt_to_subtitle.whisperjav_worker.run_ensemble",
                        side_effect=run_with_progress,
                    ) as run:
                        with patch(
                            "stt_to_subtitle.whisperjav_worker.align_cues",
                            return_value=(
                                [
                                    {
                                        "word": "はい",
                                        "start": 0.0,
                                        "end": 1.0,
                                        "speaker": "UNKNOWN",
                                    }
                                ],
                                0,
                            ),
                        ):
                            stage_progress = []
                            result = run_whisperjav(
                                audio_path,
                                {},
                                progress_callback=lambda *values: (
                                    stage_progress.append(values)
                                ),
                            )

        self.assertEqual(
            snapshot.call_args_list,
            [
                call("litagin/anime-whisper", ANIME_MODEL_REVISION),
                call(
                    "jaykwok/Qwen3-ASR-1.7B-JA-Anime-Galgame",
                    QWEN_MODEL_REVISION,
                ),
                call(
                    "Qwen/Qwen3-ForcedAligner-0.6B",
                    ALIGNER_MODEL_REVISION,
                ),
            ],
        )
        self.assertEqual(
            run.call_args.kwargs["pass1"].model_id,
            str(root / "anime"),
        )
        self.assertEqual(
            run.call_args.kwargs["pass2"].model_id,
            str(root / "qwen"),
        )
        self.assertEqual(result["model"]["revision"], WHISPERJAV_COMMIT)
        self.assertEqual(
            result["model"]["pass1"]["revision"], ANIME_MODEL_REVISION
        )
        self.assertEqual(
            result["model"]["pass2"]["revision"], QWEN_MODEL_REVISION
        )
        self.assertEqual(
            result["model"]["aligner"]["revision"], ALIGNER_MODEL_REVISION
        )
        self.assertEqual(result["runtime"]["asr_pass_count"], 2)
        self.assertEqual(result["runtime"]["alignment_pass_count"], 1)
        self.assertEqual(
            stage_progress,
            [
                ("scene_detection", 1, 7),
                ("primary_transcription", 2, 7),
                ("secondary_transcription", 3, 7),
                ("transcription_merge", 4, 7),
                ("forced_alignment", 5, 7),
            ],
        )

    def test_payload_keeps_its_schema_and_gains_the_stage_breakdown(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "audio.wav"
            audio_path.write_bytes(b"not-read-by-mocks")
            merged = root / "merged.srt"
            merged.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nはい\n",
                encoding="utf-8",
            )
            ensemble = EnsembleResult(
                cues=[Cue(0.0, 1.0, "はい")],
                merged_srt_path=merged,
                pass1=PassOutcome(
                    name="pass1", status="completed", subtitle_count=1
                ),
                pass2=PassOutcome(
                    name="pass2",
                    status="failed",
                    error="RuntimeError: boom",
                ),
                merge_stats={"strategy": "pass1_primary", "degraded": True},
                scene_count=2,
                status="degraded",
                stage_elapsed={"scene_detect": 0.5, "pass1": 2.0, "pass2": 0.0},
            )

            with patch(
                "stt_to_subtitle.whisperjav_worker._snapshot",
                side_effect=lambda model_id, revision: root / "model",
            ):
                with patch(
                    "stt_to_subtitle.whisperjav_worker._read_pcm16",
                    return_value=(object(), 16000),
                ):
                    with patch(
                        "stt_to_subtitle.whisperjav_worker.run_ensemble",
                        return_value=ensemble,
                    ):
                        with patch(
                            "stt_to_subtitle.whisperjav_worker.align_cues",
                            return_value=([], 1),
                        ):
                            result = run_whisperjav(audio_path, {})

        self.assertEqual(
            sorted(result),
            [
                "model",
                "options",
                "quality",
                "runtime",
                "segments",
                "timing",
                "words",
            ],
        )
        self.assertEqual(
            sorted(result["model"]),
            [
                "aligner",
                "id",
                "pass1",
                "pass1_vad",
                "pass2",
                "pass2_vad",
                "revision",
            ],
        )
        self.assertEqual(result["quality"]["ensemble_status"], "degraded")
        self.assertEqual(result["quality"]["pass2"]["status"], "failed")
        self.assertEqual(
            sorted(result["quality"]["pass1"]),
            ["filters", "processing_time", "status", "subtitles"],
        )
        self.assertEqual(
            sorted(result["quality"]["merge"]),
            ["statistics", "status", "strategy"],
        )
        self.assertEqual(result["quality"]["alignment_fallback_count"], 1)
        self.assertTrue(result["runtime"]["internalized"])
        self.assertEqual(result["runtime"]["scene_count"], 2)
        self.assertEqual(result["runtime"]["stage_elapsed"]["pass1"], 2.0)
        self.assertFalse(result["runtime"]["simultaneous_model_residency"])
        self.assertEqual(
            result["segments"],
            [{"start": 0.0, "end": 1.0, "speaker": "UNKNOWN", "text": "はい"}],
        )


if __name__ == "__main__":
    unittest.main()
