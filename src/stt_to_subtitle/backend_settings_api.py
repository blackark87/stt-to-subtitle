"""Backend settings, retention, observability, and capability routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from .backend_common import (
    bad_request,
    jobs_payload,
    public_value,
    service_from_request,
)
from .backend_contracts import (
    ArtifactCleanupRequest,
    PathDisplayRuleRequest,
    PromptCategoryRequest,
    PromptCategoryStateRequest,
    RuntimeEndpointCreateRequest,
    RuntimeEndpointUpdateRequest,
    ServerSettingsUpdateRequest,
    SubtitleValidatorUpdateRequest,
    TranslationModelLookupRequest,
)
from .job_state import JobPhase, JobReason, JobState
from .gpu_monitoring import gpu_snapshot_payload
from .library_progress import summarize_library_progress
from .operational_metrics import prometheus_exposition
from .orchestrator import DEFAULT_ARTIFACT_CLEANUP_AGE_DAYS
from .service_clients import ExternalServiceError, list_openai_compatible_models
from .backend_config import (
    RemoteServerSettings,
    SubtitleValidatorSettings,
    normalize_server_url,
)


router = APIRouter(prefix="/api/v1")


@router.get("/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    monitor = getattr(request.app.state, "gpu_monitor", None)
    active_states = {state.value for state in JobState if state is not JobState.DONE}
    attention_states = {
        JobState.BLOCKED.value,
        JobState.FAILED.value,
        JobState.PAUSED.value,
        JobState.STOPPED.value,
    }
    return {
        "state_counts": {
            state.value: service.store.count_jobs(
                states={state.value},
                include_comparison_transcriptions=False,
            )
            for state in JobState
        },
        "phase_counts": {
            phase.value: service.store.count_jobs(
                phases={phase.value},
                include_comparison_transcriptions=False,
            )
            for phase in JobPhase
        },
        "recent_jobs": jobs_payload(
            service.store.list_jobs(
                limit=20,
                include_comparison_transcriptions=False,
            )
        ),
        "active_jobs": jobs_payload(
            service.store.list_jobs(
                states=active_states,
                limit=100,
                include_comparison_transcriptions=False,
            )
        ),
        "recent_completed": jobs_payload(
            service.store.list_jobs(
                states={JobState.DONE.value},
                limit=10,
                include_comparison_transcriptions=False,
            )
        ),
        "attention_jobs": jobs_payload(
            service.store.list_jobs(
                states=attention_states,
                limit=10,
                include_comparison_transcriptions=False,
            )
        ),
        "dependencies": {
            "transcription": service.stt_gate_state,
            "translation": service.translation_circuit_state,
        },
        "gpu": gpu_snapshot_payload(monitor.snapshot())
        if monitor is not None
        else None,
    }


@router.get("/dashboard/library-progress")
def dashboard_library_progress(request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    items = summarize_library_progress(
        service.library.actor_library_entries(),
        service.store.latest_jobs_by_source(),
    )
    return {"items": public_value(items), "total": len(items)}


@router.get("/capabilities")
def capabilities() -> dict[str, Any]:
    return {
        "api_version": "v1",
        "operations": ["extract", "transcribe", "translate", "full", "compare"],
        "transcription_backends": ["whisperjav", "kotoba", "whisperx", "hybrid"],
        "phases": [phase.value for phase in JobPhase],
        "states": [state.value for state in JobState],
        "reason_codes": [reason.value for reason in JobReason],
        "subtitle_validator_providers": [
            "openai_compatible",
            "openrouter",
            "bedrock",
        ],
    }


@router.get("/settings")
def settings(request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    return {
        "servers": service.remote_servers_view(),
        "runtimes": service.runtime_endpoints_view(),
        "subtitle_validator": service.subtitle_validator_view(),
        "path_display_rules": public_value(service.path_display_rules),
        "prompt_categories": public_value(service.all_prompt_categories()),
    }


@router.put("/settings/servers")
def update_servers(
    payload: ServerSettingsUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    service = service_from_request(request)
    current = service.remote_servers
    updated = RemoteServerSettings(
        stt_base_url=payload.stt_base_url,
        stt_token=(
            ""
            if payload.clear_stt_token
            else payload.stt_token
            if payload.stt_token is not None
            else current.stt_token
        ),
        lm_base_url=payload.lm_base_url,
        lm_token=(
            ""
            if payload.clear_lm_token
            else payload.lm_token
            if payload.lm_token is not None
            else current.lm_token
        ),
        lm_model=payload.lm_model,
        translation_workers=payload.translation_workers,
    )
    try:
        service.update_remote_servers(updated)
    except ValueError as error:
        raise bad_request(error) from error
    return service.remote_servers_view()


@router.get("/runtimes")
def runtime_endpoints(request: Request) -> dict[str, Any]:
    items = service_from_request(request).runtime_endpoints_view()
    return {"items": items, "total": len(items)}


@router.post("/runtimes", status_code=201)
def create_runtime_endpoint(
    payload: RuntimeEndpointCreateRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).create_runtime_endpoint(
            name=payload.name,
            base_url=payload.base_url,
            token=payload.token,
            enabled=payload.enabled,
            capacity=payload.capacity,
        )
    except ValueError as error:
        raise bad_request(error) from error


@router.put("/runtimes/{runtime_id}")
def update_runtime_endpoint(
    runtime_id: str,
    payload: RuntimeEndpointUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).update_runtime_endpoint(
            runtime_id,
            name=payload.name,
            base_url=payload.base_url,
            token=payload.token,
            clear_token=payload.clear_token,
            enabled=payload.enabled,
            capacity=payload.capacity,
        )
    except ValueError as error:
        raise bad_request(error) from error


@router.delete("/runtimes/{runtime_id}", status_code=204)
def delete_runtime_endpoint(runtime_id: str, request: Request) -> Response:
    try:
        service_from_request(request).delete_runtime_endpoint(runtime_id)
    except ValueError as error:
        raise bad_request(error) from error
    return Response(status_code=204)


@router.post("/runtimes/{runtime_id}/probe")
def probe_runtime_endpoint(
    runtime_id: str,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).probe_runtime_endpoint(runtime_id)
    except ValueError as error:
        raise bad_request(error) from error


@router.post("/dependencies/transcription/probe")
def probe_transcription(request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    try:
        resumed = service.activate_transcription_stt()
    except (ExternalServiceError, ValueError) as error:
        raise HTTPException(
            status_code=503,
            detail=service.sanitize_external_error(str(error)),
        ) from error
    return {"state": service.stt_gate_state, "retried": resumed}


@router.post("/settings/translation-models")
def translation_models(
    payload: TranslationModelLookupRequest,
    request: Request,
) -> dict[str, list[str]]:
    service = service_from_request(request)
    current = service.remote_servers
    token = (
        ""
        if payload.clear_lm_token
        else payload.lm_token
        if payload.lm_token is not None
        else current.lm_token
    )
    try:
        base_url = normalize_server_url(
            payload.lm_base_url,
            "OPENAI_COMPATIBLE_BASE_URL",
        )
        models = list_openai_compatible_models(
            base_url,
            token,
            request_observer=service.record_external_request,
        )
    except ValueError as error:
        raise bad_request(error) from error
    except ExternalServiceError as error:
        raise HTTPException(
            status_code=502,
            detail="번역 서버에서 모델 목록을 조회할 수 없습니다.",
        ) from error
    return {"models": models}


@router.put("/settings/subtitle-validator")
def update_subtitle_validator(
    payload: SubtitleValidatorUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    service = service_from_request(request)
    stored = service.store.get_subtitle_validator_settings() or {}
    token = (
        ""
        if payload.clear_token
        else payload.token
        if payload.token is not None
        else str(stored.get("token", ""))
        if stored.get("provider") == payload.provider
        else ""
    )
    try:
        service.update_subtitle_validator(
            SubtitleValidatorSettings(
                provider=payload.provider,
                base_url=payload.base_url,
                token=token,
                model=payload.model,
                region=payload.region,
            )
        )
    except ValueError as error:
        raise bad_request(error) from error
    return service.subtitle_validator_view()


@router.get("/settings/path-display-rules")
def path_display_rules(request: Request) -> dict[str, Any]:
    rules = service_from_request(request).path_display_rules
    return {"items": public_value(rules), "total": len(rules)}


@router.post("/settings/path-display-rules", status_code=201)
def create_path_display_rule(
    payload: PathDisplayRuleRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        rule = service_from_request(request).create_path_display_rule(
            source_pattern=payload.source_pattern,
            display_pattern=payload.display_pattern,
        )
    except ValueError as error:
        raise bad_request(error) from error
    return public_value(rule)


@router.put("/settings/path-display-rules/{rule_id}")
def update_path_display_rule(
    rule_id: str,
    payload: PathDisplayRuleRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        rule = service_from_request(request).update_path_display_rule(
            rule_id,
            source_pattern=payload.source_pattern,
            display_pattern=payload.display_pattern,
        )
    except ValueError as error:
        raise bad_request(error) from error
    return public_value(rule)


@router.delete("/settings/path-display-rules/{rule_id}", status_code=204)
def delete_path_display_rule(rule_id: str, request: Request) -> Response:
    try:
        service_from_request(request).delete_path_display_rule(rule_id)
    except ValueError as error:
        raise bad_request(error) from error
    return Response(status_code=204)


@router.get("/settings/prompt-categories")
def prompt_categories(request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    categories = service.all_prompt_categories()
    return {
        "items": [
            {
                **asdict(category),
                "revisions": public_value(
                    service.store.list_prompt_revisions(category.id)
                ),
            }
            for category in categories
        ],
        "total": len(categories),
    }


@router.post("/settings/prompt-categories", status_code=201)
def create_prompt_category(
    payload: PromptCategoryRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        category = service_from_request(request).store.create_prompt_category(
            name=payload.name,
            translation_prompt=payload.translation_prompt,
            review_prompt=payload.review_prompt,
        )
    except ValueError as error:
        raise bad_request(error) from error
    return public_value(category)


@router.put("/settings/prompt-categories/{category_id}")
def update_prompt_category(
    category_id: str,
    payload: PromptCategoryRequest,
    request: Request,
) -> dict[str, Any]:
    service = service_from_request(request)
    try:
        service.store.update_prompt_category(
            category_id,
            name=payload.name,
            translation_prompt=payload.translation_prompt,
            review_prompt=payload.review_prompt,
        )
    except ValueError as error:
        raise bad_request(error) from error
    category = service.store.get_prompt_category(category_id)
    return public_value(category)


@router.patch("/settings/prompt-categories/{category_id}")
def set_prompt_category_state(
    category_id: str,
    payload: PromptCategoryStateRequest,
    request: Request,
) -> dict[str, Any]:
    service = service_from_request(request)
    try:
        service.store.set_prompt_category_archived(
            category_id,
            archived=payload.archived,
        )
    except ValueError as error:
        raise bad_request(error) from error
    return public_value(service.store.get_prompt_category(category_id))


@router.get("/settings/artifacts/audit")
def artifact_audit(
    request: Request,
    minimum_age_days: int = DEFAULT_ARTIFACT_CLEANUP_AGE_DAYS,
) -> dict[str, Any]:
    if not 1 <= minimum_age_days <= 3650:
        raise HTTPException(status_code=400, detail="minimum_age_days must be 1..3650")
    return public_value(
        service_from_request(request).artifact_audit(
            minimum_age_days=minimum_age_days
        )
    )


@router.post("/settings/artifacts/cleanup")
def cleanup_artifacts(
    payload: ArtifactCleanupRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        result = service_from_request(request).cleanup_artifacts(
            minimum_age_days=payload.minimum_age_days,
            expected_token=payload.cleanup_token,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return public_value(result)


@router.get("/operations/metrics")
def operation_metrics(request: Request) -> dict[str, Any]:
    return service_from_request(request).store.operational_metrics()


@router.get("/operations/metrics/prometheus")
def operation_metrics_prometheus(request: Request) -> Response:
    snapshot = service_from_request(request).store.operational_metrics()
    return Response(
        prometheus_exposition(snapshot),
        media_type="text/plain; version=0.0.4",
    )


@router.get("/operations/gpu")
def gpu_metrics(request: Request) -> dict[str, Any]:
    monitor = getattr(request.app.state, "gpu_monitor", None)
    if monitor is None:
        raise HTTPException(status_code=503, detail="GPU monitor is not initialized")
    return gpu_snapshot_payload(monitor.snapshot())


@router.get("/operations/database-integrity")
def database_integrity(request: Request) -> dict[str, Any]:
    return public_value(service_from_request(request).store.database_integrity())
