from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import json
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from stt_to_subtitle.service_clients import (
    ExternalServiceError,
    LMStudioClient,
    OpenAICompatibleClient,
    OperationStopped,
    RemoteTranscriptionFailed,
    RequestConcurrencyLimiter,
    RetryingJSONClient,
    STTAPIClient,
    SubtitleValidationClient,
    TranslationPaused,
    TranslationResponseIDError,
    batch_segments,
    list_openai_compatible_models,
    normalize_translation_response,
)
from stt_to_subtitle.translation_prompt import KOREAN_JAV_SYSTEM_PROMPT


class BatchSegmentsTests(unittest.TestCase):
    def test_splits_on_count_without_losing_segments(self) -> None:
        segments = [
            {"id": f"segment-{index}", "text": "日本語"}
            for index in range(5)
        ]

        batches = batch_segments(
            segments,
            max_segments=2,
            max_characters=100,
        )

        self.assertEqual([len(batch) for batch in batches], [2, 2, 1])
        self.assertEqual(
            [item["id"] for batch in batches for item in batch],
            [item["id"] for item in segments],
        )

    def test_single_long_segment_is_kept_intact(self) -> None:
        segments = [{"id": "one", "text": "長" * 20}]

        self.assertEqual(
            batch_segments(
                segments,
                max_segments=30,
                max_characters=10,
            ),
            [segments],
        )


class AuthenticationHeaderTests(unittest.TestCase):
    def test_omits_authorization_header_when_token_is_blank(self) -> None:
        client = RetryingJSONClient(token="")

        self.assertEqual(client.headers, {"Accept": "application/json"})

    def test_observes_each_request_attempt_without_request_secrets(self) -> None:
        observations: list[dict[str, object]] = []
        unavailable = Mock(status_code=503)
        completed = Mock(status_code=200)
        client = RetryingJSONClient(
            token="secret",
            attempts=2,
            service_name="translation_lm",
            request_observer=lambda value: observations.append(dict(value)),
        )

        with patch.object(
            client.session,
            "request",
            side_effect=[unavailable, completed],
        ), patch("stt_to_subtitle.service_clients.time.sleep"):
            response = client.request(
                "POST",
                "http://translation.test/v1/chat/completions",
                headers=client.headers,
                json={"private": "request body"},
                metric_operation="translation",
            )

        self.assertIs(response, completed)
        self.assertEqual(
            [observation["outcome"] for observation in observations],
            ["retry", "success"],
        )
        self.assertEqual(
            [observation["attempt"] for observation in observations],
            [1, 2],
        )
        self.assertTrue(
            all(
                observation["operation"] == "translation"
                and observation["service"] == "translation_lm"
                for observation in observations
            )
        )
        self.assertTrue(
            all(
                "url" not in observation
                and "headers" not in observation
                and "json" not in observation
                and "token" not in observation
                for observation in observations
            )
        )
        unavailable.close.assert_called_once()


class OpenAICompatibleModelTests(unittest.TestCase):
    def test_lists_unique_model_ids_in_stable_order(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "object": "list",
            "data": [
                {"id": "zeta"},
                {"id": "Alpha"},
                {"id": "zeta"},
            ],
        }

        with patch.object(
            RetryingJSONClient,
            "request",
            return_value=response,
        ) as request:
            models = list_openai_compatible_models(
                "http://translation.test/v1/",
                "secret",
            )

        self.assertEqual(models, ["Alpha", "zeta"])
        request.assert_called_once_with(
            "GET",
            "http://translation.test/v1/models",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer secret",
            },
            metric_operation="models",
        )

    def test_rejects_an_invalid_model_list(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"data": [{"id": ""}]}

        with patch.object(
            RetryingJSONClient,
            "request",
            return_value=response,
        ), self.assertRaisesRegex(RuntimeError, "empty id"):
            list_openai_compatible_models(
                "http://translation.test/v1",
                "",
            )

    def test_legacy_client_name_is_a_backward_compatible_alias(self) -> None:
        self.assertIs(LMStudioClient, OpenAICompatibleClient)


class SubtitleValidationClientTests(unittest.TestCase):
    def test_requests_one_structured_validation(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "severity": "review",
                                "summary": "한 문장을 확인하세요.",
                                "findings": [
                                    {
                                        "reference_index": 2,
                                        "category": "meaning",
                                        "message": "의미가 다릅니다.",
                                    }
                                ],
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        client = SubtitleValidationClient(
            "https://validator.test/v1/",
            "secret",
            "paid-model",
        )

        with patch.object(client, "request", return_value=response) as request:
            result = client.validate({"segments": []})

        self.assertEqual(result["severity"], "review")
        self.assertEqual(result["findings"][0]["reference_index"], 2)
        self.assertEqual(request.call_count, 1)
        args, kwargs = request.call_args
        self.assertEqual(args, ("POST", "https://validator.test/v1/chat/completions"))
        self.assertEqual(kwargs["json"]["model"], "paid-model")
        self.assertEqual(
            kwargs["json"]["response_format"]["type"],
            "json_schema",
        )

    def test_rejects_an_invalid_structured_validation(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "severity": "unknown",
                                "summary": "invalid",
                                "findings": [],
                            }
                        )
                    }
                }
            ]
        }
        client = SubtitleValidationClient(
            "https://validator.test/v1",
            "",
            "paid-model",
        )

        with patch.object(client, "request", return_value=response), self.assertRaises(
            ExternalServiceError
        ):
            client.validate({"segments": []})


class STTAPIClientProgressTests(unittest.TestCase):
    @staticmethod
    def event_stream(*payloads: dict[str, object]) -> Mock:
        response = Mock(status_code=200)
        lines: list[str] = []
        for payload in payloads:
            lines.extend(
                [
                    "event: transcription",
                    f"data: {json.dumps(payload)}",
                    "",
                ]
            )
        response.iter_lines.return_value = lines
        return response

    def test_checks_transcription_readiness_once_per_explicit_call(self) -> None:
        response = Mock(status_code=200)
        response.json.return_value = {"status": "ready", "backends": {}}
        client = STTAPIClient("http://stt.test", "token")
        client.request = Mock(return_value=response)

        payload = client.check_readiness()

        self.assertEqual(payload["status"], "ready")
        client.request.assert_called_once_with(
            "GET",
            "http://stt.test/readyz",
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer token",
            },
            metric_operation="readiness",
        )

    def test_stops_event_stream_when_requested(self) -> None:
        running = self.event_stream({"status": "running"})
        cancelled = Mock(status_code=200)
        cancelled.json.return_value = {"status": "cancelled"}
        client = STTAPIClient("http://stt.test", "")
        client.request = Mock(side_effect=[running, cancelled])
        should_stop = Mock(side_effect=[False, False, True])

        with self.assertRaises(OperationStopped):
            client.transcribe(
                Path("/not-read.wav"),
                options={},
                idempotency_key="key",
                existing_job_id="remote-job",
                should_stop=should_stop,
            )

        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(
            client.request.call_args_list[0].args[1],
            "http://stt.test/v1/transcriptions/remote-job/events",
        )
        self.assertEqual(client.request.call_args_list[1].args[0], "POST")
        self.assertEqual(
            client.request.call_args_list[1].args[1],
            "http://stt.test/v1/transcriptions/remote-job/cancel",
        )

    def test_waits_for_remote_cancellation_confirmation(self) -> None:
        running = self.event_stream({"status": "running"})
        requested = Mock(status_code=200)
        requested.json.return_value = {"status": "cancel_requested"}
        confirmation = self.event_stream({"status": "cancelled"})
        client = STTAPIClient("http://stt.test", "")
        client.request = Mock(
            side_effect=[running, requested, confirmation]
        )
        should_stop = Mock(side_effect=[False, False, True])

        with self.assertRaises(OperationStopped):
            client.transcribe(
                Path("/not-read.wav"),
                options={},
                idempotency_key="key",
                existing_job_id="remote-job",
                should_stop=should_stop,
            )

        self.assertEqual(client.request.call_count, 3)
        self.assertEqual(client.request.call_args_list[1].args[0], "POST")
        self.assertEqual(client.request.call_args_list[2].args[0], "GET")

    def test_resubmits_when_persisted_remote_job_is_missing(self) -> None:
        missing = Mock(status_code=404)
        missing.json.return_value = {"detail": "job not found"}
        completed = self.event_stream({"status": "completed"})
        result = Mock(status_code=200)
        result.json.return_value = {
            "schema_version": 1,
            "job_id": "replacement-job",
            "segments": [],
        }
        client = STTAPIClient("http://stt.test", "")
        client.request = Mock(side_effect=[missing, completed, result])
        client._submit = Mock(return_value="replacement-job")

        payload = client.transcribe(
            Path("/not-read.wav"),
            options={},
            idempotency_key="key",
            existing_job_id="missing-job",
        )

        self.assertEqual(payload["job_id"], "replacement-job")
        client._submit.assert_called_once_with(
            Path("/not-read.wav"),
            options={},
            idempotency_key="key",
        )

    def test_exposes_structured_remote_job_failure(self) -> None:
        failed = self.event_stream(
            {
                "status": "failed",
                "failure_code": "model_output_invalid",
                "retryable": False,
                "failure_scope": "job",
                "error": "segments must be a list",
            }
        )
        client = STTAPIClient("http://stt.test", "")
        client.request = Mock(return_value=failed)
        client._submit = Mock(return_value="new-job")

        with self.assertRaises(RemoteTranscriptionFailed) as caught:
            client.transcribe(
                Path("/not-read.wav"),
                options={},
                idempotency_key="key",
            )

        self.assertEqual(caught.exception.failure_code, "model_output_invalid")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.failure_scope, "job")
        self.assertIn("segments must be a list", str(caught.exception))

    def test_does_not_resubmit_non_retryable_existing_failure(self) -> None:
        failed = self.event_stream(
            {
                "status": "failed",
                "failure_code": "model_output_invalid",
                "retryable": False,
                "failure_scope": "job",
                "error": "segments must be a list",
            }
        )
        client = STTAPIClient("http://stt.test", "")
        client.request = Mock(return_value=failed)
        client._submit = Mock(return_value="replacement-job")

        with self.assertRaises(RemoteTranscriptionFailed):
            client.transcribe(
                Path("/not-read.wav"),
                options={},
                idempotency_key="key",
                existing_job_id="failed-job",
            )

        client._submit.assert_not_called()

    def test_resubmits_retryable_existing_service_failure(self) -> None:
        failed = self.event_stream(
            {
                "status": "failed",
                "failure_code": "service_restarted",
                "retryable": True,
                "failure_scope": "service",
                "error": "service restarted",
            }
        )
        completed = self.event_stream({"status": "completed"})
        result = Mock(status_code=200)
        result.json.return_value = {
            "schema_version": 1,
            "job_id": "replacement-job",
            "segments": [],
        }
        client = STTAPIClient("http://stt.test", "")
        client.request = Mock(side_effect=[failed, completed, result])
        client._submit = Mock(return_value="replacement-job")

        payload = client.transcribe(
            Path("/not-read.wav"),
            options={},
            idempotency_key="key",
            existing_job_id="failed-job",
        )

        self.assertEqual(payload["job_id"], "replacement-job")
        client._submit.assert_called_once()

    def test_classifies_remote_authentication_failure(self) -> None:
        unauthorized = Mock(status_code=401)
        unauthorized.json.return_value = {"detail": "invalid bearer token"}
        unauthorized.close = Mock()
        client = STTAPIClient("http://stt.test", "wrong-token")
        client.request = Mock(return_value=unauthorized)
        client._submit = Mock(return_value="remote-job")

        with self.assertRaises(RemoteTranscriptionFailed) as caught:
            client.transcribe(
                Path("/not-read.wav"),
                options={},
                idempotency_key="key",
            )

        self.assertEqual(caught.exception.failure_code, "auth_required")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.failure_scope, "configuration")

    def test_classifies_rejected_transcription_input(self) -> None:
        rejected = Mock(status_code=400)
        rejected.json.return_value = {"detail": "invalid WAV header"}
        client = STTAPIClient("http://stt.test", "")
        with TemporaryDirectory() as directory:
            audio_path = Path(directory) / "audio.wav"
            audio_path.write_bytes(b"invalid")
            with patch.object(
                client.session,
                "request",
                return_value=rejected,
            ):
                with self.assertRaises(RemoteTranscriptionFailed) as caught:
                    client._submit(
                        audio_path,
                        options={},
                        idempotency_key="key",
                    )

        self.assertEqual(caught.exception.failure_code, "invalid_input")
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(caught.exception.failure_scope, "job")

    def test_forwards_changed_chunk_progress_from_event_stream(self) -> None:
        events = self.event_stream(
            {
                "status": "running",
                "chunk_progress": {
                    "created": 20,
                    "completed": 10,
                    "in_progress": 10,
                },
            },
            {
                "status": "completed",
                "chunk_progress": {
                    "created": 23,
                    "completed": 23,
                    "in_progress": 0,
                },
            },
        )
        result = Mock(status_code=200)
        result.json.return_value = {
            "schema_version": 1,
            "job_id": "remote-job",
            "segments": [],
        }
        client = STTAPIClient("http://stt.test", "")
        client.request = Mock(side_effect=[events, result])
        progress = []

        with patch("stt_to_subtitle.service_clients.time.sleep") as sleep:
            client.transcribe(
                Path("/not-read.wav"),
                options={},
                idempotency_key="key",
                existing_job_id="remote-job",
                on_progress=progress.append,
            )

        self.assertEqual(
            progress,
            [
                {
                    "created": 20,
                    "completed": 10,
                    "in_progress": 10,
                    "report_every": 10,
                    "final": False,
                },
                {
                    "created": 23,
                    "completed": 23,
                    "in_progress": 0,
                    "report_every": 10,
                    "final": True,
                },
            ],
        )
        self.assertEqual(client.request.call_count, 2)
        self.assertTrue(client.request.call_args_list[0].kwargs["stream"])
        sleep.assert_not_called()


class TranslationResponseTests(unittest.TestCase):
    def test_adds_five_previous_and_three_following_context_segments(
        self,
    ) -> None:
        client = OpenAICompatibleClient(
            "http://translation.test/v1",
            "",
            "model",
            max_segments=2,
        )
        calls: list[tuple[list[str], list[str]]] = []

        def translate_batch(batch, context, _prompt):
            calls.append(
                (
                    [str(item["id"]) for item in batch],
                    [str(item["id"]) for item in context],
                )
            )
            return [
                {"id": str(item["id"]), "text": f"번역-{item['id']}"}
                for item in batch
            ]

        client._translate_batch_with_recovery = Mock(
            side_effect=translate_batch
        )
        segments = [
            {"id": f"segment-{index}", "text": str(index)}
            for index in range(1, 9)
        ]

        client.translate(segments)

        self.assertEqual(calls[0][0], ["segment-1", "segment-2"])
        self.assertEqual(
            calls[0][1],
            ["segment-3", "segment-4", "segment-5"],
        )
        self.assertEqual(calls[2][0], ["segment-5", "segment-6"])
        self.assertEqual(
            calls[2][1],
            [
                "segment-1",
                "segment-2",
                "segment-3",
                "segment-4",
                "segment-7",
                "segment-8",
            ],
        )

    def test_reviews_twice_and_stops_when_the_result_is_unchanged(self) -> None:
        client = OpenAICompatibleClient(
            "http://translation.test/v1",
            "",
            "model",
        )
        draft = [{"id": "segment-1", "text": "초벌"}]
        reviewed = [{"id": "segment-1", "text": "교정"}]
        client._translate_batch_with_recovery = Mock(return_value=draft)
        client._review_batch_with_recovery = Mock(
            side_effect=[reviewed, reviewed]
        )

        result = client.translate(
            [{"id": "segment-1", "text": "原文"}],
            review_prompt="review",
            review_rounds=2,
        )

        self.assertEqual(result, reviewed)
        self.assertEqual(client._review_batch_with_recovery.call_count, 2)

    def test_uses_the_initial_translation_when_review_fails(self) -> None:
        client = OpenAICompatibleClient(
            "http://translation.test/v1",
            "",
            "model",
        )
        draft = [{"id": "segment-1", "text": "초벌"}]
        warnings: list[str] = []
        client._translate_batch_with_recovery = Mock(return_value=draft)
        client._review_batch_with_recovery = Mock(
            side_effect=ExternalServiceError("review unavailable")
        )

        result = client.translate(
            [{"id": "segment-1", "text": "原文"}],
            review_prompt="review",
            review_rounds=2,
            on_review_warning=warnings.append,
        )

        self.assertEqual(result, draft)
        self.assertEqual(warnings, ["review unavailable"])

    def test_reports_logical_batch_progress_and_pauses_after_checkpoint(
        self,
    ) -> None:
        client = OpenAICompatibleClient(
            "http://translation.test/v1",
            "",
            "model",
            max_segments=1,
        )
        client._translate_batch_with_recovery = Mock(
            side_effect=lambda batch, *_args: [
                {"id": str(batch[0]["id"]), "text": "번역"}
            ]
        )
        progress: list[tuple[int, int]] = []
        checkpoints: list[list[dict[str, str]]] = []
        started: list[tuple[int, list[str]]] = []
        completed: list[tuple[int, list[dict[str, str]]]] = []

        with self.assertRaises(TranslationPaused):
            client.translate(
                [
                    {"id": "segment-1", "text": "一"},
                    {"id": "segment-2", "text": "二"},
                ],
                on_batch=checkpoints.append,
                on_batch_started=lambda index, ids: started.append((index, ids)),
                on_logical_batch=lambda index, items: completed.append(
                    (index, items)
                ),
                on_progress=lambda completed, total: progress.append(
                    (completed, total)
                ),
                should_pause=lambda: True,
            )

        self.assertEqual(progress, [(0, 2), (1, 2)])
        self.assertEqual(
            checkpoints,
            [[{"id": "segment-1", "text": "번역"}]],
        )
        self.assertEqual(client._translate_batch_with_recovery.call_count, 1)
        self.assertEqual(started, [(0, ["segment-1"])])
        self.assertEqual(
            completed,
            [(0, [{"id": "segment-1", "text": "번역"}])],
        )

    def test_reports_a_failed_logical_batch(self) -> None:
        client = OpenAICompatibleClient(
            "http://translation.test/v1",
            "",
            "model",
        )
        client._translate_batch_with_recovery = Mock(
            side_effect=ExternalServiceError("server offline")
        )
        failures: list[tuple[int, list[str], str]] = []

        with self.assertRaises(ExternalServiceError):
            client.translate(
                [{"id": "segment-1", "text": "一"}],
                on_batch_failed=lambda index, ids, error: failures.append(
                    (index, ids, error)
                ),
            )

        self.assertEqual(
            failures,
            [(0, ["segment-1"], "server offline")],
        )

    def test_translates_one_file_batches_in_parallel_and_reorders_results(
        self,
    ) -> None:
        client = OpenAICompatibleClient(
            "http://translation.test/v1",
            "",
            "model",
            max_segments=1,
        )
        all_started = threading.Event()
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0
        started = 0

        def translate_batch(batch, *_args):
            nonlocal active, maximum_active, started
            with state_lock:
                active += 1
                started += 1
                maximum_active = max(maximum_active, active)
                if started == 3:
                    all_started.set()
            if not all_started.wait(timeout=2):
                raise AssertionError("three translation batches did not overlap")
            try:
                return [
                    {
                        "id": str(batch[0]["id"]),
                        "text": f"번역-{batch[0]['id']}",
                    }
                ]
            finally:
                with state_lock:
                    active -= 1

        client._translate_batch_with_recovery = Mock(
            side_effect=translate_batch
        )
        progress: list[tuple[int, int]] = []
        checkpoints: list[list[dict[str, str]]] = []
        batch_starts: list[tuple[int, list[str]]] = []
        batch_completions: list[tuple[int, list[str]]] = []
        segments = [
            {"id": f"segment-{index}", "text": str(index)}
            for index in range(1, 4)
        ]

        result = client.translate(
            segments,
            max_workers=3,
            on_batch=checkpoints.append,
            on_batch_started=lambda index, ids: batch_starts.append(
                (index, ids)
            ),
            on_logical_batch=lambda index, items: batch_completions.append(
                (index, [item["id"] for item in items])
            ),
            on_progress=lambda completed, total: progress.append(
                (completed, total)
            ),
        )

        self.assertEqual(maximum_active, 3)
        self.assertEqual(
            result,
            [
                {"id": f"segment-{index}", "text": f"번역-segment-{index}"}
                for index in range(1, 4)
            ],
        )
        self.assertEqual(progress[0], (0, 3))
        self.assertEqual(progress[-1], (3, 3))
        self.assertEqual(checkpoints[-1], result)
        self.assertEqual(
            batch_starts,
            [
                (0, ["segment-1"]),
                (1, ["segment-2"]),
                (2, ["segment-3"]),
            ],
        )
        self.assertCountEqual(
            batch_completions,
            [
                (0, ["segment-1"]),
                (1, ["segment-2"]),
                (2, ["segment-3"]),
            ],
        )

    def test_parallel_translation_pauses_after_active_batches_checkpoint(
        self,
    ) -> None:
        client = OpenAICompatibleClient(
            "http://translation.test/v1",
            "",
            "model",
            max_segments=1,
        )
        active_batches = threading.Barrier(2, timeout=2)

        def translate_batch(batch, *_args):
            active_batches.wait()
            return [
                {"id": str(batch[0]["id"]), "text": "번역"}
            ]

        client._translate_batch_with_recovery = Mock(
            side_effect=translate_batch
        )
        checkpoints: list[list[dict[str, str]]] = []
        progress: list[tuple[int, int]] = []

        with self.assertRaises(TranslationPaused):
            client.translate(
                [
                    {"id": "segment-1", "text": "一"},
                    {"id": "segment-2", "text": "二"},
                    {"id": "segment-3", "text": "三"},
                ],
                max_workers=2,
                on_batch=checkpoints.append,
                on_progress=lambda completed, total: progress.append(
                    (completed, total)
                ),
                should_pause=lambda: True,
            )

        self.assertEqual(client._translate_batch_with_recovery.call_count, 2)
        self.assertEqual(progress[0], (0, 3))
        self.assertEqual(progress[-1], (2, 3))
        self.assertEqual(len(checkpoints[-1]), 2)

    def test_shares_request_limit_across_parallel_file_translations(
        self,
    ) -> None:
        limiter = RequestConcurrencyLimiter(2)
        clients = [
            OpenAICompatibleClient(
                "http://translation.test/v1",
                "",
                "model",
                max_segments=1,
                attempts=1,
                request_limiter=limiter,
            )
            for _index in range(2)
        ]
        all_attempted = threading.Event()
        release_requests = threading.Event()
        state_lock = threading.Lock()
        active = 0
        maximum_active = 0
        attempted = 0
        original_slot = limiter.slot

        @contextmanager
        def tracked_slot():
            nonlocal attempted
            with state_lock:
                attempted += 1
                if attempted == 4:
                    all_attempted.set()
            with original_slot():
                yield

        limiter.slot = tracked_slot

        def request(_method, _url, **kwargs):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            if not release_requests.wait(timeout=2):
                raise AssertionError("translation requests were not released")
            try:
                user_payload = json.loads(
                    kwargs["json"]["messages"][1]["content"]
                )
                translations = [
                    {"id": item["id"], "text": f"번역-{item['id']}"}
                    for item in user_payload["target_segments"]
                ]
                response = Mock(status_code=200)
                response.json.return_value = {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {"translations": translations},
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ]
                }
                return response
            finally:
                with state_lock:
                    active -= 1

        file_segments = [
            [
                {"id": f"file-{file_index}-segment-{segment_index}", "text": "원문"}
                for segment_index in range(2)
            ]
            for file_index in range(2)
        ]

        with patch(
            "stt_to_subtitle.service_clients.requests.Session.request",
            side_effect=request,
        ), ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    client.translate,
                    segments,
                    max_workers=2,
                )
                for client, segments in zip(clients, file_segments)
            ]
            self.assertTrue(all_attempted.wait(timeout=2))
            with state_lock:
                self.assertEqual(active, 2)
            release_requests.set()
            results = [future.result() for future in futures]

        self.assertEqual(maximum_active, 2)
        self.assertEqual([len(result) for result in results], [2, 2])

    def test_reorders_an_exact_translation_id_set(self) -> None:
        normalized = normalize_translation_response(
            [
                {"id": "segment-2", "text": "둘"},
                {"id": "segment-1", "text": "하나"},
            ],
            ["segment-1", "segment-2"],
        )

        self.assertEqual(
            normalized,
            [
                {"id": "segment-1", "text": "하나"},
                {"id": "segment-2", "text": "둘"},
            ],
        )

    def test_reports_missing_and_unexpected_translation_ids(self) -> None:
        with self.assertRaisesRegex(
            TranslationResponseIDError,
            "missing=.*unexpected=",
        ):
            normalize_translation_response(
                [{"id": "wrong", "text": "번역"}],
                ["segment-1", "segment-2"],
            )

    def test_ignores_stale_checkpoint_ids_on_retry(self) -> None:
        client = LMStudioClient("http://lm.test/v1", "", "model")
        client._translate_batch_with_recovery = Mock(
            return_value=[{"id": "segment-2", "text": "둘"}]
        )

        result = client.translate(
            [
                {"id": "segment-1", "text": "一"},
                {"id": "segment-2", "text": "二"},
            ],
            existing={
                "old-segment": "오래된 값",
                "segment-1": "하나",
            },
        )

        self.assertEqual(
            result,
            [
                {"id": "segment-1", "text": "하나"},
                {"id": "segment-2", "text": "둘"},
            ],
        )

    def test_splits_a_mismatched_batch_for_recovery(self) -> None:
        client = LMStudioClient("http://lm.test/v1", "", "model")

        def translate_batch(segments, *_args):
            if len(segments) > 1:
                raise TranslationResponseIDError("mismatch")
            return [
                {
                    "id": str(segments[0]["id"]),
                    "text": f"번역-{segments[0]['id']}",
                }
            ]

        client._translate_batch = Mock(side_effect=translate_batch)
        result = client._translate_batch_with_recovery(
            [
                {"id": "segment-1", "text": "一"},
                {"id": "segment-2", "text": "二"},
                {"id": "segment-3", "text": "三"},
            ]
        )

        self.assertEqual(
            [item["id"] for item in result],
            ["segment-1", "segment-2", "segment-3"],
        )

    def test_sends_the_static_korean_jav_system_prompt(self) -> None:
        client = OpenAICompatibleClient(
            "http://translation.test/v1",
            "",
            "model",
        )
        response = Mock(status_code=200)
        response.json.return_value = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "translations": [
                                    {"id": "segment-1", "text": "번역"}
                                ]
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }
        client.request = Mock(return_value=response)

        result = client._translate_batch(
            [{"id": "segment-1", "text": "翻訳"}]
        )

        self.assertEqual(result, [{"id": "segment-1", "text": "번역"}])
        request_payload = client.request.call_args.kwargs["json"]
        self.assertEqual(
            request_payload["messages"][0],
            {
                "role": "system",
                "content": KOREAN_JAV_SYSTEM_PROMPT,
            },
        )
        self.assertIn("Japanese spoken subtitle segments", KOREAN_JAV_SYSTEM_PROMPT)
        self.assertIn('"translations"', KOREAN_JAV_SYSTEM_PROMPT)
        self.assertIn("Preserve every target id exactly", KOREAN_JAV_SYSTEM_PROMPT)
        self.assertIn("生ハメ→노콘", KOREAN_JAV_SYSTEM_PROMPT)
        for metadata_marker in (
            "<<<actress",
            "<<<title",
            "<<<description",
            "<<<maker",
            "<<<label",
            "<<<director",
            "<<<JZ_DONE>>>",
        ):
            self.assertNotIn(metadata_marker, KOREAN_JAV_SYSTEM_PROMPT)
