"""Translation endpoint registry and OpenAI-compatible routing boundary."""

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
from .translation_store import TranslationEndpoint, TranslationEndpointStore


LOGGER = logging.getLogger(__name__)
DEFAULT_DRAFT_MODEL = (
    "gemma-4-12b-coder-fable5-composer2.5-v1-uncensored-heretic"
)


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


@dataclass(frozen=True)
class TranslationRouterSettings:
    """Service-local settings; endpoint topology lives in its own registry."""

    state_dir: Path
    api_token: str = ""
    builtin_name: str = "기본 번역 서버"
    builtin_base_url: str = ""
    builtin_token: str = ""
    builtin_capacity: int = 1
    builtin_draft_model: str = DEFAULT_DRAFT_MODEL
    builtin_review_model: str = ""
    builtin_review_enabled: bool = False
    builtin_batch_preferred: bool = False
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 600.0

    @classmethod
    def from_env(cls) -> TranslationRouterSettings:
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
                "기본 번역 서버",
            ).strip(),
            builtin_base_url=os.environ.get(
                "TRANSLATION_BUILTIN_BASE_URL",
                "",
            ).strip(),
            builtin_token=os.environ.get("TRANSLATION_BUILTIN_TOKEN", ""),
            builtin_capacity=int(
                os.environ.get("TRANSLATION_BUILTIN_CAPACITY", "1")
            ),
            builtin_draft_model=os.environ.get(
                "TRANSLATION_BUILTIN_DRAFT_MODEL",
                DEFAULT_DRAFT_MODEL,
            ).strip(),
            builtin_review_model=os.environ.get(
                "TRANSLATION_BUILTIN_REVIEW_MODEL",
                "",
            ).strip(),
            builtin_review_enabled=os.environ.get(
                "TRANSLATION_BUILTIN_REVIEW_ENABLED",
                "false",
            ).strip().lower() in {"1", "true", "yes", "on"},
            builtin_batch_preferred=os.environ.get(
                "TRANSLATION_BUILTIN_BATCH_PREFERRED",
                "false",
            ).strip().lower() in {"1", "true", "yes", "on"},
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
        if self.builtin_base_url and not self.builtin_name:
            raise ValueError("TRANSLATION_BUILTIN_NAME is required")
        if self.builtin_review_enabled and not self.builtin_review_model:
            raise ValueError(
                "TRANSLATION_BUILTIN_REVIEW_MODEL is required when review is enabled"
            )
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
            builtin_draft_model=self.builtin_draft_model,
            builtin_review_model=self.builtin_review_model,
            builtin_review_enabled=self.builtin_review_enabled,
            builtin_batch_preferred=self.builtin_batch_preferred,
            connect_timeout_seconds=self.connect_timeout_seconds,
            read_timeout_seconds=self.read_timeout_seconds,
        )

    @property
    def database_path(self) -> Path:
        return self.state_dir / "translation-endpoints.sqlite3"


class EndpointCreatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    token: str = ""
    enabled: bool = True
    capacity: int = Field(default=1, ge=1, le=8)


class EndpointUpdatePayload(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    base_url: str
    token: str | None = None
    clear_token: bool = False
    enabled: bool = True
    capacity: int = Field(default=1, ge=1, le=8)


class EndpointRoutingPayload(BaseModel):
    draft_model: str = ""
    review_model: str = ""
    review_enabled: bool = False
    batch_preferred: bool = False


def _authorization_matches(request: Request, expected_token: str) -> bool:
    if not expected_token:
        return True
    return request.headers.get("authorization", "") == f"Bearer {expected_token}"


def _upstream_headers(endpoint: TranslationEndpoint) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if endpoint.token:
        headers["Authorization"] = f"Bearer {endpoint.token}"
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
    store: TranslationEndpointStore | None = None,
) -> FastAPI:
    configured = (settings or TranslationRouterSettings.from_env()).normalized()
    endpoint_store = store or TranslationEndpointStore(configured.database_path)
    if configured.builtin_base_url:
        endpoint_store.sync_builtin(
            name=configured.builtin_name,
            base_url=configured.builtin_base_url,
            token=configured.builtin_token,
            capacity=configured.builtin_capacity,
            draft_model=configured.builtin_draft_model,
            review_model=configured.builtin_review_model,
            review_enabled=configured.builtin_review_enabled,
            batch_preferred=configured.builtin_batch_preferred,
        )
    else:
        endpoint_store.remove_builtin()

    health_lock = threading.Lock()
    health: dict[str, dict[str, Any]] = {}
    active_requests: dict[str, int] = {}
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

    def public_endpoint(endpoint: TranslationEndpoint) -> dict[str, Any]:
        with health_lock:
            current = dict(health.get(endpoint.id, {}))
            running = active_requests.get(endpoint.id, 0)
        status = (
            "disabled"
            if not endpoint.enabled
            else str(
                current.get(
                    "status",
                    "ready" if endpoint.models else "unknown",
                )
            )
        )
        return {
            "id": endpoint.id,
            "name": endpoint.name,
            "base_url": endpoint.base_url,
            "token_configured": bool(endpoint.token),
            "enabled": endpoint.enabled,
            "capacity": endpoint.capacity,
            "builtin": endpoint.builtin,
            "draft_model": endpoint.draft_model,
            "review_model": endpoint.review_model,
            "review_enabled": endpoint.review_enabled,
            "batch_preferred": endpoint.batch_preferred,
            "models": list(endpoint.models),
            "status": status,
            "message": current.get("message"),
            "checked_at": endpoint.checked_at,
            "running_jobs": running,
            "available_slots": max(0, endpoint.capacity - running),
        }

    def probe(endpoint: TranslationEndpoint) -> TranslationEndpoint:
        if not endpoint.enabled:
            with health_lock:
                health[endpoint.id] = {
                    "status": "disabled",
                    "message": None,
                }
            return endpoint
        try:
            response = requests.get(
                f"{endpoint.base_url}/models",
                headers=_upstream_headers(endpoint),
                timeout=(configured.connect_timeout_seconds, 30.0),
            )
            models = _model_ids(response) if response.status_code == 200 else []
            if response.status_code != 200 or not models:
                raise RuntimeError("모델 목록을 확인할 수 없습니다.")
            endpoint = endpoint_store.save_models(endpoint.id, models)
        except (requests.RequestException, RuntimeError):
            with health_lock:
                health[endpoint.id] = {
                    "status": "unavailable",
                    "message": "번역 서버에서 모델 목록을 조회할 수 없습니다.",
                }
            raise
        with health_lock:
            health[endpoint.id] = {"status": "ready", "message": None}
        return endpoint

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz(request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        endpoints = [
            endpoint
            for endpoint in endpoint_store.list()
            if endpoint.enabled and endpoint.draft_model
        ]
        if not endpoints:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        ready = False
        for endpoint in endpoints:
            try:
                probe(endpoint)
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

    @app.get("/v1/router/endpoints")
    def list_endpoints(request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        items = [public_endpoint(endpoint) for endpoint in endpoint_store.list()]
        return JSONResponse({"items": items, "total": len(items)})

    @app.post("/v1/router/endpoints", status_code=201)
    def create_endpoint(
        payload: EndpointCreatePayload,
        request: Request,
    ) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            endpoint = endpoint_store.create(
                name=payload.name.strip(),
                base_url=_normalize_url(payload.base_url, "base_url"),
                token=payload.token,
                enabled=payload.enabled,
                capacity=payload.capacity,
            )
        except (ValueError, sqlite3.IntegrityError) as error:
            return _error_response(str(error), 400)
        if endpoint.enabled:
            try:
                endpoint = probe(endpoint)
            except (requests.RequestException, RuntimeError):
                pass
        return JSONResponse(public_endpoint(endpoint), status_code=201)

    @app.put("/v1/router/endpoints/{endpoint_id}")
    def update_endpoint(
        endpoint_id: str,
        payload: EndpointUpdatePayload,
        request: Request,
    ) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        current = endpoint_store.get(endpoint_id)
        if current is None or current.builtin:
            return _error_response("추가 번역 서버를 찾을 수 없습니다.", 404)
        token = "" if payload.clear_token else (
            payload.token if payload.token is not None else current.token
        )
        try:
            endpoint = endpoint_store.update(
                endpoint_id,
                name=payload.name.strip(),
                base_url=_normalize_url(payload.base_url, "base_url"),
                token=token,
                enabled=payload.enabled,
                capacity=payload.capacity,
            )
        except (ValueError, sqlite3.IntegrityError) as error:
            return _error_response(str(error), 400)
        with health_lock:
            health.pop(endpoint_id, None)
        if endpoint.enabled:
            try:
                endpoint = probe(endpoint)
            except (requests.RequestException, RuntimeError):
                pass
        return JSONResponse(public_endpoint(endpoint))

    @app.delete("/v1/router/endpoints/{endpoint_id}", status_code=204)
    def delete_endpoint(endpoint_id: str, request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        with health_lock:
            if active_requests.get(endpoint_id, 0):
                return _error_response("진행 중인 번역 요청이 있습니다.", 409)
        if not endpoint_store.delete(endpoint_id):
            return _error_response("추가 번역 서버를 찾을 수 없습니다.", 404)
        with health_lock:
            health.pop(endpoint_id, None)
        return Response(status_code=204)

    @app.post("/v1/router/endpoints/{endpoint_id}/probe")
    def probe_endpoint(endpoint_id: str, request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        endpoint = endpoint_store.get(endpoint_id)
        if endpoint is None:
            return _error_response("번역 서버를 찾을 수 없습니다.", 404)
        try:
            endpoint = probe(endpoint)
        except (requests.RequestException, RuntimeError):
            return _error_response(
                "번역 서버에서 모델 목록을 조회할 수 없습니다.",
                502,
            )
        return JSONResponse(public_endpoint(endpoint))

    @app.put("/v1/router/endpoints/{endpoint_id}/routing")
    def update_endpoint_routing(
        endpoint_id: str,
        payload: EndpointRoutingPayload,
        request: Request,
    ) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        endpoint = endpoint_store.get(endpoint_id)
        if endpoint is None:
            return _error_response("번역 서버를 찾을 수 없습니다.", 404)
        draft_model = payload.draft_model.strip()
        review_model = payload.review_model.strip()
        if payload.review_enabled and not review_model:
            return _error_response(
                "검증 번역을 사용하려면 모델을 선택해야 합니다.",
                400,
            )
        available_models = set(endpoint.models)
        changed_models = {
            model
            for model, previous in (
                (draft_model, endpoint.draft_model),
                (review_model, endpoint.review_model),
            )
            if model and model != previous
        }
        if available_models and not changed_models <= available_models:
            return _error_response(
                "서버가 제공하지 않는 모델은 선택할 수 없습니다.",
                400,
            )
        try:
            endpoint = endpoint_store.set_models(
                endpoint_id,
                draft_model=draft_model,
                review_model=review_model,
                review_enabled=payload.review_enabled,
                batch_preferred=payload.batch_preferred,
            )
        except ValueError as error:
            return _error_response(str(error), 404)
        return JSONResponse(public_endpoint(endpoint))

    def route_candidates(
        translation_pass: str,
        translation_mode: str,
    ) -> list[TranslationEndpoint]:
        endpoints = [
            endpoint
            for endpoint in endpoint_store.list()
            if endpoint.enabled
        ]
        if translation_pass == "review":
            eligible = [
                endpoint
                for endpoint in endpoints
                if endpoint.review_enabled and endpoint.review_model
            ]
        else:
            eligible = [
                endpoint for endpoint in endpoints if endpoint.draft_model
            ]
            if translation_mode == "batch":
                eligible = [
                    endpoint for endpoint in eligible if endpoint.batch_preferred
                ]
        with health_lock:
            running = dict(active_requests)
            statuses = {
                endpoint_id: str(value.get("status", "unknown"))
                for endpoint_id, value in health.items()
            }
        if translation_mode == "batch" or translation_pass == "review":
            eligible.sort(
                key=lambda endpoint: (
                    not endpoint.batch_preferred,
                    running.get(endpoint.id, 0) >= endpoint.capacity,
                    running.get(endpoint.id, 0) / endpoint.capacity,
                    not endpoint.builtin,
                    endpoint.created_at,
                )
            )
        else:
            eligible.sort(
                key=lambda endpoint: (
                    statuses.get(endpoint.id) == "unavailable",
                    running.get(endpoint.id, 0) >= endpoint.capacity,
                    running.get(endpoint.id, 0) / endpoint.capacity,
                    not endpoint.builtin,
                    endpoint.created_at,
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
        if translation_pass not in {"draft", "review"}:
            return _error_response("unsupported translation pass", 400)
        if translation_mode not in {"live", "batch"}:
            return _error_response("unsupported translation mode", 400)
        candidates = route_candidates(translation_pass, translation_mode)
        if not candidates:
            return _error_response(
                "no translation endpoint is configured for this pass",
                503,
            )
        last_status: int | None = None
        attempted = False
        for endpoint in candidates:
            with health_lock:
                running = active_requests.get(endpoint.id, 0)
                if running >= endpoint.capacity:
                    continue
                active_requests[endpoint.id] = running + 1
            attempted = True
            upstream_payload = dict(payload)
            upstream_payload["model"] = (
                endpoint.review_model
                if translation_pass == "review"
                else endpoint.draft_model
            )
            try:
                response = requests.post(
                    f"{endpoint.base_url}/chat/completions",
                    headers=_upstream_headers(endpoint),
                    json=upstream_payload,
                    timeout=timeout,
                )
            except requests.RequestException:
                with health_lock:
                    health[endpoint.id] = {
                        "status": "unavailable",
                        "message": "번역 요청에 응답하지 않습니다.",
                    }
                LOGGER.warning(
                    "translation upstream request failed: pass=%s mode=%s "
                    "endpoint=%s",
                    translation_pass,
                    translation_mode,
                    endpoint.id,
                )
                continue
            finally:
                with health_lock:
                    active_requests[endpoint.id] = max(
                        0,
                        active_requests.get(endpoint.id, 1) - 1,
                    )
            last_status = response.status_code
            if 200 <= response.status_code < 300:
                with health_lock:
                    health[endpoint.id] = {
                        "status": "ready",
                        "message": None,
                    }
                return Response(
                    content=response.content,
                    status_code=response.status_code,
                    media_type="application/json",
                    headers={"X-Translation-Upstream": endpoint.id},
                )
        if not attempted:
            return _error_response("all translation endpoints are busy", 503)
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
        description="Run the translation endpoint routing API.",
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
