"""Independent draft/review translation server groups and routing boundary."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import requests

from . import __version__
from .time_display import configure_kst_logging
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


def _normalize_url(value: str, setting: str) -> str:
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


def _stage(value: str) -> str:
    if value not in TRANSLATION_STAGES:
        raise ValueError("지원하지 않는 번역 단계입니다.")
    return value


@dataclass(frozen=True)
class TranslationRouterSettings:
    """Deployment-owned defaults; each stage topology is stored independently."""

    state_dir: Path
    api_token: str = ""
    builtin_name: str = "기본 Runtime"
    builtin_base_url: str = ""
    builtin_token: str = ""
    builtin_capacity: int = 1
    builtin_draft_enabled: bool = True
    builtin_review_enabled: bool = False
    builtin_draft_model: str = DEFAULT_DRAFT_MODEL
    builtin_review_model: str = DEFAULT_REVIEW_MODEL
    builtin_draft_batch_preferred: bool = False
    builtin_review_batch_preferred: bool = False
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 600.0

    @classmethod
    def from_env(cls) -> TranslationRouterSettings:
        enabled = lambda name, default: os.environ.get(
            name,
            default,
        ).strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            state_dir=Path(
                os.environ.get(
                    "TRANSLATION_STATE_DIR",
                    "/var/lib/stt-translation",
                )
            ).expanduser(),
            api_token=os.environ.get("TRANSLATION_API_TOKEN", ""),
            builtin_name=os.environ.get(
                "TRANSLATION_BUILTIN_NAME",
                "기본 Runtime",
            ).strip(),
            builtin_base_url=os.environ.get(
                "TRANSLATION_BUILTIN_BASE_URL",
                "",
            ).strip(),
            builtin_token=os.environ.get("TRANSLATION_BUILTIN_TOKEN", ""),
            builtin_capacity=int(
                os.environ.get("TRANSLATION_BUILTIN_CAPACITY", "1")
            ),
            builtin_draft_enabled=enabled(
                "TRANSLATION_BUILTIN_DRAFT_ENABLED",
                "true",
            ),
            builtin_review_enabled=enabled(
                "TRANSLATION_BUILTIN_REVIEW_ENABLED",
                "false",
            ),
            builtin_draft_model=os.environ.get(
                "TRANSLATION_BUILTIN_DRAFT_MODEL",
                DEFAULT_DRAFT_MODEL,
            ).strip(),
            builtin_review_model=os.environ.get(
                "TRANSLATION_BUILTIN_REVIEW_MODEL",
                DEFAULT_REVIEW_MODEL,
            ).strip(),
            builtin_draft_batch_preferred=enabled(
                "TRANSLATION_BUILTIN_DRAFT_BATCH_PREFERRED",
                os.environ.get("TRANSLATION_BUILTIN_BATCH_PREFERRED", "false"),
            ),
            builtin_review_batch_preferred=enabled(
                "TRANSLATION_BUILTIN_REVIEW_BATCH_PREFERRED",
                "false",
            ),
            connect_timeout_seconds=float(
                os.environ.get("TRANSLATION_CONNECT_TIMEOUT_SECONDS", "10")
            ),
            read_timeout_seconds=float(
                os.environ.get("TRANSLATION_READ_TIMEOUT_SECONDS", "600")
            ),
        ).normalized()

    def normalized(self) -> TranslationRouterSettings:
        if not 1 <= self.builtin_capacity <= 8:
            raise ValueError("TRANSLATION_BUILTIN_CAPACITY must be 1..8")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("translation timeouts must be positive")
        if not self.builtin_name:
            raise ValueError("TRANSLATION_BUILTIN_NAME is required")
        if not self.builtin_draft_model or not self.builtin_review_model:
            raise ValueError("translation stage models are required")
        return TranslationRouterSettings(
            state_dir=self.state_dir,
            api_token=self.api_token,
            builtin_name=self.builtin_name,
            builtin_base_url=(
                _normalize_url(
                    self.builtin_base_url,
                    "TRANSLATION_BUILTIN_BASE_URL",
                )
                if self.builtin_base_url
                else ""
            ),
            builtin_token=self.builtin_token,
            builtin_capacity=self.builtin_capacity,
            builtin_draft_enabled=self.builtin_draft_enabled,
            builtin_review_enabled=self.builtin_review_enabled,
            builtin_draft_model=self.builtin_draft_model,
            builtin_review_model=self.builtin_review_model,
            builtin_draft_batch_preferred=self.builtin_draft_batch_preferred,
            builtin_review_batch_preferred=self.builtin_review_batch_preferred,
            connect_timeout_seconds=self.connect_timeout_seconds,
            read_timeout_seconds=self.read_timeout_seconds,
        )

    @property
    def legacy_database_path(self) -> Path:
        return self.state_dir / "translation-endpoints.sqlite3"

    def group_database_path(self, stage: str) -> Path:
        return self.state_dir / f"translation-{_stage(stage)}-servers.sqlite3"


class ServerCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    token: str = ""
    enabled: bool = True
    capacity: int = Field(default=1, ge=1, le=8)


class ServerUpdatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    token: str | None = None
    clear_token: bool = False
    enabled: bool = True
    capacity: int = Field(default=1, ge=1, le=8)


class ServerRoutingPayload(BaseModel):
    enabled: bool = True
    batch_preferred: bool = False


class GroupModelPayload(BaseModel):
    model: str = Field(min_length=1)


def _authorization_matches(request: Request, expected_token: str) -> bool:
    if not expected_token:
        return True
    return request.headers.get("authorization", "") == f"Bearer {expected_token}"


def _upstream_headers(server: TranslationServer) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if server.token:
        headers["Authorization"] = f"Bearer {server.token}"
    return headers


def _error_response(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {
            "error": {
                "message": message,
                "type": "translation_router_error",
            }
        },
        status_code=status_code,
    )


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


def create_translation_app(
    settings: TranslationRouterSettings | None = None,
    stores: Mapping[str, TranslationServerGroupStore] | None = None,
) -> FastAPI:
    configured = (settings or TranslationRouterSettings.from_env()).normalized()
    group_stores = dict(stores or {
        stage: TranslationServerGroupStore(configured.group_database_path(stage))
        for stage in TRANSLATION_STAGES
    })
    if set(group_stores) != set(TRANSLATION_STAGES):
        raise ValueError("draft and review translation stores are required")
    migrate_legacy_translation_endpoints(
        configured.legacy_database_path,
        draft_store=group_stores["draft"],
        review_store=group_stores["review"],
    )
    group_stores["draft"].ensure_model(configured.builtin_draft_model)
    group_stores["review"].ensure_model(configured.builtin_review_model)
    group_stores["draft"].sync_builtin(
        name=configured.builtin_name,
        base_url=configured.builtin_base_url,
        token=configured.builtin_token,
        enabled=configured.builtin_draft_enabled,
        capacity=configured.builtin_capacity,
        batch_preferred=configured.builtin_draft_batch_preferred,
    )
    group_stores["review"].sync_builtin(
        name=configured.builtin_name,
        base_url=configured.builtin_base_url,
        token=configured.builtin_token,
        enabled=configured.builtin_review_enabled,
        capacity=configured.builtin_capacity,
        batch_preferred=configured.builtin_review_batch_preferred,
    )

    health_lock = threading.Lock()
    health: dict[tuple[str, str], dict[str, Any]] = {}
    active_requests: dict[tuple[str, str], int] = {}
    timeout = (
        configured.connect_timeout_seconds,
        configured.read_timeout_seconds,
    )
    app = FastAPI(
        title="stt-to-subtitle translation router",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )

    def require_auth(request: Request) -> Response | None:
        if _authorization_matches(request, configured.api_token):
            return None
        return _error_response("authentication required", 401)

    def public_server(stage: str, server: TranslationServer) -> dict[str, Any]:
        key = (stage, server.id)
        with health_lock:
            current = dict(health.get(key, {}))
            running = active_requests.get(key, 0)
        if not server.enabled:
            status = "disabled"
        elif not server.base_url:
            status = "unconfigured"
        else:
            status = str(
                current.get(
                    "status",
                    "ready" if server.models else "unknown",
                )
            )
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

    def public_group(stage: str) -> dict[str, Any]:
        store = group_stores[stage]
        servers = [public_server(stage, item) for item in store.list()]
        return {
            "stage": stage,
            "label": TRANSLATION_STAGE_LABELS[stage],
            "model": store.model(),
            "servers": servers,
        }

    def probe(stage: str, server: TranslationServer) -> TranslationServer:
        key = (stage, server.id)
        if not server.enabled:
            with health_lock:
                health[key] = {"status": "disabled", "message": None}
            return server
        if not server.base_url:
            with health_lock:
                health[key] = {
                    "status": "unconfigured",
                    "message": "번역 서버 주소가 설정되지 않았습니다.",
                }
            raise RuntimeError("번역 서버 주소가 설정되지 않았습니다.")
        try:
            response = requests.get(
                f"{server.base_url}/models",
                headers=_upstream_headers(server),
                timeout=(configured.connect_timeout_seconds, 30.0),
            )
            models = _model_ids(response) if response.status_code == 200 else []
            if response.status_code != 200 or not models:
                raise RuntimeError("모델 목록을 확인할 수 없습니다.")
            server = group_stores[stage].save_models(server.id, models)
        except (requests.RequestException, RuntimeError):
            with health_lock:
                health[key] = {
                    "status": "unavailable",
                    "message": "번역 서버에서 모델 목록을 조회할 수 없습니다.",
                }
            raise
        with health_lock:
            health[key] = {"status": "ready", "message": None}
        return server

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        ready = False
        for server in group_stores["draft"].list():
            if not server.enabled or not server.base_url:
                continue
            try:
                probe("draft", server)
            except (requests.RequestException, RuntimeError):
                continue
            ready = True
        return JSONResponse(
            {"status": "ready" if ready else "not_ready"},
            status_code=200 if ready else 503,
        )

    @app.get("/v1/models")
    def models(request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": "translation-router",
                        "object": "model",
                        "owned_by": "stt-to-subtitle",
                    }
                ],
            }
        )

    @app.get("/v1/router/groups")
    def list_groups(request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        return JSONResponse(
            {"items": [public_group(stage) for stage in TRANSLATION_STAGES]}
        )

    @app.get("/v1/router/groups/{stage}")
    def get_group(stage: str, request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            resolved = _stage(stage)
        except ValueError as error:
            return _error_response(str(error), 404)
        return JSONResponse(public_group(resolved))

    @app.put("/v1/router/groups/{stage}/model")
    def update_group_model(
        stage: str,
        payload: GroupModelPayload,
        request: Request,
    ) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            resolved = _stage(stage)
            model = payload.model.strip()
            available = {
                item
                for server in group_stores[resolved].list()
                for item in server.models
            }
            if available and model not in available:
                raise ValueError("등록된 서버가 제공하지 않는 모델입니다.")
            group_stores[resolved].set_model(model)
        except ValueError as error:
            return _error_response(str(error), 400)
        return JSONResponse(public_group(resolved))

    @app.post("/v1/router/groups/{stage}/servers", status_code=201)
    def create_server(
        stage: str,
        payload: ServerCreatePayload,
        request: Request,
    ) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            resolved = _stage(stage)
            server = group_stores[resolved].create(
                name=payload.name.strip(),
                base_url=_normalize_url(payload.base_url, "base_url"),
                token=payload.token,
                enabled=payload.enabled,
                capacity=payload.capacity,
            )
        except (ValueError, sqlite3.IntegrityError) as error:
            return _error_response(str(error), 400)
        if server.enabled:
            try:
                server = probe(resolved, server)
            except (requests.RequestException, RuntimeError):
                pass
        return JSONResponse(public_server(resolved, server), status_code=201)

    @app.put("/v1/router/groups/{stage}/servers/{server_id}")
    def update_server(
        stage: str,
        server_id: str,
        payload: ServerUpdatePayload,
        request: Request,
    ) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            resolved = _stage(stage)
        except ValueError as error:
            return _error_response(str(error), 404)
        current = group_stores[resolved].get(server_id)
        if current is None or current.builtin:
            return _error_response("추가 번역 서버를 찾을 수 없습니다.", 404)
        token = "" if payload.clear_token else (
            payload.token if payload.token is not None else current.token
        )
        try:
            server = group_stores[resolved].update(
                server_id,
                name=payload.name.strip(),
                base_url=_normalize_url(payload.base_url, "base_url"),
                token=token,
                enabled=payload.enabled,
                capacity=payload.capacity,
            )
        except (ValueError, sqlite3.IntegrityError) as error:
            return _error_response(str(error), 400)
        with health_lock:
            health.pop((resolved, server_id), None)
        if server.enabled:
            try:
                server = probe(resolved, server)
            except (requests.RequestException, RuntimeError):
                pass
        return JSONResponse(public_server(resolved, server))

    @app.delete("/v1/router/groups/{stage}/servers/{server_id}", status_code=204)
    def delete_server(stage: str, server_id: str, request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            resolved = _stage(stage)
        except ValueError as error:
            return _error_response(str(error), 404)
        key = (resolved, server_id)
        with health_lock:
            if active_requests.get(key, 0):
                return _error_response("진행 중인 번역 요청이 있습니다.", 409)
        if not group_stores[resolved].delete(server_id):
            return _error_response("추가 번역 서버를 찾을 수 없습니다.", 404)
        with health_lock:
            health.pop(key, None)
        return Response(status_code=204)

    @app.post("/v1/router/groups/{stage}/servers/{server_id}/probe")
    def probe_server(stage: str, server_id: str, request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            resolved = _stage(stage)
        except ValueError as error:
            return _error_response(str(error), 404)
        server = group_stores[resolved].get(server_id)
        if server is None:
            return _error_response("번역 서버를 찾을 수 없습니다.", 404)
        try:
            server = probe(resolved, server)
        except (requests.RequestException, RuntimeError):
            return _error_response(
                "번역 서버에서 모델 목록을 조회할 수 없습니다.",
                502,
            )
        return JSONResponse(public_server(resolved, server))

    @app.put("/v1/router/groups/{stage}/servers/{server_id}/routing")
    def update_server_routing(
        stage: str,
        server_id: str,
        payload: ServerRoutingPayload,
        request: Request,
    ) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            resolved = _stage(stage)
            server = group_stores[resolved].set_routing(
                server_id,
                enabled=payload.enabled,
                batch_preferred=payload.batch_preferred,
            )
        except ValueError as error:
            return _error_response(str(error), 404)
        return JSONResponse(public_server(resolved, server))

    def route_candidates(stage: str, translation_mode: str) -> list[TranslationServer]:
        store = group_stores[stage]
        model = store.model()
        eligible = [
            server
            for server in store.list()
            if server.enabled
            and server.base_url
            and (not server.models or model in server.models)
        ]
        if translation_mode == "batch":
            eligible = [server for server in eligible if server.batch_preferred]
        with health_lock:
            running = dict(active_requests)
            statuses = {
                key: str(value.get("status", "unknown"))
                for key, value in health.items()
            }
        eligible.sort(
            key=lambda server: (
                statuses.get((stage, server.id)) == "unavailable",
                running.get((stage, server.id), 0) >= server.capacity,
                running.get((stage, server.id), 0) / server.capacity,
                not server.builtin,
                server.created_at,
            )
        )
        return eligible

    @app.post("/v1/chat/completions")
    def chat_completions(
        payload: dict[str, Any],
        request: Request,
        translation_pass: str = Header("draft", alias="X-Translation-Pass"),
        translation_mode: str = Header("live", alias="X-Translation-Mode"),
    ) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            stage = _stage(translation_pass)
        except ValueError:
            return _error_response("unsupported translation pass", 400)
        if translation_mode not in {"live", "batch"}:
            return _error_response("unsupported translation mode", 400)
        candidates = route_candidates(stage, translation_mode)
        if not candidates:
            return _error_response(
                "no translation server is configured for this stage",
                503,
            )
        model = group_stores[stage].model()
        last_status: int | None = None
        attempted = False
        for server in candidates:
            key = (stage, server.id)
            with health_lock:
                running = active_requests.get(key, 0)
                if running >= server.capacity:
                    continue
                active_requests[key] = running + 1
            attempted = True
            upstream_payload = dict(payload)
            upstream_payload["model"] = model
            try:
                response = requests.post(
                    f"{server.base_url}/chat/completions",
                    headers=_upstream_headers(server),
                    json=upstream_payload,
                    timeout=timeout,
                )
            except requests.RequestException:
                with health_lock:
                    health[key] = {
                        "status": "unavailable",
                        "message": "번역 요청에 응답하지 않습니다.",
                    }
                LOGGER.warning(
                    "translation upstream request failed: stage=%s mode=%s server=%s",
                    stage,
                    translation_mode,
                    server.id,
                )
                continue
            finally:
                with health_lock:
                    active_requests[key] = max(
                        0,
                        active_requests.get(key, 1) - 1,
                    )
            last_status = response.status_code
            if 200 <= response.status_code < 300:
                with health_lock:
                    health[key] = {"status": "ready", "message": None}
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type="application/json",
                    headers={"X-Translation-Upstream": server.id},
                )
        if not attempted:
            return _error_response("all translation servers are busy", 503)
        if last_status is not None:
            return _error_response(
                "translation upstream rejected the request",
                last_status,
            )
        return _error_response("translation upstream is unavailable", 503)

    return app


def main(argv: Sequence[str] | None = None) -> None:
    import uvicorn

    parser = argparse.ArgumentParser(
        description="Run the independent translation server group router.",
    )
    parser.parse_args(argv)
    configure_kst_logging(os.environ.get("LOG_LEVEL", "INFO").upper())
    uvicorn.run(
        "stt_to_subtitle.translation_api:create_translation_app",
        host=os.environ.get("TRANSLATION_HOST", "0.0.0.0"),
        port=int(os.environ.get("TRANSLATION_PORT", "8200")),
        factory=True,
        workers=1,
    )


if __name__ == "__main__":
    main()
