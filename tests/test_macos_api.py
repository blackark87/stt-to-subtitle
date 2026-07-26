from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import wave

from fastapi.testclient import TestClient

from stt_to_subtitle.macos_api import (
    MacOSAPISettings,
    _parse_options,
    _validate_wav,
    create_app,
)


class MacOSAPIHelpersTests(unittest.TestCase):
    def test_parses_client_options_but_keeps_server_batch_size(self) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="api-token",
            hf_token="hf-token",
            batch_size=2,
        )

        options = _parse_options(
            '{"chunk_length_seconds": 20, "num_speakers": 2}',
            settings,
        )

        self.assertEqual(options["batch_size"], 2)
        self.assertEqual(options["chunk_length_seconds"], 20)
        self.assertEqual(options["num_speakers"], 2)

    def test_accepts_16khz_mono_pcm_wav(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "audio.wav"
            with wave.open(str(path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 100)

            _validate_wav(path)

    def test_chunk_progress_interval_accepts_only_10_or_100(self) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
            chunk_progress_every=25,
        )

        with self.assertRaisesRegex(
            ValueError,
            "STT_CHUNK_PROGRESS_EVERY must be 10 or 100",
        ):
            settings.validate()


class MacOSAPIRouteTests(unittest.TestCase):
    def test_health_is_public_and_job_status_requires_bearer_token(self) -> None:
        with TemporaryDirectory() as directory:
            settings = MacOSAPISettings(
                state_dir=Path(directory),
                api_token="api-token",
                hf_token="hf-token",
            )
            with TestClient(create_app(settings)) as client:
                health = client.get("/healthz")
                unauthorized = client.get("/v1/transcriptions/missing")

            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["status"], "ok")
            self.assertEqual(unauthorized.status_code, 401)

    def test_blank_api_token_disables_bearer_authentication(self) -> None:
        with TemporaryDirectory() as directory:
            settings = MacOSAPISettings(
                state_dir=Path(directory),
                api_token="",
                hf_token="hf-token",
            )
            with TestClient(create_app(settings)) as client:
                response = client.get("/v1/transcriptions/missing")

            self.assertEqual(response.status_code, 404)
