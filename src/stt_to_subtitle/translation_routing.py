"""Backend-owned routing for independent OpenAI-compatible translation groups."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3
import threading
from typing import Any
from urllib.parse import urlsplit

import requests

from .service_clients import (
    ExternalServiceError,
    RequestConcurrencyLimiter,
    RequestObserver,
    RetryingJSONClient,
)
from .translation_store import (
    TranslationServer,
    TranslationServerGroupStore,
    migrate_legacy_translation_endpoints,
)


LOGGER = logging.getLogger(__name__)
DEFAULT_DRAFT_MODEL = (
    "gemma-4-12b-coder-fable5-composer2.5-v1-uncensored-heretic"
)
DEFAULT_REVIEW_MODEL = "gemma-4-26b-a4b-it-ultra-uncensored-heretic"
TRANSLATION_STAGES = ("draft", "review")
TRANSLATION_STAGE_LABELS = {
    "draft": "1차(초벌) 번역",
    "review": "2차(검증) 번역",
}
OPENAI_COMPLETION_FIELDS = frozenset(
    {"messages", "temperature", "response_format"}
)


def normalize_translation_url(value: str, setting: str = "base_url") -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{setting} must be an http(s) URL")
    return normalized


def translation_stage(value: str) -> str:
    if value not in TRANSLATION_STAGES:
        raise ValueError("지원하지 않는 번역 단계입니다.")
    return value


def _model_ids(response: requests.Response) -> list[str]:
    try:
        payload = response.json()
        data = payload["data"]
    except (KeyError, TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return sorted(
        {
            str(item.get("id", "")).strip()
            for item in data
            if isinstance(item, Mapping) and str(item.get("id", "")).strip()
        },
        key=str.casefold,
    )


def _server_headers(server: TranslationServer) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if server.token:
        headers["Authorization"] = f"Bearer {server.token}"
    return headers


@dataclass(frozen=True)
class TranslationRoutingDefaults:
    state_dir: Path
    builtin_name: str = "기본 서버"
    builtin_base_url: str = ""
    builtin_token: str = ""
    builtin_capacity: int = 1
    draft_enabled: bool = True
    review_enabled: bool = False
    draft_model: str = DEFAULT_DRAFT_MODEL
    review_model: str = DEFAULT_REVIEW_MODEL
    draft_batch_preferred: bool = False
    review_batch_preferred: bool = False
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 600.0

    def normalized(self) -> TranslationRoutingDefaults:
        if not self.builtin_name.strip():
            raise ValueError("기본 번역 서버 이름이 필요합니다.")
        if not 1 <= self.builtin_capacity <= 8:
            raise ValueError("기본 번역 서버 동시 요청 수는 1~8이어야 합니다.")
        if not self.draft_model.strip() or not self.review_model.strip():
            raise ValueError("1차·2차 번역 모델이 필요합니다.")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("번역 서버 제한 시간은 양수여야 합니다.")
        return TranslationRoutingDefaults(
            state_dir=self.state_dir,
            builtin_name=self.builtin_name.strip(),
            builtin_base_url=(
                normalize_translation_url(
                    self.builtin_base_url,
                    "TRANSLATION_BUILTIN_BASE_URL",
                )
                if self.builtin_base_url.strip()
                else ""
            ),
            builtin_token=self.builtin_token,
            builtin_capacity=self.builtin_capacity,
            draft_enabled=self.draft_enabled,
            review_enabled=self.review_enabled,
            draft_model=self.draft_model.strip(),
            review_model=self.review_model.strip(),
            draft_batch_preferred=self.draft_batch_preferred,
            review_batch_preferred=self.review_batch_preferred,
            connect_timeout_seconds=self.connect_timeout_seconds,
            read_timeout_seconds=self.read_timeout_seconds,
        )


class BackendTranslationRouting:
    """Store translation groups and call their OpenAI-compatible APIs directly."""

    def __init__(
        self,
        defaults: TranslationRoutingDefaults,
        *,
        stores: Mapping[str, TranslationServerGroupStore] | None = None,
    ) -> None:
        self.defaults = defaults.normalized()
        self.stores = dict(stores or {
            stage: TranslationServerGroupStore(
                self.defaults.state_dir
                / f"translation-{stage}-servers.sqlite3"
            )
            for stage in TRANSLATION_STAGES
        })
        if set(self.stores) != set(TRANSLATION_STAGES):
            raise ValueError("draft and review translation stores are required")
        migrate_legacy_translation_endpoints(
            self.defaults.state_dir / "translation-endpoints.sqlite3",
            draft_store=self.stores["draft"],
            review_store=self.stores["review"],
        )
        self.stores["draft"].ensure_model(self.defaults.draft_model)
        self.stores["review"].ensure_model(self.defaults.review_model)
        self.stores["draft"].sync_builtin(
            name=self.defaults.builtin_name,
            base_url=self.defaults.builtin_base_url,
            token=self.defaults.builtin_token,
            enabled=self.defaults.draft_enabled,
            capacity=self.defaults.builtin_capacity,
            batch_preferred=self.defaults.draft_batch_preferred,
            legacy_names=("기본 Runtime",),
        )
        self.stores["review"].sync_builtin(
            name=self.defaults.builtin_name,
            base_url=self.defaults.builtin_base_url,
            token=self.defaults.builtin_token,
            enabled=self.defaults.review_enabled,
            capacity=self.defaults.builtin_capacity,
            batch_preferred=self.defaults.review_batch_preferred,
            legacy_names=("기본 Runtime",),
        )
        self._lock = threading.RLock()
        self._health: dict[tuple[str, str], dict[str, Any]] = {}
        self._active_requests: dict[tuple[str, str], int] = {}

    def _store(self, stage: str) -> TranslationServerGroupStore:
        return self.stores[translation_stage(stage)]

    def _public_server(
        self,
        stage: str,
        server: TranslationServer,
    ) -> dict[str, Any]:
        key = (stage, server.id)
        with self._lock:
            current = dict(self._health.get(key, {}))
            running = self._active_requests.get(key, 0)
        if not server.enabled:
            status = "disabled"
        elif not server.base_url:
            status = "unconfigured"
        else:
            status = str(current.get("status", "unknown"))
        return {
            "id": server.id,
            "stage": stage,
            "name": server.name,
            "base_url": server.base_url,
            "token_configured": bool(server.token),
            "enabled": server.enabled,
            "capacity": server.capacity,
            "builtin": server.builtin,
            "batch_preferred": server.batch_preferred,
            "models": list(server.models),
            "status": status,
            "message": current.get("message"),
            "checked_at": server.checked_at,
            "running_jobs": running,
            "available_slots": max(0, server.capacity - running),
        }

    def group(self, stage: str) -> dict[str, Any]:
        resolved = translation_stage(stage)
        store = self.stores[resolved]
        return {
            "stage": resolved,
            "label": TRANSLATION_STAGE_LABELS[resolved],
            "model": store.model(),
            "servers": [
                self._public_server(resolved, server) for server in store.list()
            ],
        }

    def groups(self) -> list[dict[str, Any]]:
        return [self.group(stage) for stage in TRANSLATION_STAGES]

    def update_model(self, stage: str, model: str) -> dict[str, Any]:
        resolved = translation_stage(stage)
        normalized = model.strip()
        available = {
            item
            for server in self.stores[resolved].list()
            for item in server.models
        }
        if available and normalized not in available:
            raise ValueError("등록된 서버가 제공하지 않는 모델입니다.")
        self.stores[resolved].set_model(normalized)
        return self.group(resolved)

    @staticmethod
    def _server_values(payload: Mapping[str, Any]) -> tuple[str, str, int]:
        name = str(payload.get("name", "")).strip()
        if not name or len(name) > 80:
            raise ValueError("번역 서버 이름은 1~80자여야 합니다.")
        base_url = normalize_translation_url(str(payload.get("base_url", "")))
        capacity = int(payload.get("capacity", 1))
        if not 1 <= capacity <= 8:
            raise ValueError("번역 서버 동시 요청 수는 1~8이어야 합니다.")
        return name, base_url, capacity

    def create_server(
        self,
        stage: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        resolved = translation_stage(stage)
        name, base_url, capacity = self._server_values(payload)
        try:
            server = self.stores[resolved].create(
                name=name,
                base_url=base_url,
                token=str(payload.get("token", "")),
                enabled=bool(payload.get("enabled", True)),
                capacity=capacity,
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("같은 주소의 번역 서버가 이미 등록되어 있습니다.") from error
        if server.enabled:
            try:
                server = self._probe(resolved, server)
            except ExternalServiceError:
                pass
        return self._public_server(resolved, server)

    def update_server(
        self,
        stage: str,
        server_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        resolved = translation_stage(stage)
        store = self.stores[resolved]
        current = store.get(server_id)
        if current is None:
            raise ValueError("번역 서버를 찾을 수 없습니다.")
        name, base_url, capacity = self._server_values(payload)
        token_value = payload.get("token")
        token = "" if bool(payload.get("clear_token")) else (
            str(token_value) if token_value is not None else current.token
        )
        try:
            server = store.update(
                server_id,
                name=name,
                base_url=base_url,
                token=token,
                enabled=bool(payload.get("enabled", True)),
                capacity=capacity,
            )
        except sqlite3.IntegrityError as error:
            raise ValueError("같은 주소의 번역 서버가 이미 등록되어 있습니다.") from error
        with self._lock:
            self._health.pop((resolved, server_id), None)
        if server.enabled:
            try:
                server = self._probe(resolved, server)
            except ExternalServiceError:
                pass
        return self._public_server(resolved, server)

    def delete_server(self, stage: str, server_id: str) -> None:
        resolved = translation_stage(stage)
        key = (resolved, server_id)
        with self._lock:
            if self._active_requests.get(key, 0):
                raise ValueError("진행 중인 번역 요청이 있습니다.")
        if not self.stores[resolved].delete(server_id):
            raise ValueError("추가 번역 서버를 찾을 수 없습니다.")
        with self._lock:
            self._health.pop(key, None)

    def update_routing(
        self,
        stage: str,
        server_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        resolved = translation_stage(stage)
        server = self.stores[resolved].set_routing(
            server_id,
            enabled=bool(payload.get("enabled", True)),
            batch_preferred=bool(payload.get("batch_preferred", False)),
        )
        return self._public_server(resolved, server)

    def _probe(self, stage: str, server: TranslationServer) -> TranslationServer:
        key = (stage, server.id)
        if not server.enabled:
            with self._lock:
                self._health[key] = {"status": "disabled", "message": None}
            return server
        if not server.base_url:
            raise ExternalServiceError("번역 서버 주소가 설정되지 않았습니다.")
        try:
            response = requests.get(
                f"{server.base_url}/models",
                headers=_server_headers(server),
                timeout=(self.defaults.connect_timeout_seconds, 30.0),
            )
            models = _model_ids(response) if response.status_code == 200 else []
            if not models:
                raise ExternalServiceError(
                    "번역 서버에서 모델 목록을 조회할 수 없습니다."
                )
            server = self.stores[stage].save_models(server.id, models)
        except (requests.RequestException, ExternalServiceError) as error:
            with self._lock:
                self._health[key] = {
                    "status": "unavailable",
                    "message": "번역 서버에서 모델 목록을 조회할 수 없습니다.",
                }
            if isinstance(error, ExternalServiceError):
                raise
            raise ExternalServiceError(
                "번역 서버에서 모델 목록을 조회할 수 없습니다."
            ) from error
        with self._lock:
            self._health[key] = {"status": "ready", "message": None}
        return server

    def probe_server(self, stage: str, server_id: str) -> dict[str, Any]:
        resolved = translation_stage(stage)
        server = self.stores[resolved].get(server_id)
        if server is None:
            raise ValueError("번역 서버를 찾을 수 없습니다.")
        return self._public_server(resolved, self._probe(resolved, server))

    def _candidates(self, stage: str, mode: str) -> list[TranslationServer]:
        resolved = translation_stage(stage)
        if mode not in {"live", "batch"}:
            raise ValueError("번역 실행 모드는 live 또는 batch여야 합니다.")
        store = self.stores[resolved]
        model = store.model()
        candidates = [
            server
            for server in store.list()
            if server.enabled
            and server.base_url
            and (not server.models or model in server.models)
            and (mode != "batch" or server.batch_preferred)
        ]
        with self._lock:
            running = dict(self._active_requests)
            statuses = {
                key: str(value.get("status", "unknown"))
                for key, value in self._health.items()
            }
        candidates.sort(
            key=lambda server: (
                statuses.get((resolved, server.id)) == "unavailable",
                running.get((resolved, server.id), 0) >= server.capacity,
                running.get((resolved, server.id), 0) / server.capacity,
                not server.builtin,
                server.created_at,
            )
        )
        return candidates

    def is_configured(self, stage: str = "draft", mode: str = "live") -> bool:
        return bool(self._candidates(stage, mode))

    def worker_limit(self, mode: str) -> int:
        """Return the usable draft concurrency for one translation job."""
        candidates = self._candidates("draft", mode)
        return max(1, min(8, sum(server.capacity for server in candidates)))

    def model_contract(self) -> str:
        return "+".join(
            f"{stage}:{self.stores[stage].model()}"
            for stage in TRANSLATION_STAGES
        )

    def endpoint_contract(self, mode: str) -> str:
        server_ids = [
            f"{stage}:{','.join(server.id for server in self._candidates(stage, mode))}"
            for stage in TRANSLATION_STAGES
        ]
        return f"backend-direct:{mode}:" + ";".join(server_ids)

    def tokens(self) -> set[str]:
        return {
            server.token
            for store in self.stores.values()
            for server in store.list()
            if server.token
        }

    def request_completion(
        self,
        stage: str,
        mode: str,
        payload: Mapping[str, Any],
        *,
        request_limiter: RequestConcurrencyLimiter | None = None,
        request_observer: RequestObserver | None = None,
    ) -> requests.Response:
        resolved = translation_stage(stage)
        candidates = self._candidates(resolved, mode)
        if not candidates:
            raise ExternalServiceError(
                f"{TRANSLATION_STAGE_LABELS[resolved]} 서버가 설정되지 않았습니다."
            )
        model = self.stores[resolved].model()
        last_response: requests.Response | None = None
        attempted = False
        for server in candidates:
            key = (resolved, server.id)
            with self._lock:
                running = self._active_requests.get(key, 0)
                if running >= server.capacity:
                    continue
                self._active_requests[key] = running + 1
            attempted = True
            client = RetryingJSONClient(
                token=server.token,
                read_timeout=self.defaults.read_timeout_seconds,
                attempts=1,
                request_limiter=request_limiter,
                service_name=f"translation_{resolved}",
                request_observer=request_observer,
            )
            upstream_payload = {
                key: value
                for key, value in payload.items()
                if key in OPENAI_COMPLETION_FIELDS
            }
            upstream_payload["model"] = model
            try:
                response = client.request(
                    "POST",
                    f"{server.base_url}/chat/completions",
                    headers={**client.headers, "Content-Type": "application/json"},
                    json=upstream_payload,
                    metric_operation=(
                        "review" if resolved == "review" else "translation"
                    ),
                )
            except ExternalServiceError:
                with self._lock:
                    self._health[key] = {
                        "status": "unavailable",
                        "message": "번역 요청에 응답하지 않습니다.",
                    }
                LOGGER.warning(
                    "translation request failed: stage=%s mode=%s server=%s",
                    resolved,
                    mode,
                    server.id,
                )
                continue
            finally:
                with self._lock:
                    self._active_requests[key] = max(
                        0,
                        self._active_requests.get(key, 1) - 1,
                    )
            if 200 <= response.status_code < 300:
                with self._lock:
                    self._health[key] = {"status": "ready", "message": None}
                if last_response is not None:
                    last_response.close()
                return response
            if last_response is not None:
                last_response.close()
            last_response = response
        if last_response is not None:
            return last_response
        if not attempted:
            raise ExternalServiceError("모든 번역 서버의 동시 요청 수가 가득 찼습니다.")
        raise ExternalServiceError("번역 서버에 연결할 수 없습니다.")
