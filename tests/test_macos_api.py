from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import wave

from fastapi.testclient import TestClient

from stt_to_subtitle.macos_api import (
    MacOSAPISettings,
    _device_unavailable_reason,
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
        self.assertTrue(options["noise_filter"])

    def test_defaults_to_sixty_seconds_and_accepts_disabled_filter(self) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        defaults = _parse_options("{}", settings)
        disabled = _parse_options('{"noise_filter": false}', settings)

        self.assertEqual(defaults["chunk_length_seconds"], 60)
        self.assertTrue(defaults["noise_filter"])
        self.assertFalse(disabled["noise_filter"])
        self.assertEqual(defaults["noise_filter_trigger_level"], 7.0)

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

    def test_noise_filter_trigger_level_must_be_positive(self) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
            noise_filter_trigger_level=0,
        )

        with self.assertRaisesRegex(
            ValueError,
            "STT_NOISE_FILTER_TRIGGER_LEVEL must be positive",
        ):
            settings.validate()

    def test_accepts_cuda_devices_with_optional_indexes(self) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
            device="cuda:0",
            diarization_device="cuda:1",
        )

        settings.validate()

    def test_rejects_unsupported_device(self) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
            device="auto",
        )

        with self.assertRaisesRegex(
            ValueError,
            "STT_DEVICE must be cpu, mps, cuda",
        ):
            settings.validate()

    def test_reports_unavailable_cuda_runtime(self) -> None:
        torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=Mock(return_value=False)),
        )

        reason = _device_unavailable_reason(torch, "cuda")

        self.assertEqual(reason, "PyTorch CUDA is not available")

    def test_reports_out_of_range_cuda_device_index(self) -> None:
        torch = SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=Mock(return_value=True),
                device_count=Mock(return_value=1),
            ),
        )

        reason = _device_unavailable_reason(torch, "cuda:1")

        self.assertEqual(
            reason,
            "CUDA device cuda:1 is not available; found 1 CUDA device(s)",
        )


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
