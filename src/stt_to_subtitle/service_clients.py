"""HTTP clients for the independent STT and translation services."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, ContextManager
from urllib.parse import quote

import requests

from .contracts import validate_transcript, validate_translation_items
from .transcription_progress import parse_stage_progress
from .translation_prompt import KOREAN_JAV_DRAFT_PROMPT

LOGGER = logging.getLogger(__name__)
TRANSCRIPTION_FAILURE_SCOPES = frozenset(
    {"job", "backend", "service", "configuration"}
)
RequestObserver = Callable[[Mapping[str, Any]], None]


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_failure_scope(value: Any) -> str | None:
    return value if value in TRANSCRIPTION_FAILURE_SCOPES else None


class ExternalServiceError(RuntimeError):
    """An external stage cannot currently make progress."""


class RemoteTranscriptionNotFound(ExternalServiceError):
    """The persisted remote transcription ID no longer exists."""


class RemoteTranscriptionFailed(RuntimeError):
    """The remote worker completed with a non-recoverable job failure."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: str | None,
        retryable: bool | None = None,
        failure_scope: str | None = None,
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
        self.retryable = retryable
        self.failure_scope = failure_scope


class TranslationResponseIDError(ExternalServiceError):
    """The translation server returned a different segment ID set."""


class TranslationResponseFormatError(ExternalServiceError):
    """The translation server returned a truncated or malformed payload."""


class TranslationPaused(RuntimeError):
    """Translation stopped cleanly after a persisted logical batch."""


class TranslationDeferred(RuntimeError):
    """Translation yielded because a shared accelerator is reserved for STT."""


class OperationStopped(RuntimeError):
    """A local pipeline stage reached a safe user-requested stop point."""


class RequestConcurrencyLimiter:
    """Share an adjustable concurrent-request limit across service clients."""

    def __init__(self, limit: int) -> None:
        self._condition = threading.Condition()
        self._active = 0
        self._limit = 1
        self.set_limit(limit)

    def set_limit(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("request concurrency limit must be at least 1")
        with self._condition:
            self._limit = limit
            self._condition.notify_all()

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._condition:
            while self._active >= self._limit:
                self._condition.wait()
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()


def _safe_error(response: requests.Response) -> str:
    try:
        body = response.json()
    except (ValueError, requests.JSONDecodeError):
        body = response.text.strip()
    return str(body)[:1000] or f"HTTP {response.status_code}"


class RetryingJSONClient:
    def __init__(
        self,
        *,
        token: str,
        connect_timeout: float = 10.0,
        read_timeout: float = 120.0,
        attempts: int = 3,
        request_limiter: RequestConcurrencyLimiter | None = None,
        service_name: str = "external",
        request_observer: RequestObserver | None = None,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        self.token = token
        self.timeout = (connect_timeout, read_timeout)
        self.attempts = attempts
        self.request_limiter = request_limiter
        self.service_name = service_name.strip() or "external"
        self.request_observer = request_observer
        self._session_local = threading.local()

    @property
    def session(self) -> requests.Session:
        session = getattr(self._session_local, "session", None)
        if session is None:
            session = requests.Session()
            self._session_local.session = session
        return session

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        transient_statuses = {408, 429, 500, 502, 503, 504}
        metric_operation = str(
            kwargs.pop("metric_operation", method.lower())
        ).strip() or method.lower()
        last_error: BaseException | None = None
        for attempt in range(1, self.attempts + 1):
            started = time.monotonic()
            try:
                request_slot: ContextManager[None] = (
                    self.request_limiter.slot()
                    if self.request_limiter is not None
                    else nullcontext()
                )
                with request_slot:
                    response = self.session.request(
                        method,
                        url,
                        timeout=self.timeout,
                        **kwargs,
                    )
                retrying = (
                    response.status_code in transient_statuses
                    and attempt < self.attempts
                )
                if 200 <= response.status_code < 400:
                    outcome = "success"
                elif retrying:
                    outcome = "retry"
                elif response.status_code in transient_statuses:
                    outcome = "exhausted"
                else:
                    outcome = "http_error"
                self._observe_request(
                    operation=metric_operation,
                    outcome=outcome,
                    attempt=attempt,
                    elapsed_seconds=time.monotonic() - started,
                    status_code=response.status_code,
                )
                if not retrying:
                    return response
                response.close()
            except requests.RequestException as error:
                last_error = error
                self._observe_request(
                    operation=metric_operation,
                    outcome=(
                        "retry" if attempt < self.attempts else "exhausted"
                    ),
                    attempt=attempt,
                    elapsed_seconds=time.monotonic() - started,
                    error_type=error.__class__.__name__,
                )
                if attempt == self.attempts:
                    break
            delay = float(2 ** (attempt - 1))
            LOGGER.warning(
                "external request failed; retrying in %.0fs (%d/%d)",
                delay,
                attempt,
                self.attempts,
            )
            time.sleep(delay)
        raise ExternalServiceError(
            f"external service is unavailable after {self.attempts} attempts: "
            f"{last_error}"
        ) from last_error

    def _observe_request(
        self,
        *,
        operation: str,
        outcome: str,
        attempt: int,
        elapsed_seconds: float,
        status_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        if self.request_observer is None:
            return
        observation: dict[str, Any] = {
            "service": self.service_name,
            "operation": operation,
            "outcome": outcome,
            "attempt": attempt,
            "elapsed_seconds": max(0.0, elapsed_seconds),
        }
        if status_code is not None:
            observation["status_code"] = status_code
        if error_type is not None:
            observation["error_type"] = error_type
        try:
            self.request_observer(observation)
        except Exception:
            LOGGER.exception("external request observer failed")


class STTAPIClient(RetryingJSONClient):
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        attempts: int = 3,
        connect_timeout: float = 10.0,
        read_timeout: float = 300.0,
        request_observer: RequestObserver | None = None,
    ) -> None:
        super().__init__(
            token=token,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            attempts=attempts,
            service_name="stt",
            request_observer=request_observer,
        )
        self.base_url = base_url.rstrip("/")

    def check_readiness(self) -> Mapping[str, Any]:
        response = self.request(
            "GET",
            f"{self.base_url}/readyz",
            headers=self.headers,
            metric_operation="readiness",
        )
        if response.status_code != 200:
            raise ExternalServiceError(
                "transcription server is not ready: "
                f"HTTP {response.status_code}: {_safe_error(response)}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ExternalServiceError(
                "transcription readiness response is invalid"
            ) from error
        if not isinstance(payload, Mapping) or payload.get("status") != "ready":
            raise ExternalServiceError(
                "transcription readiness response is invalid"
            )
        return payload

    def transcribe(
        self,
        audio_path: Path,
        *,
        options: Mapping[str, Any],
        idempotency_key: str,
        existing_job_id: str | None = None,
        on_job_created: Callable[[str], None] | None = None,
        on_progress: Callable[[Mapping[str, Any]], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        job_id = existing_job_id
        may_requeue_existing = existing_job_id is not None
        while True:
            stop_initiated = False
            try:
                if should_stop is not None and should_stop():
                    raise OperationStopped("transcription stop requested")
                if job_id is None:
                    job_id = self._submit(
                        audio_path,
                        options=options,
                        idempotency_key=idempotency_key,
                    )
                    if on_job_created is not None:
                        on_job_created(job_id)

                status_payload = self._wait_for_terminal_status(
                    job_id,
                    on_progress=on_progress,
                    should_stop=should_stop,
                )
            except RemoteTranscriptionNotFound:
                if not may_requeue_existing:
                    raise
                may_requeue_existing = False
                job_id = None
                continue
            except OperationStopped:
                if job_id is None:
                    raise
                stop_initiated = True
                status_payload = self.cancel_job_and_wait(job_id)
            remote_status = str(status_payload["status"])
            if remote_status == "completed":
                break
            if remote_status == "cancelled":
                if may_requeue_existing and not stop_initiated:
                    may_requeue_existing = False
                    job_id = None
                    continue
                raise OperationStopped("remote transcription was cancelled")
            if remote_status == "failed":
                failure_code = (
                    str(status_payload["failure_code"])
                    if status_payload.get("failure_code") is not None
                    else None
                )
                retryable = _optional_bool(status_payload.get("retryable"))
                if (
                    may_requeue_existing
                    and (
                        retryable is True
                        or (
                            retryable is None
                            and failure_code == "service_restarted"
                        )
                    )
                ):
                    may_requeue_existing = False
                    job_id = None
                    continue
                raise RemoteTranscriptionFailed(
                    "transcription job failed: "
                    f"{status_payload.get('error', 'unknown remote error')}",
                    failure_code=failure_code,
                    retryable=retryable,
                    failure_scope=_optional_failure_scope(
                        status_payload.get("failure_scope")
                    ),
                )

        response = self.request(
            "GET",
            f"{self.base_url}/v1/transcriptions/{job_id}/result",
            headers=self.headers,
            metric_operation="result",
        )
        if response.status_code != 200:
            if response.status_code in {401, 403}:
                raise RemoteTranscriptionFailed(
                    "transcription API authentication failed",
                    failure_code="auth_required",
                    retryable=False,
                    failure_scope="configuration",
                )
            raise ExternalServiceError(
                "transcription result request failed: "
                f"HTTP {response.status_code}: {_safe_error(response)}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ExternalServiceError(
                "transcription API returned invalid result JSON"
            ) from error
        try:
            validate_transcript(payload)
        except ValueError as error:
            raise RemoteTranscriptionFailed(
                "transcription API returned an invalid transcript",
                failure_code="model_output_invalid",
                retryable=False,
                failure_scope="job",
            ) from error
        return payload

    def cancel_job(self, job_id: str) -> Mapping[str, Any]:
        response = self.request(
            "POST",
            f"{self.base_url}/v1/transcriptions/{job_id}/cancel",
            headers=self.headers,
            metric_operation="cancel",
        )
        if response.status_code != 200:
            raise ExternalServiceError(
                "transcription cancellation failed: "
                f"HTTP {response.status_code}: {_safe_error(response)}"
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ExternalServiceError(
                "transcription API returned invalid cancellation JSON"
            ) from error
        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("status"), str
        ):
            raise ExternalServiceError(
                "transcription API returned an invalid cancellation response"
            )
        return payload

    def cancel_job_and_wait(self, job_id: str) -> Mapping[str, Any]:
        payload = self.cancel_job(job_id)
        remote_status = str(payload["status"])
        if remote_status in {"cancelled", "completed", "failed"}:
            return payload
        if remote_status not in {"cancel_requested", "queued", "running"}:
            raise ExternalServiceError(
                "transcription cancellation returned unknown status "
                f"{remote_status}"
            )
        return self._wait_for_terminal_status(
            job_id,
            on_progress=None,
            should_stop=None,
        )

    def _wait_for_terminal_status(
        self,
        job_id: str,
        *,
        on_progress: Callable[[Mapping[str, Any]], None] | None,
        should_stop: Callable[[], bool] | None,
    ) -> Mapping[str, Any]:
        last_progress: tuple[int, int, int, int, bool] | None = None
        last_stage_progress: tuple[str, int, int] | None = None
        for status_payload in self._status_events(
            job_id,
            should_stop=should_stop,
        ):
            try:
                remote_status = str(status_payload["status"])
            except (KeyError, TypeError, ValueError) as error:
                raise ExternalServiceError(
                    "transcription API returned an invalid status event"
                ) from error
            if remote_status not in {
                "cancel_requested",
                "cancelled",
                "queued",
                "running",
                "completed",
                "failed",
            }:
                raise ExternalServiceError(
                    f"transcription job returned unknown status {remote_status}"
                )
            progress_update: dict[str, Any] = {}
            progress = status_payload.get("chunk_progress")
            if isinstance(progress, Mapping):
                try:
                    current_progress = (
                        int(progress["created"]),
                        int(progress["completed"]),
                        int(progress["in_progress"]),
                        int(progress.get("report_every", 10)),
                        remote_status == "completed",
                    )
                except (KeyError, TypeError, ValueError):
                    current_progress = None
                if current_progress is not None and (
                    current_progress[0] < 0
                    or current_progress[1] < 0
                    or current_progress[1] > current_progress[0]
                    or current_progress[2]
                    != current_progress[0] - current_progress[1]
                    or current_progress[3] not in {10, 100}
                ):
                    current_progress = None
                if (
                    current_progress is not None
                    and current_progress != last_progress
                    and any(current_progress)
                ):
                    last_progress = current_progress
                    progress_update.update(
                        {
                            "created": current_progress[0],
                            "completed": current_progress[1],
                            "in_progress": current_progress[2],
                            "report_every": current_progress[3],
                            "final": current_progress[4],
                        }
                    )
            stage_payload = status_payload.get("stage_progress")
            if isinstance(stage_payload, Mapping):
                try:
                    current_stage_progress = parse_stage_progress(stage_payload)
                except ValueError:
                    current_stage_progress = None
                if (
                    current_stage_progress is not None
                    and current_stage_progress != last_stage_progress
                ):
                    last_stage_progress = current_stage_progress
                    progress_update.update(
                        {
                            "stage": current_stage_progress[0],
                            "stage_index": current_stage_progress[1],
                            "stage_total": current_stage_progress[2],
                        }
                    )
            if on_progress is not None and progress_update:
                on_progress(progress_update)
            if remote_status in {"cancelled", "completed", "failed"}:
                return status_payload
        raise ExternalServiceError(
            "transcription event stream ended before a terminal status"
        )

    def _status_events(
        self,
        job_id: str,
        *,
        should_stop: Callable[[], bool] | None,
    ) -> Iterator[Mapping[str, Any]]:
        last_error: BaseException | None = None
        for attempt in range(1, self.attempts + 1):
            if should_stop is not None and should_stop():
                raise OperationStopped("transcription stop requested")
            response = self.request(
                "GET",
                f"{self.base_url}/v1/transcriptions/{job_id}/events",
                headers={**self.headers, "Accept": "text/event-stream"},
                stream=True,
                metric_operation="status_stream",
            )
            if response.status_code != 200:
                try:
                    detail = _safe_error(response)
                finally:
                    response.close()
                if response.status_code == 404:
                    raise RemoteTranscriptionNotFound(
                        "remote transcription job was not found"
                    )
                if response.status_code in {401, 403}:
                    raise RemoteTranscriptionFailed(
                        "transcription API authentication failed",
                        failure_code="auth_required",
                        retryable=False,
                        failure_scope="configuration",
                    )
                raise ExternalServiceError(
                    "transcription status request failed: "
                    f"HTTP {response.status_code}: {detail}"
                )
            data_lines: list[str] = []
            stream_started = time.monotonic()
            stream_error: BaseException | None = None
            try:
                for raw_line in response.iter_lines(
                    chunk_size=1,
                    decode_unicode=True,
                ):
                    if should_stop is not None and should_stop():
                        raise OperationStopped("transcription stop requested")
                    line = (
                        raw_line.decode("utf-8", errors="replace")
                        if isinstance(raw_line, bytes)
                        else str(raw_line)
                    )
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                        continue
                    if line or not data_lines:
                        continue
                    try:
                        payload = json.loads("\n".join(data_lines))
                    except (TypeError, ValueError, json.JSONDecodeError) as error:
                        raise ExternalServiceError(
                            "transcription API returned an invalid status event"
                        ) from error
                    data_lines.clear()
                    if not isinstance(payload, Mapping):
                        raise ExternalServiceError(
                            "transcription status event must be an object"
                        )
                    yield payload
                    if str(payload.get("status", "")) in {
                        "cancelled",
                        "completed",
                        "failed",
                    }:
                        return
            except requests.RequestException as error:
                last_error = error
                stream_error = error
            finally:
                response.close()
            self._observe_request(
                operation="status_stream_disconnect",
                outcome=(
                    "retry" if attempt < self.attempts else "exhausted"
                ),
                attempt=attempt,
                elapsed_seconds=time.monotonic() - stream_started,
                error_type=(
                    stream_error.__class__.__name__
                    if stream_error is not None
                    else "StreamEnded"
                ),
            )
            if attempt < self.attempts:
                delay = float(2 ** (attempt - 1))
                LOGGER.warning(
                    "transcription event stream disconnected; reconnecting "
                    "in %.0fs (%d/%d)",
                    delay,
                    attempt,
                    self.attempts,
                )
                time.sleep(delay)
        raise ExternalServiceError(
            "transcription event stream disconnected before completion"
        ) from last_error

    def _submit(
        self,
        audio_path: Path,
        *,
        options: Mapping[str, Any],
        idempotency_key: str,
    ) -> str:
        try:
            from requests_toolbelt.multipart.encoder import MultipartEncoder
        except ImportError as error:
            raise RuntimeError(
                "requests-toolbelt is required for streaming WAV uploads"
            ) from error

        response: requests.Response | None = None
        last_error: requests.RequestException | None = None
        transient_statuses = {408, 429, 500, 502, 503, 504}
        for attempt in range(1, self.attempts + 1):
            response = None
            started = time.monotonic()
            try:
                with audio_path.open("rb") as audio_stream:
                    multipart = MultipartEncoder(
                        fields={
                            "audio": (
                                audio_path.name,
                                audio_stream,
                                "audio/wav",
                            ),
                            "options": json.dumps(dict(options), sort_keys=True),
                        }
                    )
                    headers = {
                        **self.headers,
                        "Content-Type": multipart.content_type,
                        "Idempotency-Key": idempotency_key,
                    }
                    response = self.session.request(
                        "POST",
                        f"{self.base_url}/v1/transcriptions",
                        headers=headers,
                        data=multipart,
                        timeout=self.timeout,
                    )
            except requests.RequestException as error:
                last_error = error
                self._observe_request(
                    operation="submit",
                    outcome=(
                        "retry" if attempt < self.attempts else "exhausted"
                    ),
                    attempt=attempt,
                    elapsed_seconds=time.monotonic() - started,
                    error_type=error.__class__.__name__,
                )
            if response is not None:
                retrying = (
                    response.status_code in transient_statuses
                    and attempt < self.attempts
                )
                if 200 <= response.status_code < 400:
                    outcome = "success"
                elif retrying:
                    outcome = "retry"
                elif response.status_code in transient_statuses:
                    outcome = "exhausted"
                else:
                    outcome = "http_error"
                self._observe_request(
                    operation="submit",
                    outcome=outcome,
                    attempt=attempt,
                    elapsed_seconds=time.monotonic() - started,
                    status_code=response.status_code,
                )
                if not retrying:
                    break
                response.close()
            if attempt < self.attempts:
                delay = float(2 ** (attempt - 1))
                LOGGER.warning(
                    "WAV upload failed; retrying in %.0fs (%d/%d)",
                    delay,
                    attempt,
                    self.attempts,
                )
                time.sleep(delay)
        if response is None:
            raise ExternalServiceError(
                "transcription API is unavailable after "
                f"{self.attempts} upload attempts: {last_error}"
            ) from last_error
        if response.status_code != 202:
            if response.status_code in {400, 413, 422}:
                raise RemoteTranscriptionFailed(
                    "transcription request was rejected: "
                    f"HTTP {response.status_code}: {_safe_error(response)}",
                    failure_code="invalid_input",
                    retryable=False,
                    failure_scope="job",
                )
            if response.status_code in {401, 403}:
                raise RemoteTranscriptionFailed(
                    "transcription API authentication failed",
                    failure_code="auth_required",
                    retryable=False,
                    failure_scope="configuration",
                )
            raise ExternalServiceError(
                "transcription submission failed: "
                f"HTTP {response.status_code}: {_safe_error(response)}"
            )
        try:
            return str(response.json()["id"])
        except (KeyError, TypeError, ValueError) as error:
            raise ExternalServiceError(
                "transcription API returned an invalid job response"
            ) from error


def batch_segments(
    segments: Sequence[Mapping[str, Any]],
    *,
    max_segments: int,
    max_characters: int,
) -> list[list[Mapping[str, Any]]]:
    if max_segments < 1 or max_characters < 1:
        raise ValueError("translation batch limits must be positive")
    batches: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    current_characters = 0
    for segment in segments:
        characters = len(str(segment["text"]))
        if current and (
            len(current) >= max_segments
            or current_characters + characters > max_characters
        ):
            batches.append(current)
            current = []
            current_characters = 0
        current.append(segment)
        current_characters += characters
    if current:
        batches.append(current)
    return batches


def normalize_translation_response(
    items: Any,
    expected_ids: Sequence[str],
) -> list[dict[str, str]]:
    """Accept reordered IDs, but reject missing, duplicate, or unrelated IDs."""
    if not isinstance(items, list):
        raise TranslationResponseIDError(
            "translation response must contain a translations list"
        )

    received: dict[str, str] = {}
    duplicate_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise TranslationResponseIDError(
                f"translation item {index} must be an object"
            )
        segment_id = str(item.get("id", "")).strip()
        text = str(item.get("text", "")).strip()
        if not segment_id or not text:
            raise TranslationResponseIDError(
                f"translation item {index} has an empty id or text"
            )
        if segment_id in received:
            duplicate_ids.add(segment_id)
        received[segment_id] = text

    expected = list(expected_ids)
    if len(expected) == 1 and len(items) == 1 and not duplicate_ids:
        return [{"id": expected[0], "text": next(iter(received.values()))}]

    expected_set = set(expected)
    received_set = set(received)
    missing = [segment_id for segment_id in expected if segment_id not in received]
    unexpected = sorted(received_set - expected_set)
    if missing or unexpected or duplicate_ids:
        details = []
        if missing:
            details.append(f"missing={missing[:5]}")
        if unexpected:
            details.append(f"unexpected={unexpected[:5]}")
        if duplicate_ids:
            details.append(f"duplicate={sorted(duplicate_ids)[:5]}")
        raise TranslationResponseIDError(
            "translation response ids do not match the request"
            + (f" ({', '.join(details)})" if details else "")
        )
    return [
        {"id": segment_id, "text": received[segment_id]}
        for segment_id in expected
    ]


def list_openai_compatible_models(
    base_url: str,
    token: str,
    *,
    attempts: int = 1,
    request_observer: RequestObserver | None = None,
) -> list[str]:
    """Return model identifiers exposed by an OpenAI-compatible server."""
    client = RetryingJSONClient(
        token=token,
        read_timeout=30.0,
        attempts=attempts,
        service_name="translation_lm",
        request_observer=request_observer,
    )
    response = client.request(
        "GET",
        f"{base_url.rstrip('/')}/models",
        headers=client.headers,
        metric_operation="models",
    )
    if response.status_code != 200:
        raise ExternalServiceError(
            "OpenAI-compatible model lookup failed: "
            f"HTTP {response.status_code}: {_safe_error(response)}"
        )
    try:
        data = response.json()["data"]
    except (KeyError, TypeError, ValueError) as error:
        raise ExternalServiceError(
            "OpenAI-compatible server returned an invalid model list"
        ) from error
    if not isinstance(data, list):
        raise ExternalServiceError(
            "OpenAI-compatible server returned an invalid model list"
        )

    model_ids: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(data):
        if not isinstance(item, Mapping):
            raise ExternalServiceError(
                f"model list item {index} must be an object"
            )
        model_id = str(item.get("id", "")).strip()
        if not model_id:
            raise ExternalServiceError(
                f"model list item {index} has an empty id"
            )
        if model_id not in seen:
            seen.add(model_id)
            model_ids.append(model_id)
    return sorted(model_ids, key=str.casefold)


class SubtitleValidationClient(RetryingJSONClient):
    """Run one explicit structured subtitle review against a paid LLM."""

    def __init__(
        self,
        base_url: str,
        token: str,
        model: str,
        *,
        provider: str = "openai_compatible",
        region: str = "",
        request_observer: RequestObserver | None = None,
    ) -> None:
        super().__init__(
            token=token,
            read_timeout=180.0,
            attempts=1,
            service_name="subtitle_validator",
            request_observer=request_observer,
        )
        if not model.strip():
            raise ValueError("subtitle validator model name is required")
        if provider not in {"openrouter", "bedrock", "openai_compatible"}:
            raise ValueError("unsupported subtitle validator provider")
        if provider == "bedrock" and not region.strip():
            raise ValueError("subtitle validator Bedrock region is required")
        self.base_url = (
            "https://openrouter.ai/api/v1"
            if provider == "openrouter"
            else base_url.rstrip("/")
        )
        self.model = model.strip()
        self.provider = provider
        self.region = region.strip()

    def validate(self, comparison: Mapping[str, Any]) -> dict[str, Any]:
        finding_schema = {
            "type": "object",
            "properties": {
                "reference_index": {"type": "integer"},
                "category": {
                    "type": "string",
                    "enum": [
                        "omission",
                        "meaning",
                        "naturalness",
                        "timing",
                        "other",
                    ],
                },
                "message": {"type": "string"},
            },
            "required": ["reference_index", "category", "message"],
            "additionalProperties": False,
        }
        schema = {
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "enum": ["pass", "review", "fail"],
                },
                "summary": {"type": "string"},
                "findings": {
                    "type": "array",
                    "items": finding_schema,
                },
            },
            "required": ["severity", "summary", "findings"],
            "additionalProperties": False,
        }
        system_prompt = (
            "외부 자막을 한국어 기준 자막으로 보고 생성 자막을 검수하라. "
            "의미 누락, 오역, 부자연스러운 표현, 타이밍 문제만 지적하고 "
            "문체 차이만으로 실패 판정하지 마라. 결과는 한국어로 작성하라."
        )
        if self.provider == "bedrock":
            return self._validate_bedrock(
                comparison,
                schema=schema,
                system_prompt=system_prompt,
            )
        payload: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": json.dumps(comparison, ensure_ascii=False),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "subtitle_validation",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        if self.provider == "openrouter":
            payload["provider"] = {"require_parameters": True}
        response = self.request(
            "POST",
            f"{self.base_url}/chat/completions",
            headers={**self.headers, "Content-Type": "application/json"},
            json=payload,
            metric_operation="validation",
        )
        if response.status_code != 200:
            raise ExternalServiceError(
                f"{self.provider} subtitle validation failed: "
                f"HTTP {response.status_code}: {_safe_error(response)}"
            )
        try:
            content = response.json()["choices"][0]["message"]["content"]
            result = json.loads(content) if isinstance(content, str) else content
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ExternalServiceError(
                f"{self.provider} subtitle validator returned invalid JSON"
            ) from error
        return _normalize_subtitle_validation(result)

    def _validate_bedrock(
        self,
        comparison: Mapping[str, Any],
        *,
        schema: Mapping[str, Any],
        system_prompt: str,
    ) -> dict[str, Any]:
        model_path = quote(self.model, safe="")
        response = self.request(
            "POST",
            (
                f"https://bedrock-runtime.{self.region}.amazonaws.com/"
                f"model/{model_path}/converse"
            ),
            headers={**self.headers, "Content-Type": "application/json"},
            json={
                "system": [{"text": system_prompt}],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": json.dumps(
                                    comparison,
                                    ensure_ascii=False,
                                )
                            }
                        ],
                    }
                ],
                "inferenceConfig": {"temperature": 0, "maxTokens": 4096},
                "outputConfig": {
                    "textFormat": {
                        "type": "json_schema",
                        "structure": {
                            "jsonSchema": {
                                "name": "subtitle_validation",
                                "schema": json.dumps(
                                    schema,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            }
                        },
                    }
                },
            },
            metric_operation="validation",
        )
        if response.status_code != 200:
            raise ExternalServiceError(
                "bedrock subtitle validation failed: "
                f"HTTP {response.status_code}: {_safe_error(response)}"
            )
        try:
            blocks = response.json()["output"]["message"]["content"]
            content = next(
                block["text"]
                for block in blocks
                if isinstance(block, Mapping) and "text" in block
            )
            result = json.loads(content) if isinstance(content, str) else content
        except (KeyError, StopIteration, TypeError, ValueError) as error:
            raise ExternalServiceError(
                "bedrock subtitle validator returned invalid JSON"
            ) from error
        return _normalize_subtitle_validation(result)


def _normalize_subtitle_validation(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ExternalServiceError("subtitle validation result must be an object")
    severity = str(value.get("severity", ""))
    summary = str(value.get("summary", "")).strip()
    findings = value.get("findings")
    if severity not in {"pass", "review", "fail"} or not summary:
        raise ExternalServiceError("subtitle validation summary is invalid")
    if not isinstance(findings, list) or len(findings) > 100:
        raise ExternalServiceError("subtitle validation findings are invalid")
    normalized: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, Mapping):
            raise ExternalServiceError("subtitle validation finding is invalid")
        reference_index = item.get("reference_index")
        category = str(item.get("category", ""))
        message = str(item.get("message", "")).strip()
        if (
            not isinstance(reference_index, int)
            or reference_index < 0
            or category
            not in {"omission", "meaning", "naturalness", "timing", "other"}
            or not message
        ):
            raise ExternalServiceError("subtitle validation finding is invalid")
        normalized.append(
            {
                "reference_index": reference_index,
                "category": category,
                "category_label": {
                    "omission": "누락",
                    "meaning": "의미 차이",
                    "naturalness": "자연스러움",
                    "timing": "타이밍",
                    "other": "기타",
                }[category],
                "message": message[:2000],
            }
        )
    return {
        "severity": severity,
        "severity_label": {
            "pass": "통과",
            "review": "검토 필요",
            "fail": "실패",
        }[severity],
        "summary": summary[:4000],
        "findings": normalized,
    }


class OpenAICompatibleClient(RetryingJSONClient):
    def __init__(
        self,
        base_url: str,
        token: str,
        model: str,
        *,
        max_segments: int = 30,
        max_characters: int = 6000,
        attempts: int = 3,
        request_limiter: RequestConcurrencyLimiter | None = None,
        request_observer: RequestObserver | None = None,
        completion_request: (
            Callable[[str, str, Mapping[str, Any]], requests.Response] | None
        ) = None,
    ) -> None:
        super().__init__(
            token=token,
            read_timeout=600.0,
            attempts=attempts,
            request_limiter=request_limiter,
            service_name="translation_lm",
            request_observer=request_observer,
        )
        if not model.strip() and completion_request is None:
            raise ValueError("OpenAI-compatible model name is required")
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.completion_request = completion_request
        self.max_segments = max_segments
        self.max_characters = max_characters
        self.translation_execution_mode = "live"

    def translate(
        self,
        segments: Sequence[Mapping[str, Any]],
        *,
        system_prompt: str = KOREAN_JAV_DRAFT_PROMPT,
        review_prompt: str = "",
        review_rounds: int = 0,
        existing: Mapping[str, str] | None = None,
        on_batch: Callable[[list[dict[str, str]]], None] | None = None,
        on_batch_started: Callable[[int, list[str]], None] | None = None,
        on_logical_batch: (
            Callable[[int, list[dict[str, str]]], None] | None
        ) = None,
        on_batch_failed: (
            Callable[[int, list[str], str], None] | None
        ) = None,
        on_progress: Callable[[int, int], None] | None = None,
        should_pause: Callable[[], bool] | None = None,
        on_review_warning: Callable[[str], None] | None = None,
        max_workers: int = 1,
        execution_mode: str = "live",
    ) -> list[dict[str, str]]:
        if not system_prompt.strip():
            raise ValueError("translation system prompt is required")
        if not 0 <= review_rounds <= 2:
            raise ValueError("review_rounds must be between 0 and 2")
        if review_rounds and not review_prompt.strip():
            raise ValueError("translation review prompt is required")
        if max_workers < 1:
            raise ValueError("translation max_workers must be at least 1")
        if execution_mode not in {"live", "batch"}:
            raise ValueError("translation execution_mode must be live or batch")
        self.translation_execution_mode = execution_mode
        expected_ids = [str(segment["id"]) for segment in segments]
        expected_set = set(expected_ids)
        known = {
            str(segment_id): str(text).strip()
            for segment_id, text in (existing or {}).items()
            if str(segment_id) in expected_set and str(text).strip()
        }
        ignored_existing = {
            str(segment_id) for segment_id in (existing or {})
        } - expected_set
        if ignored_existing:
            LOGGER.warning(
                "ignored %d stale translation checkpoint id(s)",
                len(ignored_existing),
            )

        pending = [
            segment for segment in segments if str(segment["id"]) not in known
        ]
        batches = batch_segments(
            pending,
            max_segments=self.max_segments,
            max_characters=self.max_characters,
        )
        if on_progress is not None:
            on_progress(0, len(batches))
        completed_batches = 0

        def accept_batch(
            batch_index: int,
            translated: list[dict[str, str]],
            review_warning: str | None,
        ) -> None:
            nonlocal completed_batches
            if review_warning is not None and on_review_warning is not None:
                on_review_warning(review_warning)
            if on_logical_batch is not None:
                on_logical_batch(batch_index, translated)
            for item in translated:
                known[item["id"]] = item["text"]
            completed_batches += 1
            if on_batch is not None:
                on_batch(
                    [
                        {"id": segment_id, "text": known[segment_id]}
                        for segment_id in expected_ids
                        if segment_id in known
                    ]
                )
            if on_progress is not None:
                on_progress(completed_batches, len(batches))

        if len(batches) <= 1 or max_workers == 1:
            for batch_index, batch in enumerate(batches):
                segment_ids = [str(segment["id"]) for segment in batch]
                if on_batch_started is not None:
                    on_batch_started(batch_index, segment_ids)
                try:
                    result = self._translate_logical_batch(
                        segments,
                        batch,
                        system_prompt=system_prompt,
                        review_prompt=review_prompt,
                        review_rounds=review_rounds,
                    )
                except Exception as error:
                    if on_batch_failed is not None:
                        on_batch_failed(batch_index, segment_ids, str(error))
                    raise
                accept_batch(batch_index, *result)
                if should_pause is not None and should_pause():
                    raise TranslationPaused(
                        "translation paused after checkpoint"
                    )
        else:
            self._translate_batches_in_parallel(
                segments,
                batches,
                system_prompt=system_prompt,
                review_prompt=review_prompt,
                review_rounds=review_rounds,
                max_workers=max_workers,
                accept_batch=accept_batch,
                on_batch_started=on_batch_started,
                on_batch_failed=on_batch_failed,
                should_pause=should_pause,
            )

        result = [
            {"id": segment_id, "text": known[segment_id]}
            for segment_id in expected_ids
            if segment_id in known
        ]
        return validate_translation_items(result, expected_ids)

    def _translate_batches_in_parallel(
        self,
        all_segments: Sequence[Mapping[str, Any]],
        batches: Sequence[Sequence[Mapping[str, Any]]],
        *,
        system_prompt: str,
        review_prompt: str,
        review_rounds: int,
        max_workers: int,
        accept_batch: Callable[
            [int, list[dict[str, str]], str | None], None
        ],
        on_batch_started: Callable[[int, list[str]], None] | None,
        on_batch_failed: Callable[[int, list[str], str], None] | None,
        should_pause: Callable[[], bool] | None,
    ) -> None:
        worker_count = min(max_workers, len(batches))
        executor = ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="translation-batch",
        )
        next_batch = 0
        in_flight: dict[
            Future[tuple[list[dict[str, str]], str | None]],
            int,
        ] = {}
        first_error: BaseException | None = None
        pause_requested = False

        def fill_available_slots() -> None:
            nonlocal next_batch
            while len(in_flight) < worker_count and next_batch < len(batches):
                batch_index = next_batch
                batch = batches[batch_index]
                next_batch += 1
                if on_batch_started is not None:
                    on_batch_started(
                        batch_index,
                        [str(segment["id"]) for segment in batch],
                    )
                future = executor.submit(
                    self._translate_logical_batch,
                    all_segments,
                    batch,
                    system_prompt=system_prompt,
                    review_prompt=review_prompt,
                    review_rounds=review_rounds,
                )
                in_flight[future] = batch_index

        try:
            fill_available_slots()
            while in_flight:
                done, _pending = wait(
                    tuple(in_flight),
                    return_when=FIRST_COMPLETED,
                )
                for future in sorted(done, key=in_flight.__getitem__):
                    batch_index = in_flight.pop(future)
                    try:
                        result = future.result()
                    except BaseException as error:
                        if on_batch_failed is not None:
                            on_batch_failed(
                                batch_index,
                                [
                                    str(segment["id"])
                                    for segment in batches[batch_index]
                                ],
                                str(error),
                            )
                        if first_error is None:
                            first_error = error
                    else:
                        try:
                            accept_batch(batch_index, *result)
                        except BaseException as error:
                            if first_error is None:
                                first_error = error
                if (
                    first_error is None
                    and not pause_requested
                    and should_pause is not None
                    and should_pause()
                ):
                    pause_requested = True
                if first_error is None and not pause_requested:
                    fill_available_slots()
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        if first_error is not None:
            raise first_error
        if pause_requested:
            raise TranslationPaused("translation paused after checkpoint")

    def _translate_logical_batch(
        self,
        all_segments: Sequence[Mapping[str, Any]],
        batch: Sequence[Mapping[str, Any]],
        *,
        system_prompt: str,
        review_prompt: str,
        review_rounds: int,
    ) -> tuple[list[dict[str, str]], str | None]:
        reference_context = self._reference_context(
            all_segments,
            batch,
            before=5,
            after=3,
        )
        draft = self._translate_batch_with_recovery(
            batch,
            reference_context,
            system_prompt,
        )
        translated = draft
        review_warning: str | None = None
        if review_rounds:
            for _round in range(review_rounds):
                try:
                    reviewed = self._review_batch_with_recovery(
                        batch,
                        reference_context,
                        translated,
                        review_prompt,
                    )
                    if reviewed == translated:
                        break
                    translated = reviewed
                except ExternalServiceError as error:
                    review_warning = str(error)
                    LOGGER.warning(
                        "translation review failed; using latest successful "
                        "translation: %s",
                        error,
                    )
                    break
        return translated, review_warning

    @staticmethod
    def _reference_context(
        all_segments: Sequence[Mapping[str, Any]],
        targets: Sequence[Mapping[str, Any]],
        *,
        before: int,
        after: int,
    ) -> list[Mapping[str, Any]]:
        if not targets:
            return []
        indices = {
            str(segment["id"]): index
            for index, segment in enumerate(all_segments)
        }
        target_indices = [indices[str(segment["id"])] for segment in targets]
        target_ids = {str(segment["id"]) for segment in targets}
        start = max(0, min(target_indices) - before)
        end = min(len(all_segments), max(target_indices) + after + 1)
        return [
            segment
            for segment in all_segments[start:end]
            if str(segment["id"]) not in target_ids
        ]

    def _translate_batch_with_recovery(
        self,
        segments: Sequence[Mapping[str, Any]],
        reference_context: Sequence[Mapping[str, Any]] = (),
        system_prompt: str = KOREAN_JAV_DRAFT_PROMPT,
    ) -> list[dict[str, str]]:
        try:
            return self._translate_batch(
                segments,
                reference_context,
                system_prompt,
            )
        except (TranslationResponseIDError, TranslationResponseFormatError):
            if len(segments) <= 1:
                raise
            midpoint = len(segments) // 2
            LOGGER.warning(
                "translation server returned a recoverable batch response; "
                "retrying as %d and %d segment batches",
                midpoint,
                len(segments) - midpoint,
            )
            return [
                *self._translate_batch_with_recovery(
                    segments[:midpoint],
                    [*reference_context, *segments[midpoint:]],
                    system_prompt,
                ),
                *self._translate_batch_with_recovery(
                    segments[midpoint:],
                    [*reference_context, *segments[:midpoint]],
                    system_prompt,
                ),
            ]

    def _review_batch_with_recovery(
        self,
        segments: Sequence[Mapping[str, Any]],
        reference_context: Sequence[Mapping[str, Any]],
        drafts: Sequence[Mapping[str, str]],
        review_prompt: str,
    ) -> list[dict[str, str]]:
        try:
            return self._review_batch(
                segments,
                reference_context,
                drafts,
                review_prompt,
            )
        except (TranslationResponseIDError, TranslationResponseFormatError):
            if len(segments) <= 1:
                raise
            midpoint = len(segments) // 2
            draft_by_id = {str(item["id"]): item for item in drafts}
            left = segments[:midpoint]
            right = segments[midpoint:]
            return [
                *self._review_batch_with_recovery(
                    left,
                    [*reference_context, *right],
                    [draft_by_id[str(segment["id"])] for segment in left],
                    review_prompt,
                ),
                *self._review_batch_with_recovery(
                    right,
                    [*reference_context, *left],
                    [draft_by_id[str(segment["id"])] for segment in right],
                    review_prompt,
                ),
            ]

    def _translate_batch(
        self,
        segments: Sequence[Mapping[str, Any]],
        reference_context: Sequence[Mapping[str, Any]] = (),
        system_prompt: str = KOREAN_JAV_DRAFT_PROMPT,
    ) -> list[dict[str, str]]:
        return self._request_translation_items(
            system_prompt=system_prompt,
            schema_name="subtitle_translation",
            error_label="translation",
            expected_ids=[str(segment["id"]) for segment in segments],
            user_payload={
                "target_segments": [
                    {"id": str(segment["id"]), "text": str(segment["text"])}
                    for segment in segments
                ],
                "reference_context": [
                    {"id": str(segment["id"]), "text": str(segment["text"])}
                    for segment in reference_context
                ],
            },
        )

    def _review_batch(
        self,
        segments: Sequence[Mapping[str, Any]],
        reference_context: Sequence[Mapping[str, Any]],
        drafts: Sequence[Mapping[str, str]],
        review_prompt: str,
    ) -> list[dict[str, str]]:
        return self._request_translation_items(
            system_prompt=review_prompt,
            schema_name="subtitle_translation_review",
            error_label="translation review",
            expected_ids=[str(segment["id"]) for segment in segments],
            user_payload={
                "target_segments": [
                    {"id": str(segment["id"]), "text": str(segment["text"])}
                    for segment in segments
                ],
                "reference_context": [
                    {"id": str(segment["id"]), "text": str(segment["text"])}
                    for segment in reference_context
                ],
                "draft_translations": [dict(item) for item in drafts],
            },
        )

    def _request_translation_items(
        self,
        *,
        system_prompt: str,
        schema_name: str,
        error_label: str,
        expected_ids: list[str],
        user_payload: Mapping[str, Any],
    ) -> list[dict[str, str]]:
        schema = {
            "type": "object",
            "properties": {
                "translations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["id", "text"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["translations"],
            "additionalProperties": False,
        }
        request_payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 4096,
            # Translation needs the schema-constrained answer, not a hidden
            # reasoning trace. Thinking models can otherwise exhaust their
            # context window before emitting message.content.
            "reasoning_effort": "none",
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        stage = "review" if "review" in schema_name else "draft"
        if self.completion_request is not None:
            response = self.completion_request(
                stage,
                self.translation_execution_mode,
                request_payload,
            )
        else:
            response = self.request(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={
                    **self.headers,
                    "Content-Type": "application/json",
                },
                json=request_payload,
                metric_operation=(
                    "review" if stage == "review" else "translation"
                ),
            )
        if response.status_code != 200:
            raise ExternalServiceError(
                f"OpenAI-compatible {error_label} failed: "
                f"HTTP {response.status_code}: {_safe_error(response)}"
            )
        try:
            choice = response.json()["choices"][0]
            content = choice["message"]["content"]
            if str(choice.get("finish_reason", "")).strip().lower() == "length":
                raise TranslationResponseFormatError(
                    f"OpenAI-compatible {error_label} reached its output limit"
                )
            decoded = json.loads(content) if isinstance(content, str) else content
            translations = decoded["translations"]
        except TranslationResponseFormatError:
            raise
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise TranslationResponseFormatError(
                f"OpenAI-compatible {error_label} server returned invalid "
                "structured translation JSON"
            ) from error
        return normalize_translation_response(translations, expected_ids)


# Backward-compatible import for callers that used the former product-specific
# class name. New code should use OpenAICompatibleClient.
LMStudioClient = OpenAICompatibleClient
