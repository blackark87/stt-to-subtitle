"""HTTP clients for the independent STT and translation services."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import json
import logging
from pathlib import Path
import time
from typing import Any

import requests

from .contracts import validate_transcript, validate_translation_items
from .translation_prompt import KOREAN_JAV_SYSTEM_PROMPT

LOGGER = logging.getLogger(__name__)


class ExternalServiceError(RuntimeError):
    """An external stage cannot currently make progress."""


class TranslationResponseIDError(ExternalServiceError):
    """The translation server returned a different segment ID set."""


class TranslationPaused(RuntimeError):
    """Translation stopped cleanly after a persisted logical batch."""


class OperationStopped(RuntimeError):
    """A local pipeline stage reached a safe user-requested stop point."""


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
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        self.token = token
        self.timeout = (connect_timeout, read_timeout)
        self.attempts = attempts
        self.session = requests.Session()

    @property
    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        transient_statuses = {408, 429, 500, 502, 503, 504}
        last_error: BaseException | None = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=self.timeout,
                    **kwargs,
                )
                if (
                    response.status_code not in transient_statuses
                    or attempt == self.attempts
                ):
                    return response
            except requests.RequestException as error:
                last_error = error
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


class STTAPIClient(RetryingJSONClient):
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        poll_interval: float = 5.0,
        attempts: int = 3,
    ) -> None:
        super().__init__(
            token=token,
            read_timeout=300.0,
            attempts=attempts,
        )
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval

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
        last_progress: tuple[int, int, int, int, bool] | None = None
        while True:
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

            response = self.request(
                "GET",
                f"{self.base_url}/v1/transcriptions/{job_id}",
                headers=self.headers,
            )
            if response.status_code != 200:
                raise ExternalServiceError(
                    "transcription status request failed: "
                    f"HTTP {response.status_code}: {_safe_error(response)}"
                )
            try:
                status_payload = response.json()
                remote_status = str(status_payload["status"])
            except (KeyError, TypeError, ValueError) as error:
                raise ExternalServiceError(
                    "transcription API returned an invalid status response"
                ) from error
            progress = status_payload.get("chunk_progress")
            if on_progress is not None and isinstance(progress, Mapping):
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
                    on_progress(
                        {
                            "created": current_progress[0],
                            "completed": current_progress[1],
                            "in_progress": current_progress[2],
                            "report_every": current_progress[3],
                            "final": current_progress[4],
                        }
                    )
            if remote_status == "completed":
                break
            if remote_status == "failed":
                if may_requeue_existing:
                    may_requeue_existing = False
                    job_id = None
                    last_progress = None
                    continue
                raise ExternalServiceError(
                    "transcription job failed: "
                    f"{status_payload.get('error', 'unknown remote error')}"
                )
            if remote_status not in {"queued", "running"}:
                raise ExternalServiceError(
                    f"transcription job returned unknown status {remote_status}"
                )
            time.sleep(self.poll_interval)

        if should_stop is not None and should_stop():
            raise OperationStopped("transcription stop requested")
        response = self.request(
            "GET",
            f"{self.base_url}/v1/transcriptions/{job_id}/result",
            headers=self.headers,
        )
        if response.status_code != 200:
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
        validate_transcript(payload)
        return payload

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
            if response is not None and (
                response.status_code not in transient_statuses
                or attempt == self.attempts
            ):
                break
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
) -> list[str]:
    """Return model identifiers exposed by an OpenAI-compatible server."""
    client = RetryingJSONClient(
        token=token,
        read_timeout=30.0,
        attempts=attempts,
    )
    response = client.request(
        "GET",
        f"{base_url.rstrip('/')}/models",
        headers=client.headers,
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
    ) -> None:
        super().__init__(
            token=token,
            read_timeout=600.0,
            attempts=attempts,
        )
        if not model.strip():
            raise ValueError("OpenAI-compatible model name is required")
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.max_segments = max_segments
        self.max_characters = max_characters

    def translate(
        self,
        segments: Sequence[Mapping[str, Any]],
        *,
        system_prompt: str = KOREAN_JAV_SYSTEM_PROMPT,
        review_prompt: str = "",
        review_rounds: int = 0,
        existing: Mapping[str, str] | None = None,
        on_batch: Callable[[list[dict[str, str]]], None] | None = None,
        on_progress: Callable[[int, int], None] | None = None,
        should_pause: Callable[[], bool] | None = None,
        on_review_warning: Callable[[str], None] | None = None,
    ) -> list[dict[str, str]]:
        if not system_prompt.strip():
            raise ValueError("translation system prompt is required")
        if not 0 <= review_rounds <= 2:
            raise ValueError("review_rounds must be between 0 and 2")
        if review_rounds and not review_prompt.strip():
            raise ValueError("translation review prompt is required")
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
        for completed_batches, batch in enumerate(batches, start=1):
            reference_context = self._reference_context(
                segments,
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
            if review_rounds:
                try:
                    for _round in range(review_rounds):
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
                    translated = draft
                    LOGGER.warning(
                        "translation review failed; using initial translation: %s",
                        error,
                    )
                    if on_review_warning is not None:
                        on_review_warning(str(error))
            for item in translated:
                known[item["id"]] = item["text"]
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
            if should_pause is not None and should_pause():
                raise TranslationPaused("translation paused after checkpoint")

        result = [
            {"id": segment_id, "text": known[segment_id]}
            for segment_id in expected_ids
            if segment_id in known
        ]
        return validate_translation_items(result, expected_ids)

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
        system_prompt: str = KOREAN_JAV_SYSTEM_PROMPT,
    ) -> list[dict[str, str]]:
        try:
            return self._translate_batch(
                segments,
                reference_context,
                system_prompt,
            )
        except TranslationResponseIDError:
            if len(segments) <= 1:
                raise
            midpoint = len(segments) // 2
            LOGGER.warning(
                "translation server returned mismatched translation IDs; "
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
        except TranslationResponseIDError:
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
        system_prompt: str = KOREAN_JAV_SYSTEM_PROMPT,
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
        response = self.request(
            "POST",
            f"{self.base_url}/chat/completions",
            headers={**self.headers, "Content-Type": "application/json"},
            json=request_payload,
        )
        if response.status_code != 200:
            raise ExternalServiceError(
                f"OpenAI-compatible {error_label} failed: "
                f"HTTP {response.status_code}: {_safe_error(response)}"
            )
        try:
            content = response.json()["choices"][0]["message"]["content"]
            decoded = json.loads(content) if isinstance(content, str) else content
            translations = decoded["translations"]
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise ExternalServiceError(
                f"OpenAI-compatible {error_label} server returned invalid "
                "structured translation JSON"
            ) from error
        return normalize_translation_response(translations, expected_ids)


# Backward-compatible import for callers that used the former product-specific
# class name. New code should use OpenAICompatibleClient.
LMStudioClient = OpenAICompatibleClient
