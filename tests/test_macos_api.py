import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from fastapi.testclient import TestClient

from stt_to_subtitle.macos_api import (
    MacOSAPISettings,
    TranscriptionService,
    _device_unavailable_reason,
    _parse_options,
    _validate_wav,
    create_app,
)
from stt_to_subtitle.transcription_store import TranscriptionJob


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
        self.assertEqual(options["backend"], "kotoba")

    def test_accepts_request_level_whisperx_backend_case_insensitively(self) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        options = _parse_options('{"backend": "whisperX"}', settings)

        self.assertEqual(options["backend"], "whisperx")
        self.assertTrue(options["noise_filter"])
        self.assertTrue(
            options["subtitle_segmentation"]["split_on_speaker_change"]
        )
        self.assertEqual(options["repetition_policy"], "flag")

    def test_accepts_hybrid_backend_with_independent_chunk_defaults(self) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        options = _parse_options('{"backend": "hybrid"}', settings)

        self.assertEqual(options["backend"], "hybrid")
        self.assertEqual(options["chunk_length_seconds"], 15)
        self.assertEqual(
            options["hybrid_rescue"]["kotoba_chunk_length_seconds"], 15
        )
        self.assertEqual(
            options["hybrid_rescue"]["whisperx_chunk_length_seconds"], 30
        )
        self.assertEqual(
            options["subtitle_segmentation"]["max_duration_sec"], 8.0
        )
        self.assertEqual(options["repetition_policy"], "flag")

    def test_hybrid_reject_policy_is_invalid_because_rescue_needs_flags(
        self,
    ) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        with self.assertRaisesRegex(
            ValueError,
            "requires repetition_policy='flag'",
        ):
            _parse_options(
                '{"backend":"hybrid","repetition_policy":"reject"}',
                settings,
            )

    def test_accepts_configurable_whisperx_segmentation_and_reject_policy(
        self,
    ) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        options = _parse_options(
            json.dumps(
                {
                    "backend": "whisperx",
                    "subtitle_segmentation": {
                        "max_gap_sec": 0.7,
                        "max_duration_sec": 8.0,
                        "max_chars": 36,
                    },
                    "repetition_policy": "reject",
                    "repetition_min_count": 12,
                }
            ),
            settings,
        )

        self.assertEqual(options["subtitle_segmentation"]["max_gap_sec"], 0.7)
        self.assertEqual(options["subtitle_segmentation"]["max_chars"], 36)
        self.assertEqual(options["repetition_policy"], "reject")
        self.assertEqual(options["repetition_min_count"], 12)

    def test_short_span_policy_is_observation_only(self) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        with self.assertRaisesRegex(ValueError, "non-destructive 'observe'"):
            _parse_options('{"short_span_policy":"drop"}', settings)

    def test_rejects_unknown_transcription_backend(self) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        with self.assertRaisesRegex(
            ValueError,
            "backend must be 'kotoba', 'whisperx', or 'hybrid'",
        ):
            _parse_options('{"backend": "other"}', settings)

    def test_whisperx_backend_requires_vad(self) -> None:
        settings = MacOSAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        with self.assertRaisesRegex(
            ValueError,
            "WhisperX backend requires noise_filter=true",
        ):
            _parse_options(
                '{"backend": "whisperx", "noise_filter": false}',
                settings,
            )

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

    def test_whisperx_worker_is_isolated_and_releases_kotoba(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            whisperx_python = root / "python"
            whisperx_python.write_text("placeholder", encoding="utf-8")
            settings = MacOSAPISettings(
                state_dir=root / "state",
                api_token="",
                hf_token="secret-hf-token",
                device="cpu",
                diarization_device="cpu",
                whisperx_python=whisperx_python,
            )
            service = TranscriptionService(settings)
            job = TranscriptionJob(
                id="job-id",
                idempotency_key="key",
                status="running",
                audio_path=str(root / "audio.wav"),
                audio_sha256="hash",
                options={
                    "backend": "whisperx",
                    "batch_size": 1,
                    "chunk_length_seconds": 30,
                },
                result_path=None,
                error=None,
                chunks_created=0,
                chunks_completed=0,
                created_at=0.0,
                updated_at=0.0,
            )

            def fake_run(command, **kwargs):
                output = Path(command[command.index("--output") + 1])
                output.write_text(
                    json.dumps(
                        {
                            "model": {"id": "large-v3"},
                            "timing": {"postprocessor": "whisperx"},
                            "runtime": {"backend": "whisperx"},
                            "noise_filter": {"enabled": True},
                            "segments": [],
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(service, "_release_pipeline") as release:
                with patch(
                    "stt_to_subtitle.macos_api.subprocess.run",
                    side_effect=fake_run,
                ) as run:
                    result = service._run_whisperx_worker(job)

            release.assert_called_once_with()
            command = run.call_args.args[0]
            environment = run.call_args.kwargs["env"]
            self.assertNotIn("secret-hf-token", command)
            self.assertEqual(environment["HF_TOKEN"], "secret-hf-token")
            self.assertEqual(result["model"]["id"], "large-v3")

            with patch.object(service, "_release_pipeline") as release:
                with patch(
                    "stt_to_subtitle.macos_api.subprocess.run",
                    side_effect=fake_run,
                ) as run:
                    service._run_whisperx_worker(
                        job,
                        release_kotoba=False,
                        options={
                            **job.options,
                            "chunk_length_seconds": 30,
                        },
                    )

            release.assert_not_called()
            command = run.call_args.args[0]
            worker_options = json.loads(
                command[command.index("--options") + 1]
            )
            self.assertEqual(worker_options["chunk_length_seconds"], 30)

    def test_hybrid_job_runs_both_backends_and_rescues_failed_window(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            audio_path = root / "audio.wav"
            with wave.open(str(audio_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 16000)
            settings = MacOSAPISettings(
                state_dir=state_dir,
                api_token="",
                hf_token="hf-token",
                device="cpu",
                diarization_device="cpu",
                debug_artifacts=True,
            )
            service = TranscriptionService(settings)
            options = _parse_options('{"backend":"hybrid"}', settings)
            service.store.create(
                job_id="hybrid-job",
                idempotency_key="hybrid-key",
                audio_path=audio_path,
                audio_sha256="hybrid-sha",
                options=options,
            )
            primary_result = {
                "model": {"id": "large-v3", "revision": "whisperx-test"},
                "timing": {"postprocessor": "whisperx-align"},
                "runtime": {"backend": "whisperx", "device": "cpu"},
                "noise_filter": {
                    "enabled": True,
                    "provider": "whisperx-vad",
                },
                "quality": {"encoding_warning": {"flagged": False}},
                "words": [],
                "segments": [
                    {
                        "start": 0.1,
                        "end": 0.9,
                        "speaker": "WX_A",
                        "text": "い" * 8,
                        "provider": "whisperx",
                    }
                ],
            }
            fallback_result = {
                "chunks": [
                    {
                        "timestamp": [0.0, 1.0],
                        "speaker_id": "K_A",
                        "text": "はい",
                    }
                ],
                "timestamp_postprocessor": "kotoba-test",
                "noise_filter": {
                    "enabled": True,
                    "provider": "kotoba-noise-filter-v1",
                },
            }

            with patch.object(
                service, "_get_pipeline", return_value=Mock()
            ) as get_pipeline:
                with patch.object(
                    service,
                    "_run_whisperx_worker",
                    return_value=primary_result,
                ) as run_whisperx:
                    with patch(
                        "stt_to_subtitle.macos_api.run_pipeline",
                        return_value=fallback_result,
                    ) as run_kotoba:
                        service._run_job("hybrid-job")

            get_pipeline.assert_called_once_with()
            run_whisperx.assert_called_once()
            self.assertFalse(
                run_whisperx.call_args.kwargs["release_kotoba"]
            )
            self.assertEqual(
                run_whisperx.call_args.kwargs["options"][
                    "chunk_length_seconds"
                ],
                30,
            )
            self.assertEqual(
                run_kotoba.call_args.args[2].chunk_length_seconds,
                15,
            )
            completed = service.store.get("hybrid-job")
            payload = json.loads(
                Path(completed.result_path).read_text(encoding="utf-8")
            )
            request_trace = json.loads(
                (
                    state_dir
                    / "artifacts"
                    / "hybrid-job"
                    / "00_request.json"
                ).read_text(encoding="utf-8")
            )

            self.assertEqual(payload["runtime"]["backend"], "hybrid")
            self.assertTrue(
                payload["runtime"]["simultaneous_model_residency"]
            )
            self.assertEqual(payload["segments"][0]["text"], "はい")
            self.assertEqual(
                payload["segments"][0]["provider"], "hybrid-rescue-v1"
            )
            self.assertEqual(
                payload["segments"][0]["source_segment_id"],
                "kotoba-segment-000001",
            )
            self.assertEqual(
                payload["segments"][0]["rescue_window_id"],
                "rescue-window-000001",
            )
            self.assertEqual(
                payload["quality"]["hybrid"]["replaced_window_count"],
                1,
            )
            self.assertEqual(
                request_trace["option_semantics"]["stt_call_count"], 2
            )

    def test_completed_job_contains_additive_trace_and_debug_metrics(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            audio_path = root / "audio.wav"
            with wave.open(str(audio_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 16000)
            service = TranscriptionService(
                MacOSAPISettings(
                    state_dir=state_dir,
                    api_token="",
                    hf_token="hf-token",
                    device="cpu",
                    diarization_device="cpu",
                    debug_artifacts=True,
                )
            )
            service.store.create(
                job_id="job-id",
                idempotency_key="key",
                audio_path=audio_path,
                audio_sha256="abc",
                options={
                    "backend": "kotoba",
                    "batch_size": 1,
                    "chunk_length_seconds": 15,
                    "num_speakers": None,
                    "min_speakers": None,
                    "max_speakers": None,
                    "add_punctuation": False,
                    "noise_filter": True,
                    "noise_filter_trigger_level": 7.0,
                    "short_span_policy": "observe",
                    "threads": None,
                },
            )
            result = {
                "chunks": [
                    {
                        "timestamp": [0.0, 1.0],
                        "speaker_id": "SPEAKER_00",
                        "text": "はい",
                    }
                ],
                "timestamp_postprocessor": "test-postprocessor",
                "noise_filter": {
                    "provider": "kotoba-noise-filter-v1",
                    "execution_state": "run_no_removal",
                    "enabled": True,
                    "candidate_count": 1,
                    "kept_count": 1,
                    "removed_count": 0,
                    "removed_spans": [],
                },
            }

            with patch.object(service, "_get_pipeline", return_value=Mock()):
                with patch(
                    "stt_to_subtitle.macos_api.run_pipeline",
                    return_value=result,
                ):
                    service._run_job("job-id")

            completed = service.store.get("job-id")
            payload = json.loads(
                Path(completed.result_path).read_text(encoding="utf-8")
            )
            artifact_dir = state_dir / "artifacts" / "job-id"

            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["trace_schema_version"], "stt-trace-v1")
            self.assertEqual(payload["input"]["delivery_mode"], "single_wav")
            self.assertEqual(payload["input"]["duration_sec"], 1.0)
            self.assertEqual(payload["request"]["attempt"], 1)
            self.assertEqual(
                payload["noise_filter"]["execution_state"],
                "run_no_removal",
            )
            self.assertTrue((artifact_dir / "00_request.json").is_file())
            self.assertTrue((artifact_dir / "00_runtime.json").is_file())
            self.assertTrue(
                (artifact_dir / "metrics" / "metric_result.json").is_file()
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

    def test_whisperx_request_reports_missing_isolated_python(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            wav_path = root / "audio.wav"
            with wave.open(str(wav_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 100)
            settings = MacOSAPISettings(
                state_dir=root / "state",
                api_token="",
                hf_token="hf-token",
                device="cpu",
                diarization_device="cpu",
                whisperx_python=root / "missing-python",
            )

            with patch.dict("sys.modules", {"torch": SimpleNamespace()}):
                with TestClient(create_app(settings)) as client:
                    ready = client.get("/readyz")
                    response = client.post(
                        "/v1/transcriptions",
                        headers={"Idempotency-Key": "whisperx-missing"},
                        files={
                            "audio": (
                                "audio.wav",
                                wav_path.read_bytes(),
                                "audio/wav",
                            )
                        },
                        data={"options": '{"backend":"whisperx"}'},
                    )

            self.assertEqual(ready.status_code, 200)
            self.assertEqual(
                ready.json()["backends"]["whisperx"]["status"],
                "unavailable",
            )
            self.assertEqual(
                ready.json()["backends"]["hybrid"]["status"],
                "unavailable",
            )
            self.assertEqual(response.status_code, 503)
            self.assertIn("WhisperX Python was not found", response.text)
