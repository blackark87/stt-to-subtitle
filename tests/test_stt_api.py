import asyncio
import os
import json
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

from fastapi.testclient import TestClient

from stt_to_subtitle import __version__
from stt_to_subtitle.stt_api import (
    STTAPISettings,
    TranscriptionChangeHook,
    TranscriptionService,
    _device_unavailable_reason,
    _parse_options,
    _validate_wav,
    create_app,
)
from stt_to_subtitle.transcription_store import (
    TranscriptionJob,
    TranscriptionStore,
)


class TranscriptionChangeHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_wakes_only_the_changed_transcription_job(self) -> None:
        hook = TranscriptionChangeHook(asyncio.get_running_loop())
        first_version = hook.version("job-1")

        hook.publish("job-1")
        updated = await hook.wait("job-1", first_version, timeout=0.1)

        self.assertEqual(updated, first_version + 1)
        self.assertEqual(hook.version("job-2"), 0)


class STTAPIHelpersTests(unittest.TestCase):
    def test_service_uses_separate_work_storage_and_rebases_uploads(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            work_dir = root / "work"
            store = TranscriptionStore(state_dir / "jobs.sqlite3")
            store.create(
                job_id="legacy-job",
                idempotency_key="legacy-key",
                audio_path=state_dir / "incoming" / "legacy-job.wav",
                audio_sha256="abc",
                options={},
            )

            service = TranscriptionService(
                STTAPISettings(
                    state_dir=state_dir,
                    work_dir=work_dir,
                    api_token="",
                    hf_token="hf-token",
                    device="cpu",
                    diarization_device="cpu",
                )
            )

            self.assertEqual(service.incoming_dir, work_dir)
            self.assertTrue(work_dir.is_dir())
            self.assertEqual(
                service.store.get("legacy-job").audio_path,
                str(work_dir / "legacy-job.wav"),
            )

    def test_reads_a_separate_stt_work_directory(self) -> None:
        with patch.dict(
            os.environ,
            {
                "HF_TOKEN": "hf-token",
                "STT_STATE_DIR": "/state",
                "STT_WORK_DIR": "/work",
            },
            clear=True,
        ):
            settings = STTAPISettings.from_env()

        self.assertEqual(settings.state_dir, Path("/state"))
        self.assertEqual(settings.incoming_dir, Path("/work"))

    def test_exposes_the_package_version(self) -> None:
        app = create_app(
            STTAPISettings(
                state_dir=Path("/tmp/not-used"),
                api_token="",
                hf_token="",
            )
        )

        self.assertEqual(app.version, __version__)

    def test_parses_client_options_and_keeps_server_batch_size_for_kotoba(
        self,
    ) -> None:
        settings = STTAPISettings(
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

    def test_uses_whisperx_batch_size_default_for_whisperx_paths(self) -> None:
        settings = STTAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
            batch_size=2,
            whisperx_batch_size=8,
        )

        for backend in ("whisperx", "hybrid"):
            with self.subTest(backend=backend):
                options = _parse_options(
                    json.dumps({"backend": backend}),
                    settings,
                )

                self.assertEqual(options["batch_size"], 8)

    def test_allows_per_job_batch_size_for_whisperx_paths(self) -> None:
        settings = STTAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
            whisperx_batch_size=8,
        )

        for backend in ("whisperx", "hybrid"):
            with self.subTest(backend=backend):
                options = _parse_options(
                    json.dumps({"backend": backend, "batch_size": 16}),
                    settings,
                )

                self.assertEqual(options["batch_size"], 16)

    def test_rejects_per_job_batch_size_outside_the_supported_range(
        self,
    ) -> None:
        settings = STTAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        for value in (0, 65):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "between 1 and 64"):
                    _parse_options(
                        json.dumps(
                            {"backend": "whisperx", "batch_size": value}
                        ),
                        settings,
                    )

    def test_rejects_per_job_batch_size_for_load_time_backends(self) -> None:
        settings = STTAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        for backend in ("kotoba", "whisperjav"):
            with self.subTest(backend=backend):
                with self.assertRaisesRegex(
                    ValueError, "fixed at pipeline load"
                ):
                    _parse_options(
                        json.dumps({"backend": backend, "batch_size": 4}),
                        settings,
                    )

    def test_accepts_request_level_whisperx_backend_case_insensitively(self) -> None:
        settings = STTAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        options = _parse_options('{"backend": "whisperX"}', settings)

        self.assertEqual(options["backend"], "whisperx")
        self.assertEqual(options["chunk_length_seconds"], 30)
        self.assertTrue(options["noise_filter"])
        self.assertTrue(
            options["subtitle_segmentation"]["split_on_speaker_change"]
        )
        self.assertEqual(options["repetition_policy"], "flag")

    def test_rejects_whisperx_chunks_longer_than_native_window(self) -> None:
        settings = STTAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        with self.assertRaisesRegex(ValueError, "must be at most 30"):
            _parse_options(
                '{"backend":"whisperx","chunk_length_seconds":31}',
                settings,
            )
        with self.assertRaisesRegex(ValueError, "must be at most 30"):
            _parse_options(
                json.dumps(
                    {
                        "backend": "hybrid",
                        "hybrid_rescue": {
                            "whisperx_chunk_length_seconds": 31,
                        },
                    }
                ),
                settings,
            )

    def test_accepts_hybrid_backend_with_independent_chunk_defaults(self) -> None:
        settings = STTAPISettings(
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

    def test_accepts_fixed_whisperjav_recipe_with_group_overrides(self) -> None:
        settings = STTAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        options = _parse_options(
            json.dumps(
                {
                    "backend": "whisperjav",
                    "whisperjav": {
                        "anime_max_group_duration_seconds": 2.5,
                        "qwen_max_group_duration_seconds": 4.0,
                    },
                }
            ),
            settings,
        )

        self.assertEqual(options["backend"], "whisperjav")
        self.assertEqual(
            options["whisperjav"]["recipe"],
            "whisperjav-domain-ensemble-v1",
        )
        self.assertEqual(
            options["whisperjav"]["anime_max_group_duration_seconds"],
            2.5,
        )
        self.assertEqual(
            options["whisperjav"]["qwen_max_group_duration_seconds"],
            4.0,
        )
        self.assertTrue(
            options["subtitle_segmentation"]["split_on_speaker_change"]
        )

        with self.assertRaisesRegex(ValueError, "must be between"):
            _parse_options(
                '{"backend":"whisperjav","whisperjav":'
                '{"anime_max_group_duration_seconds":0.1}}',
                settings,
            )

    def test_hybrid_reject_policy_is_invalid_because_rescue_needs_flags(
        self,
    ) -> None:
        settings = STTAPISettings(
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
        settings = STTAPISettings(
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
        settings = STTAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        with self.assertRaisesRegex(ValueError, "non-destructive 'observe'"):
            _parse_options('{"short_span_policy":"drop"}', settings)

    def test_rejects_unknown_transcription_backend(self) -> None:
        settings = STTAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
        )

        with self.assertRaisesRegex(
            ValueError,
            "backend must be 'kotoba', 'whisperx', 'hybrid', or "
            "'whisperjav'",
        ):
            _parse_options('{"backend": "other"}', settings)

    def test_whisperx_backend_requires_vad(self) -> None:
        settings = STTAPISettings(
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
        settings = STTAPISettings(
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
        settings = STTAPISettings(
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
        settings = STTAPISettings(
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
        settings = STTAPISettings(
            state_dir=Path("/tmp/not-used"),
            api_token="",
            hf_token="hf-token",
            device="cuda:0",
            diarization_device="cuda:1",
        )

        settings.validate()

    def test_rejects_unsupported_device(self) -> None:
        settings = STTAPISettings(
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
            settings = STTAPISettings(
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
                    "stt_to_subtitle.stt_api.subprocess.run",
                    side_effect=fake_run,
                ) as run:
                    result = service._run_whisperx_worker(job)

            release.assert_called_once_with()
            command = run.call_args.args[0]
            environment = run.call_args.kwargs["env"]
            self.assertNotIn("secret-hf-token", command)
            self.assertEqual(
                environment.get("PYTHONPATH"),
                os.environ.get("PYTHONPATH"),
            )
            self.assertEqual(environment["HF_TOKEN"], "secret-hf-token")
            self.assertEqual(environment["PYTHONIOENCODING"], "utf-8")
            self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
            self.assertEqual(run.call_args.kwargs["errors"], "replace")
            self.assertEqual(result["model"]["id"], "large-v3")

            with patch.object(service, "_release_pipeline") as release:
                with patch(
                    "stt_to_subtitle.stt_api.subprocess.run",
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

    def test_whisperjav_worker_runs_ensemble_then_speaker_assignment(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            whisperjav_python = root / "whisperjav-python"
            whisperx_python = root / "whisperx-python"
            whisperjav_python.write_text("placeholder", encoding="utf-8")
            whisperx_python.write_text("placeholder", encoding="utf-8")
            settings = STTAPISettings(
                state_dir=root / "state",
                api_token="",
                hf_token="secret-hf-token",
                device="cpu",
                diarization_device="cpu",
                whisperjav_python=whisperjav_python,
                whisperx_python=whisperx_python,
            )
            service = TranscriptionService(settings)
            job = TranscriptionJob(
                id="whisperjav-job",
                idempotency_key="key",
                status="running",
                audio_path=str(root / "audio.wav"),
                audio_sha256="hash",
                options={"backend": "whisperjav", "whisperjav": {}},
                result_path=None,
                error=None,
                chunks_created=0,
                chunks_completed=0,
                created_at=0.0,
                updated_at=0.0,
            )

            commands = []

            def fake_run(command, **kwargs):
                commands.append((command, kwargs))
                output = Path(command[command.index("--output") + 1])
                if "stt_to_subtitle.whisperjav_worker" in command:
                    output.write_text('{"words":[]}', encoding="utf-8")
                else:
                    output.write_text(
                        json.dumps(
                            {
                                "model": {"id": "whisperjav-domain-ensemble"},
                                "timing": {"postprocessor": "qwen3"},
                                "runtime": {"backend": "whisperjav"},
                                "noise_filter": {"enabled": True},
                                "words": [],
                                "segments": [],
                            }
                        ),
                        encoding="utf-8",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch.object(service, "_release_pipeline") as release:
                with patch(
                    "stt_to_subtitle.stt_api.subprocess.run",
                    side_effect=fake_run,
                ):
                    result = service._run_whisperjav_worker(job)

            release.assert_called_once_with()
            self.assertEqual(len(commands), 2)
            self.assertEqual(commands[0][0][0], str(whisperjav_python))
            self.assertEqual(commands[1][0][0], str(whisperx_python))
            self.assertIn(
                "stt_to_subtitle.whisperjav_worker", commands[0][0]
            )
            self.assertIn("stt_to_subtitle.speaker_worker", commands[1][0])
            self.assertNotIn("secret-hf-token", commands[0][0])
            self.assertEqual(
                commands[0][1]["env"]["HF_TOKEN"], "secret-hf-token"
            )
            self.assertEqual(result["model"]["id"], "whisperjav-domain-ensemble")

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
            settings = STTAPISettings(
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
                        "stt_to_subtitle.stt_api.run_pipeline",
                        return_value=fallback_result,
                    ) as run_kotoba:
                        service._run_job("hybrid-job")

            get_pipeline.assert_called_once_with()
            run_whisperx.assert_called_once()
            # Kotoba must not stay resident while WhisperX batches on the GPU.
            self.assertTrue(
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
            self.assertFalse(
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

    def test_hybrid_window_scope_decodes_only_the_rescue_span(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            audio_path = root / "audio.wav"
            with wave.open(str(audio_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 16000 * 40)
            settings = STTAPISettings(
                state_dir=state_dir,
                api_token="",
                hf_token="hf-token",
                device="cpu",
                diarization_device="cpu",
            )
            service = TranscriptionService(settings)
            options = _parse_options(
                '{"backend":"hybrid",'
                '"hybrid_rescue":{"rescue_scope":"windows"}}',
                settings,
            )
            service.store.create(
                job_id="window-job",
                idempotency_key="window-key",
                audio_path=audio_path,
                audio_sha256="window-sha",
                options=options,
            )
            primary_result = {
                "model": {"id": "large-v3", "revision": "whisperx-test"},
                "timing": {"postprocessor": "whisperx-align"},
                "runtime": {"backend": "whisperx", "device": "cpu"},
                "noise_filter": {"enabled": True, "provider": "whisperx-vad"},
                "quality": {"encoding_warning": {"flagged": False}},
                "words": [],
                "segments": [
                    {
                        "start": 20.0,
                        "end": 20.9,
                        "speaker": "WX_A",
                        "text": "い" * 8,
                        "provider": "whisperx",
                    }
                ],
            }
            window_result = {
                "chunks": [
                    {
                        "timestamp": [5.0, 6.0],
                        "speaker_id": "K_A",
                        "text": "はい",
                    }
                ],
                "timestamp_postprocessor": "kotoba-test",
            }

            with patch.object(service, "_get_pipeline", return_value=Mock()):
                with patch.object(
                    service,
                    "_run_whisperx_worker",
                    return_value=primary_result,
                ):
                    with patch(
                        "stt_to_subtitle.stt_api.run_pipeline",
                        return_value=window_result,
                    ) as run_kotoba:
                        service._run_job("window-job")

            run_kotoba.assert_called_once()
            sliced = run_kotoba.call_args.args[1]
            self.assertNotEqual(sliced, audio_path)
            self.assertTrue(sliced.name.startswith("window-"))

            completed = service.store.get("window-job")
            payload = json.loads(
                Path(completed.result_path).read_text(encoding="utf-8")
            )
            rescued = payload["segments"][0]
            self.assertEqual(rescued["text"], "はい")
            self.assertAlmostEqual(rescued["start"], 20.0, places=3)
            self.assertAlmostEqual(rescued["end"], 21.0, places=3)
            self.assertEqual(payload["runtime"]["rescue"]["scope"], "windows")
            self.assertEqual(
                payload["noise_filter"]["rescue"]["window_count"], 1
            )
            self.assertLess(
                payload["noise_filter"]["rescue"]["decoded_seconds"],
                40.0,
            )

    def test_hybrid_job_skips_kotoba_when_no_window_needs_rescue(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "audio.wav"
            with wave.open(str(audio_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 16000)
            settings = STTAPISettings(
                state_dir=root / "state",
                api_token="",
                hf_token="hf-token",
                device="cpu",
                diarization_device="cpu",
            )
            service = TranscriptionService(settings)
            options = _parse_options('{"backend":"hybrid"}', settings)
            service.store.create(
                job_id="clean-hybrid-job",
                idempotency_key="clean-hybrid-key",
                audio_path=audio_path,
                audio_sha256="clean-sha",
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
                        "text": "こんにちは",
                        "provider": "whisperx",
                    }
                ],
            }

            with patch.object(
                service, "_get_pipeline", return_value=Mock()
            ) as get_pipeline:
                with patch.object(
                    service,
                    "_run_whisperx_worker",
                    return_value=primary_result,
                ):
                    with patch(
                        "stt_to_subtitle.stt_api.run_pipeline",
                    ) as run_kotoba:
                        service._run_job("clean-hybrid-job")

            run_kotoba.assert_not_called()
            get_pipeline.assert_not_called()
            completed = service.store.get("clean-hybrid-job")
            self.assertEqual(completed.status, "completed")
            payload = json.loads(
                Path(completed.result_path).read_text(encoding="utf-8")
            )

            self.assertTrue(payload["runtime"]["kotoba_skipped"])
            self.assertTrue(payload["runtime"]["rescue"]["skipped"])
            self.assertEqual(
                payload["runtime"]["rescue"]["elapsed_seconds"], 0.0
            )
            self.assertEqual(
                payload["noise_filter"]["rescue"]["execution_state"],
                "skipped",
            )
            self.assertEqual(
                [segment["text"] for segment in payload["segments"]],
                ["こんにちは"],
            )
            self.assertEqual(
                payload["quality"]["hybrid"]["replaced_window_count"], 0
            )
            self.assertEqual(
                payload["quality"]["hybrid"]["fallback_segment_count"], 0
            )


    def test_whisperjav_job_persists_aligned_speaker_result(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            audio_path = root / "audio.wav"
            with wave.open(str(audio_path), "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(16000)
                wav_file.writeframes(b"\x00\x00" * 16000)
            settings = STTAPISettings(
                state_dir=root / "state",
                api_token="",
                hf_token="hf-token",
                device="cpu",
                diarization_device="cpu",
                debug_artifacts=True,
            )
            service = TranscriptionService(settings)
            options = _parse_options('{"backend":"whisperjav"}', settings)
            service.store.create(
                job_id="whisperjav-job",
                idempotency_key="whisperjav-key",
                audio_path=audio_path,
                audio_sha256="whisperjav-sha",
                options=options,
            )
            worker_result = {
                "model": {
                    "id": "whisperjav-domain-ensemble",
                    "revision": "pinned",
                },
                "timing": {"postprocessor": "qwen3-forced-alignment"},
                "runtime": {"backend": "whisperjav"},
                "noise_filter": {"enabled": True, "provider": "pyannote"},
                "quality": {"alignment_fallback_count": 0},
                "words": [
                    {
                        "word": "はい",
                        "start": 0.0,
                        "end": 1.0,
                        "speaker": "SPEAKER_00",
                    }
                ],
                "segments": [
                    {
                        "start": 0.0,
                        "end": 1.0,
                        "speaker": "SPEAKER_00",
                        "text": "はい",
                    }
                ],
            }

            with patch.object(
                service,
                "_run_whisperjav_worker",
                return_value=worker_result,
            ) as run_worker:
                service._run_job("whisperjav-job")

            run_worker.assert_called_once()
            completed = service.store.get("whisperjav-job")
            payload = json.loads(
                Path(completed.result_path).read_text(encoding="utf-8")
            )
            request_trace = json.loads(
                (
                    settings.artifacts_dir
                    / "whisperjav-job"
                    / "00_request.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(completed.status, "completed")
            self.assertEqual(payload["pipeline"]["provider"], "whisperjav")
            self.assertEqual(payload["segments"][0]["speaker"], "SPEAKER_00")
            self.assertEqual(
                request_trace["option_semantics"]["stt_call_count"], 2
            )
            self.assertEqual(
                request_trace["option_semantics"]["alignment_call_count"],
                1,
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
                STTAPISettings(
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
                    "stt_to_subtitle.stt_api.run_pipeline",
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


class STTAPIRouteTests(unittest.TestCase):
    def test_terminal_job_is_delivered_as_an_sse_status_event(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            settings = STTAPISettings(
                state_dir=root,
                api_token="api-token",
                hf_token="hf-token",
            )
            with TestClient(create_app(settings)) as client:
                service = client.app.state.transcription_service
                service.store.create(
                    job_id="job-1",
                    idempotency_key="key-1",
                    audio_path=root / "audio.wav",
                    audio_sha256="abc",
                    options={},
                )
                service.store.update("job-1", status="completed")

                with client.stream(
                    "GET",
                    "/v1/transcriptions/job-1/events",
                    headers={"Authorization": "Bearer api-token"},
                ) as response:
                    body = "".join(response.iter_text())

            self.assertEqual(response.status_code, 200)
            self.assertIn("event: transcription", body)
            self.assertIn('"status":"completed"', body)
            self.assertEqual(response.headers["x-accel-buffering"], "no")

    def test_health_is_public_and_job_status_requires_bearer_token(self) -> None:
        with TemporaryDirectory() as directory:
            settings = STTAPISettings(
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
            settings = STTAPISettings(
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
            settings = STTAPISettings(
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


class IdleModelReleaseTests(unittest.TestCase):
    """The Kotoba pipeline is the only model that stays resident in-process,
    so it is the one that has to be handed back when nothing is using it."""

    def _service(self, root: Path, **overrides: object) -> TranscriptionService:
        settings = STTAPISettings(
            state_dir=root / "state",
            api_token="",
            hf_token="secret-hf-token",
            device="cpu",
            diarization_device="cpu",
            **overrides,
        )
        return TranscriptionService(settings)

    def test_releases_the_pipeline_once_it_has_been_idle_past_the_limit(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            service = self._service(
                Path(directory), model_idle_timeout_seconds=60.0
            )
            service._pipeline = SimpleNamespace()
            service._pipeline_idle_since = time.monotonic() - 61.0

            service._release_idle_pipeline()

            self.assertIsNone(service._pipeline)

    def test_keeps_the_pipeline_warm_before_the_limit(self) -> None:
        with TemporaryDirectory() as directory:
            service = self._service(
                Path(directory), model_idle_timeout_seconds=60.0
            )
            pipeline = SimpleNamespace()
            service._pipeline = pipeline
            service._pipeline_idle_since = time.monotonic() - 59.0

            service._release_idle_pipeline()

            self.assertIs(service._pipeline, pipeline)

    def test_zero_timeout_releases_as_soon_as_the_queue_drains(self) -> None:
        with TemporaryDirectory() as directory:
            service = self._service(
                Path(directory), model_idle_timeout_seconds=0.0
            )
            service._pipeline = SimpleNamespace()

            service._release_idle_pipeline()

            self.assertIsNone(service._pipeline)

    def test_negative_timeout_disables_the_reaper(self) -> None:
        with TemporaryDirectory() as directory:
            service = self._service(
                Path(directory), model_idle_timeout_seconds=-1.0
            )
            pipeline = SimpleNamespace()
            service._pipeline = pipeline
            service._pipeline_idle_since = time.monotonic() - 100000.0

            service._release_idle_pipeline()

            self.assertIs(service._pipeline, pipeline)

    def test_keeps_the_pipeline_while_work_is_still_queued(self) -> None:
        with TemporaryDirectory() as directory:
            service = self._service(
                Path(directory), model_idle_timeout_seconds=0.0
            )
            pipeline = SimpleNamespace()
            service._pipeline = pipeline
            service._queue.put("pending-job")

            service._release_idle_pipeline()

            self.assertIs(service._pipeline, pipeline)

    def test_never_unloads_underneath_a_running_job(self) -> None:
        with TemporaryDirectory() as directory:
            service = self._service(
                Path(directory), model_idle_timeout_seconds=0.0
            )
            pipeline = SimpleNamespace()
            service._pipeline = pipeline
            service._pipeline_idle_since = time.monotonic() - 100000.0
            held = threading.Event()
            release = threading.Event()

            def hold_the_job_lock() -> None:
                with service._pipeline_lock:
                    held.set()
                    release.wait(5)

            worker = threading.Thread(target=hold_the_job_lock)
            worker.start()
            try:
                self.assertTrue(held.wait(5))
                service._release_idle_pipeline()
                self.assertIs(service._pipeline, pipeline)
            finally:
                release.set()
                worker.join(5)

            service._release_idle_pipeline()
            self.assertIsNone(service._pipeline)

    def test_health_reports_the_idle_budget(self) -> None:
        with TemporaryDirectory() as directory:
            service = self._service(
                Path(directory), model_idle_timeout_seconds=120.0
            )

            self.assertEqual(
                service.health()["model_idle_timeout_seconds"], 120.0
            )
            self.assertIsNone(service.health()["model_idle_seconds"])

            service._pipeline = SimpleNamespace()
            service._pipeline_idle_since = time.monotonic() - 30.0

            self.assertAlmostEqual(
                service.health()["model_idle_seconds"], 30.0, delta=1.0
            )

    def test_reads_the_idle_limit_from_the_environment(self) -> None:
        with patch.dict(
            os.environ,
            {"HF_TOKEN": "x", "STT_MODEL_IDLE_TIMEOUT_SECONDS": "45"},
        ):
            self.assertEqual(
                STTAPISettings.from_env().model_idle_timeout_seconds, 45.0
            )

    def test_defaults_to_a_fifteen_minute_idle_limit(self) -> None:
        with patch.dict(os.environ, {"HF_TOKEN": "x"}, clear=False):
            os.environ.pop("STT_MODEL_IDLE_TIMEOUT_SECONDS", None)
            self.assertEqual(
                STTAPISettings.from_env().model_idle_timeout_seconds, 900.0
            )
