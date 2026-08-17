import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import call, patch

from stt_to_subtitle.whisperjav_worker import (
    ALIGNER_MODEL_REVISION,
    ANIME_MODEL_REVISION,
    QWEN_MODEL_REVISION,
    WHISPERJAV_COMMIT,
    WhisperJAVOptions,
    build_whisperjav_command,
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

    def test_builds_fixed_two_pass_recipe(self) -> None:
        options = WhisperJAVOptions.from_options(
            {
                "whisperjav": {
                    "anime_max_group_duration_seconds": 2.5,
                    "qwen_max_group_duration_seconds": 4.0,
                }
            }
        )
        command = build_whisperjav_command(
            python=Path("/venv/bin/python"),
            audio_path=Path("audio.wav"),
            output_dir=Path("output"),
            temp_dir=Path("temp"),
            anime_model_path=Path("anime-model"),
            qwen_model_path=Path("qwen-model"),
            options=options,
        )

        pass1 = json.loads(command[command.index("--pass1-qwen-params") + 1])
        pass2 = json.loads(command[command.index("--pass2-qwen-params") + 1])
        self.assertIn("--ensemble-serial", command)
        self.assertEqual(pass1["generator_backend"], "anime-whisper")
        self.assertEqual(pass1["model_id"], "anime-model")
        self.assertEqual(pass1["max_group_duration"], 2.5)
        self.assertFalse(pass1["use_aligner"])
        self.assertEqual(pass2["generator_backend"], "qwen3")
        self.assertEqual(pass2["model_id"], "qwen-model")
        self.assertEqual(pass2["max_group_duration"], 4.0)
        self.assertEqual(
            command[command.index("--merge-strategy") + 1],
            "pass1_primary",
        )

    def test_reports_pinned_models_after_ensemble_and_alignment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "audio.wav"
            audio_path.write_bytes(b"not-read-by-mocks")
            snapshots = {
                "litagin/anime-whisper": root / "anime",
                "jaykwok/Qwen3-ASR-1.7B-JA-Anime-Galgame": root / "qwen",
                "Qwen/Qwen3-ForcedAligner-0.6B": root / "aligner",
            }

            def fake_run(command, **kwargs):
                temp_dir = Path(command[command.index("--temp-dir") + 1])
                output_dir = Path(command[command.index("--output-dir") + 1])
                final_srt = output_dir / "final.srt"
                final_srt.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nはい\n",
                    encoding="utf-8",
                )
                (temp_dir / "ensemble_summary_test.json").write_text(
                    json.dumps(
                        {
                            "files": [
                                {
                                    "status": "completed",
                                    "final_output": str(final_srt),
                                    "pass1": {"status": "completed"},
                                    "pass2": {"status": "completed"},
                                    "merge": {"strategy": "pass1_primary"},
                                }
                            ]
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch(
                "stt_to_subtitle.whisperjav_worker._snapshot",
                side_effect=lambda model_id, revision: snapshots[model_id],
            ) as snapshot:
                with patch(
                    "stt_to_subtitle.whisperjav_worker.subprocess.run",
                    side_effect=fake_run,
                ):
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
                        result = run_whisperjav(audio_path, {})

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


if __name__ == "__main__":
    unittest.main()
