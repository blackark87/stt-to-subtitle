from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import wave

from stt_to_subtitle.kotoba_worker import _validated_windows, run_kotoba


class KotobaWorkerTests(unittest.TestCase):
    def test_rejects_an_invalid_rescue_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "greater than start"):
            _validated_windows([{"start": 2.0, "end": 1.0}])

    def test_loads_one_pipeline_for_all_rescue_windows(self) -> None:
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "audio.wav"
            with wave.open(str(audio_path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\x00\x00" * 16000 * 4)
            windows = [
                {"start": 0.0, "end": 1.0, "window_id": "window-1"},
                {"start": 2.0, "end": 3.0, "window_id": "window-2"},
            ]
            pipeline = Mock()
            results = [
                {
                    "chunks": [
                        {
                            "timestamp": [0.1, 0.8],
                            "speaker_id": "A",
                            "text": "一",
                        }
                    ]
                },
                {
                    "chunks": [
                        {
                            "timestamp": [0.2, 0.9],
                            "speaker_id": "B",
                            "text": "二",
                        }
                    ]
                },
            ]

            with patch(
                "stt_to_subtitle.kotoba_worker.load_pipeline",
                return_value=pipeline,
            ) as load_pipeline:
                with patch(
                    "stt_to_subtitle.kotoba_worker.run_pipeline",
                    side_effect=results,
                ) as run_pipeline:
                    payload = run_kotoba(
                        audio_path,
                        {"batch_size": 2, "chunk_length_seconds": 15},
                        windows=windows,
                    )

            load_pipeline.assert_called_once()
            self.assertEqual(run_pipeline.call_count, 2)
            self.assertEqual(payload["mode"], "windows")
            self.assertAlmostEqual(
                payload["windows"][0]["segments"][0]["start"],
                0.1,
            )
            self.assertAlmostEqual(
                payload["windows"][1]["segments"][0]["start"],
                2.2,
            )


if __name__ == "__main__":
    unittest.main()
