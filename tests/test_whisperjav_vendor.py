"""Tests for the vendored WhisperJAV ensemble and its resolved presets."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stt_to_subtitle.vendor.whisperjav import presets
from stt_to_subtitle.vendor.whisperjav.ensemble.merge import MergeEngine
from stt_to_subtitle.vendor.whisperjav.runner import (
    Cue,
    PassOutcome,
    parse_srt_cues,
    run_ensemble,
)

GOLDEN = Path(__file__).parent / "golden" / "whisperjav"

try:  # The VAD factory reaches numpy through its package __init__.
    import numpy  # noqa: F401

    HAS_NUMPY = True
except ImportError:  # pragma: no cover - depends on the environment
    HAS_NUMPY = False


def _write_srt(path: Path, entries: list[tuple[float, float, str]]) -> None:
    def stamp(value: float) -> str:
        hours, rest = divmod(value, 3600)
        minutes, seconds = divmod(rest, 60)
        whole = int(seconds)
        millis = round((seconds - whole) * 1000)
        return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d},{millis:03d}"

    blocks = [
        f"{index}\n{stamp(start)} --> {stamp(end)}\n{text}\n"
        for index, (start, end, text) in enumerate(entries, start=1)
    ]
    path.write_text("\n".join(blocks), encoding="utf-8")


class PresetSnapshotTests(unittest.TestCase):
    """Pin the transcribed literals against the upstream resolver output."""

    def setUp(self) -> None:
        self.fixture = json.loads(
            (GOLDEN / "resolved_pass_params.json").read_text(encoding="utf-8")
        )

    def test_pass1_segmenter_config_matches_upstream_resolution(self) -> None:
        self.assertEqual(
            presets.PASS1_SEGMENTER_CONFIG,
            self.fixture["pass1"]["segmenter_config"],
        )

    def test_pass2_segmenter_config_matches_upstream_resolution(self) -> None:
        self.assertEqual(
            presets.PASS2_SEGMENTER_CONFIG,
            self.fixture["pass2"]["segmenter_config"],
        )

    def test_every_segmenter_key_is_one_upstream_would_forward(self) -> None:
        allowed = set(self.fixture["segmenter_params"])
        for config in (
            presets.PASS1_SEGMENTER_CONFIG,
            presets.PASS2_SEGMENTER_CONFIG,
        ):
            self.assertEqual(set(config) - allowed, set())

    def test_scalars_override_the_sensitivity_values(self) -> None:
        config = presets.pass1_config("anime", max_group_duration=2.0)
        kwargs = config.segmenter_kwargs()

        # The YAML/sensitivity layer carries 5 s and 1.0 s; the constructor
        # scalars must win, exactly as QwenPipeline injects them.
        self.assertEqual(presets.PASS1_SEGMENTER_CONFIG["max_group_duration_s"], 5)
        self.assertEqual(kwargs["max_group_duration_s"], 2.0)
        self.assertEqual(kwargs["chunk_threshold_s"], 0.2)
        self.assertEqual(kwargs["start_pad_ms"], 0)
        self.assertEqual(kwargs["end_pad_ms"], 30)

    def test_pass_recipes_keep_the_fixed_backends(self) -> None:
        first = presets.pass1_config("anime", max_group_duration=2.0)
        second = presets.pass2_config("qwen", max_group_duration=3.0)

        self.assertEqual(first.generator_backend, "anime-whisper")
        self.assertEqual(first.segmenter_backend, "whisperseg")
        self.assertFalse(first.assembly_cleaner)
        self.assertTrue(first.anime_srt_filter)
        self.assertEqual(first.max_new_tokens, 444)

        self.assertEqual(second.generator_backend, "qwen3")
        self.assertEqual(second.segmenter_backend, "ten")
        self.assertTrue(second.assembly_cleaner)
        self.assertFalse(second.anime_srt_filter)
        self.assertEqual(second.max_new_tokens, 4096)


@unittest.skipUnless(HAS_NUMPY, "speech segmentation requires numpy")
class SegmenterFactoryStrictnessTests(unittest.TestCase):
    def test_unknown_parameter_is_rejected_instead_of_dropped(self) -> None:
        from stt_to_subtitle.vendor.whisperjav.modules.speech_segmentation.factory import (
            SpeechSegmenterFactory,
        )

        with self.assertRaisesRegex(ValueError, "unsupported ten"):
            SpeechSegmenterFactory._sanitize_params(
                "ten",
                {"threshold": 0.32, "speech_pad_ms": 300},
            )

    def test_known_parameters_are_coerced_and_kept(self) -> None:
        from stt_to_subtitle.vendor.whisperjav.modules.speech_segmentation.factory import (
            SpeechSegmenterFactory,
        )

        sanitized = SpeechSegmenterFactory._sanitize_params(
            "ten",
            {"threshold": "0.4", "hop_size": "256"},
        )

        self.assertEqual(sanitized, {"threshold": 0.4, "hop_size": 256})


class MergeStrategyTests(unittest.TestCase):
    def test_pass1_primary_keeps_pass1_and_fills_only_the_gaps(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "pass1.srt"
            second = root / "pass2.srt"
            merged = root / "merged.srt"
            _write_srt(first, [(0.0, 1.0, "いち"), (5.0, 6.0, "さん")])
            _write_srt(
                second,
                [
                    (0.5, 1.2, "overlapping"),
                    (2.0, 3.0, "にい"),
                    (5.5, 6.5, "overlapping too"),
                ],
            )

            stats = MergeEngine().merge(
                first, second, merged, strategy="pass1_primary"
            )
            texts = [cue.text for cue in parse_srt_cues(merged)]

        self.assertEqual(texts, ["いち", "にい", "さん"])
        self.assertEqual(stats["pass1_count"], 2)
        self.assertEqual(stats["merged_count"], 3)
        self.assertEqual(stats["strategy"], "pass1_primary")

    def test_consecutive_duplicates_are_dropped_after_merging(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "pass1.srt"
            second = root / "pass2.srt"
            merged = root / "merged.srt"
            _write_srt(first, [(0.0, 1.0, "はい")])
            _write_srt(second, [(2.0, 3.0, "はい")])

            stats = MergeEngine().merge(
                first, second, merged, strategy="pass1_primary"
            )

        self.assertEqual(stats["dedup_removed"], 1)
        self.assertEqual(stats["merged_count"], 1)


class RunEnsembleTests(unittest.TestCase):
    """Drive run_ensemble with stubbed passes — no models, no GPU."""

    def setUp(self) -> None:
        self.scenes = [(Path("scene_0000.wav"), 0.0, 10.0, 10.0)]

    def _run(self, root: Path, run_pass):
        with patch(
            "stt_to_subtitle.vendor.whisperjav.runner.detect_scenes",
            return_value=self.scenes,
        ):
            with patch(
                "stt_to_subtitle.vendor.whisperjav.runner.run_pass",
                side_effect=run_pass,
            ):
                return run_ensemble(
                    root / "audio.wav",
                    pass1=presets.pass1_config("anime", max_group_duration=2.0),
                    pass2=presets.pass2_config("qwen", max_group_duration=3.0),
                    work_dir=root / "work",
                )

    def test_both_passes_are_merged_into_cues(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def run_pass(config, scenes, work_dir, **_kwargs):
                path = work_dir / f"{config.name}.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                entries = (
                    [(0.0, 1.0, "いち")]
                    if config.name == "pass1"
                    else [(4.0, 5.0, "にい")]
                )
                _write_srt(path, entries)
                return PassOutcome(
                    name=config.name,
                    status="completed",
                    cues=parse_srt_cues(path),
                    srt_path=path,
                    subtitle_count=1,
                    elapsed_seconds=1.0,
                )

            result = self._run(root, run_pass)

        self.assertEqual(result.status, "completed")
        self.assertEqual([cue.text for cue in result.cues], ["いち", "にい"])
        self.assertEqual(result.scene_count, 1)
        self.assertIn("scene_detect", result.stage_elapsed)

    def test_a_failing_second_pass_degrades_to_the_first(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def run_pass(config, scenes, work_dir, **_kwargs):
                if config.name == "pass2":
                    raise RuntimeError("qwen exploded")
                path = work_dir / "pass1.srt"
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_srt(path, [(0.0, 1.0, "いち")])
                return PassOutcome(
                    name=config.name,
                    status="completed",
                    cues=parse_srt_cues(path),
                    srt_path=path,
                    subtitle_count=1,
                )

            with self.assertLogs(
                "stt_to_subtitle.vendor.whisperjav", level="WARNING"
            ) as captured:
                result = self._run(root, run_pass)

        self.assertIn("degrading to the pass1 result", captured.output[0])
        self.assertEqual(result.status, "degraded")
        self.assertEqual([cue.text for cue in result.cues], ["いち"])
        self.assertEqual(result.pass2.status, "failed")
        self.assertIn("qwen exploded", result.pass2.error)
        self.assertTrue(result.merge_stats["degraded"])

    def test_no_scenes_is_an_error_rather_than_an_empty_transcript(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.scenes = []
            with self.assertRaisesRegex(RuntimeError, "no scenes"):
                self._run(root, lambda *args, **kwargs: None)


class ParseSrtCuesTests(unittest.TestCase):
    def test_reads_multiline_text_and_both_decimal_separators(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "cues.srt"
            path.write_text(
                "1\n00:00:01,250 --> 00:00:02,500\nこんにちは\n世界\n\n"
                "2\n00:00:03.000 --> 00:00:04.000\nはい\n",
                encoding="utf-8",
            )
            cues = parse_srt_cues(path)

        self.assertEqual(len(cues), 2)
        self.assertEqual(cues[0], Cue(1.25, 2.5, "こんにちは\n世界"))
        self.assertEqual(cues[1].end, 4.0)

    def test_a_missing_file_yields_no_cues(self) -> None:
        self.assertEqual(parse_srt_cues(Path("/nonexistent.srt")), [])


if __name__ == "__main__":
    unittest.main()
