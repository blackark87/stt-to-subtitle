from pathlib import Path
import unittest

from stt_to_subtitle.audio import AudioExtraction, build_ffmpeg_command


class BuildFfmpegCommandTests(unittest.TestCase):
    def test_builds_16khz_mono_command_for_selected_range(self) -> None:
        command = build_ffmpeg_command(
            Path("/data/movie.mkv"),
            Path("/output/movie.wav"),
            AudioExtraction(
                audio_stream=1,
                start_seconds=30.5,
                duration_seconds=120.0,
            ),
        )

        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("0:a:1", command)
        self.assertIn("16000", command)
        self.assertIn("pcm_s16le", command)
        self.assertEqual(command[-1], str(Path("/output/movie.wav")))
        self.assertEqual(command[command.index("-ss") + 1], "30.5")
        self.assertEqual(command[command.index("-t") + 1], "120.0")

    def test_rejects_non_positive_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "duration_seconds"):
            AudioExtraction(duration_seconds=0).validate()
