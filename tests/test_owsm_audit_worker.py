from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import wave

from stt_to_subtitle.owsm_audit_worker import (
    _owsm_text,
    padded_wav_window,
    window_ranges,
)


class OWSMAuditWorkerTests(unittest.TestCase):
    def test_window_ranges_cover_audio_with_overlap(self) -> None:
        self.assertEqual(
            window_ranges(
                70.0,
                window_seconds=30.0,
                overlap_seconds=5.0,
            ),
            [(0.0, 30.0), (25.0, 55.0), (50.0, 70.0)],
        )

    def test_padded_window_is_fixed_length_pcm(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "source.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\x01\x00" * 16000)

            with padded_wav_window(
                source,
                start_seconds=0.5,
                end_seconds=1.0,
                padded_duration_seconds=2.0,
            ) as window_path:
                with wave.open(str(window_path), "rb") as audio:
                    self.assertEqual(audio.getnframes(), 32000)
                    frames = audio.readframes(32000)

            self.assertEqual(frames[:2], b"\x01\x00")
            self.assertEqual(frames[-2:], b"\x00\x00")

    def test_extracts_text_and_removes_language_prefix(self) -> None:
        self.assertEqual(
            _owsm_text([["<jpn><asr> 聞こえます"]]),
            "聞こえます",
        )


if __name__ == "__main__":
    unittest.main()
