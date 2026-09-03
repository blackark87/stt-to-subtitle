"""Backend settings, retention, observability, and capability routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping, Sequence

from fastapi import APIRouter, HTTPException, Query, Request, Response

from .backend_common import (
    bad_request,
    jobs_payload,
    public_value,
    service_from_request,
)
from .backend_contracts import (
    ArtifactCleanupRequest,
    ExternalModelProfileRequest,
    ExternalModelSelectionRequest,
    PathDisplayRuleRequest,
    PromptCategoryRequest,
    PromptCategoryStateRequest,
    PromptDraftCreateRequest,
    PromptImprovementCreateRequest,
    ServerSettingsUpdateRequest,
    SubtitleValidatorUpdateRequest,
    TranslationEndpointCreateRequest,
    TranslationEndpointRoutingRequest,
    TranslationEndpointUpdateRequest,
    TranslationFeedbackStateRequest,
    TranslationServerModelRequest,
    TranscriberEndpointCreateRequest,
    TranscriberEndpointUpdateRequest,
)
from .job_state import JobPhase, JobReason, JobState
from .job_store import PipelineJob
from .gpu_monitoring import gpu_snapshot_payload
from .library_progress import summarize_library_progress
from .operational_metrics import prometheus_exposition
from .orchestrator import DEFAULT_ARTIFACT_CLEANUP_AGE_DAYS, SubtitleOrchestrator
from .service_clients import ExternalServiceError
from .time_display import format_kst_iso
from .backend_config import (
    RemoteServerSettings,
    SubtitleValidatorSettings,
)


router = APIRouter(prefix="/api/v1")


def _completed_jobs_payload(
    service: SubtitleOrchestrator,
    jobs: Sequence[PipelineJob],
) -> list[dict[str, Any]]:
    summaries = service.store.job_completion_summaries([job.id for job in jobs])
    payloads = jobs_payload(jobs)
    for job, payload in zip(jobs, payloads, strict=True):
        summary = summaries.get(job.id, {})
        prompt_snapshot = job.options.get("translation_prompt")
        if not isinstance(prompt_snapshot, Mapping):
            prompt_snapshot = {}
        revision_number = prompt_snapshot.get("revision_number")
        if not isinstance(revision_number, int) or isinstance(
            revision_number,
            bool,
        ):
            revision_number = None
        category_id = str(prompt_snapshot.get("category_id", "")).strip()
        prompt_name = {
            "jav": "JAV",
            "variety": "버라이어티",
        }.get(category_id, job.prompt_category_name)
        started_at = summary.get("started_at")
        ended_at = summary.get("ended_at")
        processing_seconds = summary.get("processing_seconds")
        transcription_backend = summary.get("transcription_backend")
        payload["completion_summary"] = {
            "transcription_backend": str(
                transcription_backend
                or job.options.get("backend")
                or "기본"
            ),
            "transcription_model_revision": summary.get(
                "transcription_model_revision"
            ),
            "translation_prompt_name": prompt_name,
            "translation_prompt_version": revision_number,
            "started_at": format_kst_iso(
                started_at
                if isinstance(started_at, int | float)
                else job.created_at
            ),
            "ended_at": format_kst_iso(
                ended_at
                if isinstance(ended_at, int | float)
                else job.status_updated_at
            ),
            "processing_seconds": (
                processing_seconds
                if isinstance(processing_seconds, int | float)
                else max(0.0, job.status_updated_at - job.created_at)
            ),
            "timing_source": summary.get("timing_source", "job"),
        }
    return payloads


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
    sampled_states = (
        JobState.WAITING,
        JobState.PAUSED,
        JobState.BLOCKED,
        JobState.STOPPED,
        JobState.FAILED,
    )
    completion_statuses = {
        "audio": "audio_completed",
        "transcription": "transcription_completed",
        "subtitle": "completed",
    }
    recent_completed = service.store.list_jobs(
        statuses={completion_statuses["subtitle"]},
        limit=10,
        include_comparison_transcriptions=False,
    )
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
        "completion_counts": {
            name: service.store.count_jobs(
                statuses={completion_status},
                include_comparison_transcriptions=False,
            )
            for name, completion_status in completion_statuses.items()
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
        "recent_completed": _completed_jobs_payload(service, recent_completed),
        "state_samples": {
            state.value: jobs_payload(
                service.store.list_jobs(
                    states={state.value},
                    limit=3,
                    include_comparison_transcriptions=False,
                )
            )
            for state in sampled_states
        },
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
        "operations": [
            "transcribe",
            "draft_translate",
            "review_translate",
            "external_review",
        ],
        "transcription_backends": [
            "whisperjav",
            "hybrid",
        ],
        "phases": [phase.value for phase in JobPhase],
        "states": [state.value for state in JobState],
        "reason_codes": [reason.value for reason in JobReason],
        "subtitle_validator_providers": [
            "openai_compatible",
            "openrouter",
            "bedrock",
        ],
        "external_model_providers": [
            "openrouter",
            "bedrock",
            "nvidia_build",
        ],
    }


@router.get("/settings")
def settings(request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    translation_groups: list[dict[str, Any]] = []
    translation_groups_error: str | None = None
    try:
        translation_groups = service.translation_groups_view()
    except (ExternalServiceError, ValueError) as error:
        translation_groups_error = service.sanitize_external_error(str(error))
    return {
        "servers": service.remote_servers_view(),
        "transcribers": service.runtime_endpoints_view(),
        "translation_groups": translation_groups,
        "translation_groups_error": translation_groups_error,
        "subtitle_validator": service.subtitle_validator_view(),
        "external_models": service.external_model_profiles_view(),
        "path_display_rules": public_value(service.path_display_rules),
        "prompt_categories": public_value(service.all_prompt_categories()),
        "prompt_authoring": public_value(service.prompt_authoring_view()),
        "translation_feedback": public_value(
            service.translation_feedback_view()
        ),
        "prompt_improvement_runs": public_value(
            service.prompt_improvement_runs_view()
        ),
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
        resource_group_id=current.resource_group_id,
    )
    try:
        service.update_remote_servers(updated)
    except ValueError as error:
        raise bad_request(error) from error
    return service.remote_servers_view()


@router.get("/transcribers")
def transcriber_endpoints(request: Request) -> dict[str, Any]:
    items = service_from_request(request).runtime_endpoints_view()
    return {"items": items, "total": len(items)}


@router.post("/transcribers", status_code=201)
def create_transcriber_endpoint(
    payload: TranscriberEndpointCreateRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).create_runtime_endpoint(
            name=payload.name,
            base_url=payload.base_url,
            token=payload.token,
            enabled=payload.enabled,
            capacity=payload.capacity,
            resource_group_id=payload.resource_group_id,
            kotoba_batch_size=payload.kotoba_batch_size,
            whisperx_batch_size=payload.whisperx_batch_size,
        )
    except ValueError as error:
        raise bad_request(error) from error


@router.put("/transcribers/{transcriber_id}")
def update_transcriber_endpoint(
    transcriber_id: str,
    payload: TranscriberEndpointUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).update_runtime_endpoint(
            transcriber_id,
            name=payload.name,
            base_url=payload.base_url,
            token=payload.token,
            clear_token=payload.clear_token,
            enabled=payload.enabled,
            capacity=payload.capacity,
            resource_group_id=payload.resource_group_id,
            kotoba_batch_size=payload.kotoba_batch_size,
            whisperx_batch_size=payload.whisperx_batch_size,
            clear_kotoba_batch_size=payload.clear_kotoba_batch_size,
            clear_whisperx_batch_size=payload.clear_whisperx_batch_size,
        )
    except ValueError as error:
        raise bad_request(error) from error


@router.delete("/transcribers/{transcriber_id}", status_code=204)
def delete_transcriber_endpoint(
    transcriber_id: str,
    request: Request,
) -> Response:
    try:
        service_from_request(request).delete_runtime_endpoint(transcriber_id)
    except ValueError as error:
        raise bad_request(error) from error
    return Response(status_code=204)


@router.post("/transcribers/{transcriber_id}/probe")
def probe_transcriber_endpoint(
    transcriber_id: str,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).probe_runtime_endpoint(
            transcriber_id
        )
    except ValueError as error:
        raise bad_request(error) from error


def _translation_settings_failure(error: Exception) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail="번역 서버 설정 요청을 처리할 수 없습니다.",
    )


@router.put("/translation-groups/{stage}/servers/{endpoint_id}/model")
def update_translation_server_model(
    stage: str,
    endpoint_id: str,
    payload: TranslationServerModelRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).update_translation_server_model(
            stage,
            endpoint_id,
            payload.model,
        )
    except (ExternalServiceError, ValueError) as error:
        raise _translation_settings_failure(error) from error


@router.post("/translation-groups/{stage}/servers", status_code=201)
def create_translation_endpoint(
    stage: str,
    payload: TranslationEndpointCreateRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).create_translation_endpoint(
            stage,
            payload.model_dump()
        )
    except (ExternalServiceError, ValueError) as error:
        raise _translation_settings_failure(error) from error


@router.put("/translation-groups/{stage}/servers/{endpoint_id}")
def update_translation_endpoint(
    stage: str,
    endpoint_id: str,
    payload: TranslationEndpointUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).update_translation_endpoint(
            stage,
            endpoint_id,
            payload.model_dump(),
        )
    except (ExternalServiceError, ValueError) as error:
        raise _translation_settings_failure(error) from error


@router.delete(
    "/translation-groups/{stage}/servers/{endpoint_id}",
    status_code=204,
)
def delete_translation_endpoint(
    stage: str,
    endpoint_id: str,
    request: Request,
) -> Response:
    try:
        service_from_request(request).delete_translation_endpoint(
            stage,
            endpoint_id,
        )
    except (ExternalServiceError, ValueError) as error:
        raise _translation_settings_failure(error) from error
    return Response(status_code=204)


@router.post("/translation-groups/{stage}/servers/{endpoint_id}/probe")
def probe_translation_endpoint(
    stage: str,
    endpoint_id: str,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).probe_translation_endpoint(
            stage,
            endpoint_id
        )
    except (ExternalServiceError, ValueError) as error:
        raise _translation_settings_failure(error) from error


@router.put("/translation-groups/{stage}/servers/{endpoint_id}/routing")
def update_translation_endpoint_routing(
    stage: str,
    endpoint_id: str,
    payload: TranslationEndpointRoutingRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(
            request
        ).update_translation_endpoint_routing(
            stage,
            endpoint_id,
            payload.model_dump(),
        )
    except (ExternalServiceError, ValueError) as error:
        raise _translation_settings_failure(error) from error


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


@router.get("/settings/external-models")
def external_models(request: Request) -> dict[str, Any]:
    items = service_from_request(request).external_model_profiles_view()
    return {"items": items, "total": len(items)}


@router.put("/settings/external-models/{provider}")
def update_external_model(
    provider: str,
    payload: ExternalModelProfileRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).update_external_model_profile(
            provider,
            base_url=payload.base_url,
            credential=payload.credential,
            clear_credential=payload.clear_credential,
            region=payload.region,
        )
    except ValueError as error:
        raise bad_request(error) from error


@router.post("/settings/external-models/{provider}/probe")
def probe_external_model(
    provider: str,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).probe_external_model_profile(
            provider
        )
    except ValueError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@router.put("/settings/external-models/{provider}/model")
def select_external_model(
    provider: str,
    payload: ExternalModelSelectionRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return service_from_request(request).select_external_model(
            provider,
            payload.model,
        )
    except ValueError as error:
        raise bad_request(error) from error


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


@router.get("/settings/translation-feedback")
def translation_feedback(
    request: Request,
    category_id: str | None = None,
    stage: str | None = Query(default=None, pattern="^(translation|review)$"),
    included: bool | None = None,
) -> dict[str, Any]:
    items = service_from_request(request).translation_feedback_view(
        category_id=category_id,
        stage=stage,
        included=included,
    )
    return {"items": public_value(items), "total": len(items)}


@router.patch("/settings/translation-feedback/{feedback_id}")
def set_translation_feedback_state(
    feedback_id: str,
    payload: TranslationFeedbackStateRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return public_value(
            service_from_request(request).set_translation_feedback_included(
                feedback_id,
                included=payload.included,
            )
        )
    except ValueError as error:
        raise bad_request(error) from error


@router.get("/settings/prompt-improvements")
def prompt_improvements(
    request: Request,
    category_id: str | None = None,
    stage: str | None = Query(default=None, pattern="^(translation|review)$"),
) -> dict[str, Any]:
    items = service_from_request(request).prompt_improvement_runs_view(
        category_id=category_id,
        stage=stage,
    )
    return {"items": public_value(items), "total": len(items)}


@router.post("/settings/prompt-improvements", status_code=202)
def create_prompt_improvement(
    payload: PromptImprovementCreateRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return public_value(
            service_from_request(request).create_prompt_improvement(
                category_id=payload.category_id,
                stage=payload.stage,
                provider=payload.provider,
                model=payload.model,
            )
        )
    except ValueError as error:
        raise bad_request(error) from error


@router.post("/settings/prompt-drafts")
def create_prompt_draft(
    payload: PromptDraftCreateRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        return public_value(
            service_from_request(request).create_prompt_draft(
                name=payload.name,
                domain_description=payload.domain_description,
                provider=payload.provider,
                model=payload.model,
            )
        )
    except ExternalServiceError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error
    except ValueError as error:
        raise bad_request(error) from error


@router.post("/settings/prompt-improvements/{run_id}/cancel")
def cancel_prompt_improvement(run_id: str, request: Request) -> dict[str, Any]:
    try:
        return public_value(
            service_from_request(request).cancel_prompt_improvement(run_id)
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/settings/prompt-improvements/{run_id}/reject")
def reject_prompt_improvement(run_id: str, request: Request) -> dict[str, Any]:
    try:
        return public_value(
            service_from_request(request).reject_prompt_improvement(run_id)
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/settings/prompt-improvements/{run_id}/activate")
def activate_prompt_improvement(run_id: str, request: Request) -> dict[str, Any]:
    try:
        return public_value(
            service_from_request(request).activate_prompt_improvement(run_id)
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


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


@router.get("/operations/metrics/media-durations")
def operation_media_duration_metrics(
    request: Request,
    window_days: int = Query(default=30, ge=1, le=3650),
) -> dict[str, Any]:
    try:
        return service_from_request(request).store.media_duration_metrics(
            window_seconds=window_days * 24 * 60 * 60,
        )
    except ValueError as error:
        raise bad_request(error) from error


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
