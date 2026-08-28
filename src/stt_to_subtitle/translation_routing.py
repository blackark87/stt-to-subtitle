"""Backend-owned routing for independent OpenAI-compatible translation groups."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from .service_clients import (
    ExternalServiceError,
    RequestConcurrencyLimiter,
    RequestObserver,
    RetryingJSONClient,
    TranslationDeferred,
)
from .translation_store import (
    TranslationServer,
    TranslationServerGroupStore,
    migrate_legacy_translation_endpoints,
)


LOGGER = logging.getLogger(__name__)
TRANSLATION_STAGES = ("draft", "review")
TRANSLATION_STAGE_LABELS = {
    "draft": "1차(초벌) 번역",
    "review": "2차(검증) 번역",
}
OPENAI_COMPLETION_FIELDS = frozenset(
    {
        "max_tokens",
        "messages",
        "temperature",
        "reasoning_effort",
        "response_format",
    }
)
HARD_BREAKER_MESSAGE = (
    "전사 모델의 메모리를 보호하기 위해 번역 서버를 일시 중지했습니다."
)
REVIEW_PRIORITY_MESSAGE = (
    "같은 서버의 2차 검수를 우선 처리하는 동안 1차 요청을 다른 "
    "서버로 전환합니다."
)
REVIEW_REQUEST_ATTEMPTS = 7


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
    draft_batch_preferred: bool = False
    review_batch_preferred: bool = False
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 600.0
    stt_hard_breaker_hosts: tuple[str, ...] = ()
    stt_hard_breaker_timeout_seconds: float = 600.0

    def normalized(self) -> TranslationRoutingDefaults:
        if not self.builtin_name.strip():
            raise ValueError("기본 번역 서버 이름이 필요합니다.")
        if not 1 <= self.builtin_capacity <= 8:
            raise ValueError("기본 번역 서버 동시 요청 수는 1~8이어야 합니다.")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("번역 서버 제한 시간은 양수여야 합니다.")
        if self.stt_hard_breaker_timeout_seconds <= 0:
            raise ValueError("번역/STT 하드 브레이커 제한 시간은 양수여야 합니다.")
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
            draft_batch_preferred=self.draft_batch_preferred,
            review_batch_preferred=self.review_batch_preferred,
            connect_timeout_seconds=self.connect_timeout_seconds,
            read_timeout_seconds=self.read_timeout_seconds,
            stt_hard_breaker_hosts=tuple(dict.fromkeys(
                host.strip().casefold().rstrip(".")
                for host in self.stt_hard_breaker_hosts
                if host.strip()
            )),
            stt_hard_breaker_timeout_seconds=(
                self.stt_hard_breaker_timeout_seconds
            ),
        )


class BackendTranslationRouting:
    """Store translation groups and call their OpenAI-compatible APIs directly."""

    def __init__(
        self,
        defaults: TranslationRoutingDefaults,
        *,
        stores: Mapping[str, TranslationServerGroupStore] | None = None,
        stt_hard_breaker_active: Callable[[], bool] | None = None,
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
        self._condition = threading.Condition(self._lock)
        self._health: dict[tuple[str, str], dict[str, Any]] = {}
        self._active_requests: dict[tuple[str, str], int] = {}
        self._review_host_holders: dict[str, int] = {}
        self._stt_hard_breaker_active = stt_hard_breaker_active
        self._hard_breaker_holders = 0

    def _store(self, stage: str) -> TranslationServerGroupStore:
        return self.stores[translation_stage(stage)]

    @staticmethod
    def _server_host(server: TranslationServer) -> str:
        return (urlsplit(server.base_url).hostname or "").casefold().rstrip(".")

    def _uses_shared_stt_memory(self, server: TranslationServer) -> bool:
        return self._server_host(server) in set(
            self.defaults.stt_hard_breaker_hosts
        )

    def _active_requests_on_host_locked(self, stage: str, host: str) -> int:
        return sum(
            self._active_requests.get((stage, server.id), 0)
            for server in self.stores[stage].list()
            if self._server_host(server) == host
        )

    def _review_priority_active_locked(self, server: TranslationServer) -> bool:
        return self._review_host_holders.get(self._server_host(server), 0) > 0

    def _external_hard_breaker_active(self) -> bool:
        if self._stt_hard_breaker_active is None:
            return False
        try:
            return bool(self._stt_hard_breaker_active())
        except (OSError, RuntimeError, sqlite3.Error):
            LOGGER.exception("failed to read shared-accelerator STT state")
            return True

    def hard_breaker_active(self) -> bool:
        with self._lock:
            locally_held = self._hard_breaker_holders > 0
        return locally_held or self._external_hard_breaker_active()

    def _hard_blocked_server_keys(self) -> set[tuple[str, str]]:
        return {
            (stage, server.id)
            for stage in TRANSLATION_STAGES
            for server in self.stores[stage].list()
            if self._uses_shared_stt_memory(server)
        }

    def _ollama_origins(self) -> dict[str, TranslationServer]:
        origins: dict[str, TranslationServer] = {}
        for stage in TRANSLATION_STAGES:
            for server in self.stores[stage].list():
                if not server.base_url or not self._uses_shared_stt_memory(server):
                    continue
                parsed = urlsplit(server.base_url)
                origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
                origins.setdefault(origin, server)
        return origins

    @staticmethod
    def _loaded_ollama_models(response: requests.Response) -> list[str]:
        if response.status_code != 200:
            raise ExternalServiceError(
                "공유 메모리 번역 서버의 모델 상태를 확인할 수 없습니다."
            )
        try:
            models = response.json()["models"]
        except (KeyError, TypeError, ValueError, requests.JSONDecodeError) as error:
            raise ExternalServiceError(
                "공유 메모리 번역 서버의 모델 상태 응답이 올바르지 않습니다."
            ) from error
        if not isinstance(models, list):
            raise ExternalServiceError(
                "공유 메모리 번역 서버의 모델 상태 응답이 올바르지 않습니다."
            )
        return sorted({
            str(item.get("name") or item.get("model") or "").strip()
            for item in models
            if isinstance(item, Mapping)
            and str(item.get("name") or item.get("model") or "").strip()
        })

    def _ollama_models(self, origin: str, server: TranslationServer) -> list[str]:
        try:
            response = requests.get(
                f"{origin}/api/ps",
                headers=_server_headers(server),
                timeout=(self.defaults.connect_timeout_seconds, 30.0),
            )
        except requests.RequestException as error:
            raise ExternalServiceError(
                "공유 메모리 번역 서버의 모델 상태를 확인할 수 없습니다."
            ) from error
        try:
            return self._loaded_ollama_models(response)
        finally:
            response.close()

    def _unload_ollama_models(self) -> list[str]:
        unloaded: list[str] = []
        for origin, server in self._ollama_origins().items():
            loaded = self._ollama_models(origin, server)
            for model in loaded:
                try:
                    response = requests.post(
                        f"{origin}/api/generate",
                        headers=_server_headers(server),
                        json={
                            "model": model,
                            "prompt": "",
                            "stream": False,
                            "keep_alive": 0,
                        },
                        timeout=(self.defaults.connect_timeout_seconds, 30.0),
                    )
                except requests.RequestException as error:
                    raise ExternalServiceError(
                        "공유 메모리 번역 모델을 강제 언로드할 수 없습니다."
                    ) from error
                try:
                    if not 200 <= response.status_code < 300:
                        raise ExternalServiceError(
                            "공유 메모리 번역 모델을 강제 언로드할 수 없습니다."
                        )
                finally:
                    response.close()
                unloaded.append(model)
            if self._ollama_models(origin, server):
                raise ExternalServiceError(
                    "공유 메모리 번역 모델이 언로드되지 않아 전사를 차단했습니다."
                )
        return unloaded

    def engage_stt_hard_breaker(self) -> dict[str, Any]:
        """Drain translation calls and unload Ollama before local STT starts."""
        if not self.defaults.stt_hard_breaker_hosts:
            return {"enabled": False, "unloaded_models": []}
        blocked_keys = self._hard_blocked_server_keys()
        deadline = (
            time.monotonic()
            + self.defaults.stt_hard_breaker_timeout_seconds
        )
        with self._condition:
            self._hard_breaker_holders += 1
            while any(self._active_requests.get(key, 0) for key in blocked_keys):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._hard_breaker_holders -= 1
                    self._condition.notify_all()
                    raise ExternalServiceError(
                        "공유 메모리 번역 요청이 끝나지 않아 전사를 차단했습니다."
                    )
                self._condition.wait(timeout=min(1.0, remaining))
        try:
            unloaded = self._unload_ollama_models()
        except BaseException:
            self.release_stt_hard_breaker()
            raise
        return {
            "enabled": True,
            "hosts": list(self.defaults.stt_hard_breaker_hosts),
            "unloaded_models": unloaded,
        }

    def release_stt_hard_breaker(self) -> None:
        with self._condition:
            if self._hard_breaker_holders > 0:
                self._hard_breaker_holders -= 1
            self._condition.notify_all()

    def _public_server(
        self,
        stage: str,
        server: TranslationServer,
    ) -> dict[str, Any]:
        key = (stage, server.id)
        with self._lock:
            current = dict(self._health.get(key, {}))
            running = self._active_requests.get(key, 0)
            review_priority = (
                stage == "draft"
                and self._review_priority_active_locked(server)
            )
        hard_blocked = (
            bool(server.base_url)
            and self._uses_shared_stt_memory(server)
            and self.hard_breaker_active()
        )
        if hard_blocked:
            status = "suspended"
            current["message"] = HARD_BREAKER_MESSAGE
        elif review_priority:
            status = "suspended"
            current["message"] = REVIEW_PRIORITY_MESSAGE
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
            "selected_model": server.selected_model,
            "models": list(server.models),
            "status": status,
            "message": current.get("message"),
            "checked_at": server.checked_at,
            "running_jobs": running,
            "available_slots": (
                0
                if hard_blocked or review_priority
                else max(0, server.capacity - running)
            ),
        }

    def group(self, stage: str) -> dict[str, Any]:
        resolved = translation_stage(stage)
        return {
            "stage": resolved,
            "label": TRANSLATION_STAGE_LABELS[resolved],
            "servers": [
                self._public_server(resolved, server)
                for server in self.stores[resolved].list()
            ],
        }

    def groups(self) -> list[dict[str, Any]]:
        return [self.group(stage) for stage in TRANSLATION_STAGES]

    def update_server_model(
        self,
        stage: str,
        server_id: str,
        model: str,
    ) -> dict[str, Any]:
        resolved = translation_stage(stage)
        server = self.stores[resolved].set_selected_model(server_id, model)
        return self._public_server(resolved, server)

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

    def _configured_candidates(
        self,
        stage: str,
        mode: str,
    ) -> list[TranslationServer]:
        resolved = translation_stage(stage)
        if mode not in {"live", "batch"}:
            raise ValueError("번역 실행 모드는 live 또는 batch여야 합니다.")
        return [
            server
            for server in self.stores[resolved].list()
            if server.enabled
            and server.base_url
            and server.selected_model
            and (
                not server.models
                or server.selected_model in server.models
            )
            and (mode != "batch" or server.batch_preferred)
        ]

    def _candidates(self, stage: str, mode: str) -> list[TranslationServer]:
        resolved = translation_stage(stage)
        hard_breaker_active = self.hard_breaker_active()
        candidates = [
            server
            for server in self._configured_candidates(resolved, mode)
            if not (
                hard_breaker_active
                and self._uses_shared_stt_memory(server)
            )
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
        return bool(self._configured_candidates(stage, mode))

    def has_routable_server(
        self,
        stage: str = "draft",
        mode: str = "live",
    ) -> bool:
        candidates = self._candidates(stage, mode)
        if stage != "draft":
            return bool(candidates)
        with self._lock:
            return any(
                not self._review_priority_active_locked(server)
                for server in candidates
            )

    def route_suspended_by_stt(
        self,
        stage: str = "draft",
        mode: str = "live",
    ) -> bool:
        configured = self._configured_candidates(stage, mode)
        return bool(
            configured
            and self.hard_breaker_active()
            and all(
                self._uses_shared_stt_memory(server)
                for server in configured
            )
        )

    def worker_limit(self, mode: str) -> int:
        """Return the usable draft concurrency for one translation job."""
        candidates = self._candidates("draft", mode)
        with self._lock:
            usable = [
                server
                for server in candidates
                if not self._review_priority_active_locked(server)
            ]
        return max(1, min(8, sum(server.capacity for server in usable)))

    @contextmanager
    def review_priority(self, mode: str) -> Iterator[None]:
        """Reserve review hosts for one two-pass translation run.

        A run-level reservation closes the gaps between individual review
        requests. This prevents a draft model from being loaded again while
        concurrent logical batches are moving through the review pass.
        """

        review_servers = self._configured_candidates("review", mode)
        if not review_servers:
            raise ExternalServiceError(
                f"{TRANSLATION_STAGE_LABELS['review']} 서버가 설정되지 않았습니다."
            )
        hosts = tuple(sorted({
            self._server_host(server)
            for server in review_servers
        }))
        deadline = time.monotonic() + self.defaults.read_timeout_seconds
        with self._condition:
            for host in hosts:
                self._review_host_holders[host] = (
                    self._review_host_holders.get(host, 0) + 1
                )
            try:
                while any(
                    self._active_requests_on_host_locked("draft", host) > 0
                    for host in hosts
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ExternalServiceError(
                            "같은 서버의 1차 번역이 끝나지 않아 2차 검수 "
                            "우선 경로를 시작할 수 없습니다."
                        )
                    self._condition.wait(timeout=min(1.0, remaining))
            except BaseException:
                self._release_review_priority_hosts_locked(hosts)
                raise
        try:
            yield
        finally:
            with self._condition:
                self._release_review_priority_hosts_locked(hosts)

    def _release_review_priority_hosts_locked(
        self,
        hosts: tuple[str, ...],
    ) -> None:
        for host in hosts:
            holders = max(0, self._review_host_holders.get(host, 1) - 1)
            if holders:
                self._review_host_holders[host] = holders
            else:
                self._review_host_holders.pop(host, None)
        self._condition.notify_all()

    def model_contract(self) -> str:
        return "+".join(
            f"{stage}:" + ",".join(
                f"{server.id}={server.selected_model}"
                for server in self.stores[stage].list()
                if server.selected_model
            )
            for stage in TRANSLATION_STAGES
        )

    def endpoint_contract(self, mode: str) -> str:
        server_ids = [
            f"{stage}:"
            + ",".join(
                server.id
                for server in self._configured_candidates(stage, mode)
            )
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

    def _reserve_request_slot(
        self,
        stage: str,
        server: TranslationServer,
        *,
        deadline: float,
    ) -> bool:
        """Reserve one server slot while giving same-host review priority."""

        key = (stage, server.id)
        host = self._server_host(server)
        with self._condition:
            if (
                self._hard_breaker_holders > 0
                and self._uses_shared_stt_memory(server)
            ):
                return False
            running = self._active_requests.get(key, 0)
            if running >= server.capacity:
                return False
            if stage == "draft" and self._review_host_holders.get(host, 0):
                return False

            self._active_requests[key] = running + 1
            if stage != "review":
                return True

            # Reserving the host before waiting prevents new draft calls from
            # racing in while existing draft calls drain. Reviews may share a
            # host with other reviews up to their configured stage capacity.
            self._review_host_holders[host] = (
                self._review_host_holders.get(host, 0) + 1
            )
            while self._active_requests_on_host_locked("draft", host) > 0:
                if (
                    self._hard_breaker_holders > 0
                    and self._uses_shared_stt_memory(server)
                ):
                    self._release_request_slot_locked(stage, server)
                    raise TranslationDeferred(HARD_BREAKER_MESSAGE)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._release_request_slot_locked(stage, server)
                    raise ExternalServiceError(
                        "같은 서버의 1차 번역이 끝나지 않아 2차 검수를 "
                        "시작할 수 없습니다."
                    )
                self._condition.wait(timeout=min(1.0, remaining))
            return True

    def _release_request_slot_locked(
        self,
        stage: str,
        server: TranslationServer,
    ) -> None:
        key = (stage, server.id)
        self._active_requests[key] = max(
            0,
            self._active_requests.get(key, 1) - 1,
        )
        if stage == "review":
            host = self._server_host(server)
            holders = max(0, self._review_host_holders.get(host, 1) - 1)
            if holders:
                self._review_host_holders[host] = holders
            else:
                self._review_host_holders.pop(host, None)
        self._condition.notify_all()

    def _release_request_slot(
        self,
        stage: str,
        server: TranslationServer,
    ) -> None:
        with self._condition:
            self._release_request_slot_locked(stage, server)

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
        configured = self._configured_candidates(resolved, mode)
        if not configured:
            raise ExternalServiceError(
                f"{TRANSLATION_STAGE_LABELS[resolved]} 서버가 설정되지 않았습니다."
            )
        last_response: requests.Response | None = None
        attempted_ids: set[str] = set()
        deadline = time.monotonic() + self.defaults.read_timeout_seconds
        while len(attempted_ids) < len(configured):
            candidates = [
                server
                for server in self._candidates(resolved, mode)
                if server.id not in attempted_ids
            ]
            if not candidates:
                if (
                    not attempted_ids
                    and self.hard_breaker_active()
                    and all(
                        self._uses_shared_stt_memory(server)
                        for server in configured
                    )
                ):
                    raise TranslationDeferred(HARD_BREAKER_MESSAGE)
                break
            server: TranslationServer | None = None
            for candidate in candidates:
                if self._reserve_request_slot(
                    resolved,
                    candidate,
                    deadline=deadline,
                ):
                    server = candidate
                    break
            if server is None:
                if (
                    all(
                        self._uses_shared_stt_memory(candidate)
                        for candidate in candidates
                    )
                    and self.hard_breaker_active()
                ):
                    raise TranslationDeferred(HARD_BREAKER_MESSAGE)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ExternalServiceError(
                        "모든 번역 서버의 동시 요청 수가 가득 찼습니다."
                    )
                with self._condition:
                    self._condition.wait(timeout=min(1.0, remaining))
                continue

            attempted_ids.add(server.id)
            key = (resolved, server.id)
            client = RetryingJSONClient(
                token=server.token,
                read_timeout=self.defaults.read_timeout_seconds,
                attempts=(
                    REVIEW_REQUEST_ATTEMPTS
                    if resolved == "review"
                    else 1
                ),
                request_limiter=request_limiter,
                service_name=f"translation_{resolved}",
                request_observer=request_observer,
            )
            upstream_payload = {
                key: value
                for key, value in payload.items()
                if key in OPENAI_COMPLETION_FIELDS
            }
            upstream_payload["model"] = server.selected_model
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
                self._release_request_slot(resolved, server)
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
        raise ExternalServiceError("번역 서버에 연결할 수 없습니다.")
