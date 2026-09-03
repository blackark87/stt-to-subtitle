"""Job, artifact, and playback JSON API routes."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Annotated, Any, Literal, Mapping

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse

from .artifacts import artifact_filename
from .backend_common import (
    bad_request,
    job_payload,
    jobs_payload,
    public_value,
    require_job,
    service_from_request,
)
from .backend_contracts import (
    ArtifactUpdateRequest,
    ExternalReviewSelectionRequest,
    JobCreateRequest,
    JobIdsRequest,
    ReprocessRequest,
    RestartTranslationRequest,
    SubtitleGenerationPublishRequest,
    SubtitleTimelineUpdateRequest,
    TranslationItemUpdateRequest,
    TranslationSelectionRequest,
)
from .files import sha256_file
from .media_preview import (
    guess_media_type,
    iter_file_range,
    parse_byte_range,
    read_subtitle_text,
    srt_to_webvtt,
)
from .service_clients import ExternalServiceError
from .subtitle_validation import (
    compare_subtitles,
    parse_subtitle,
    render_webvtt,
    subtitle_asset_hash,
)
from .translation_comparison import compare_translation_items


router = APIRouter(prefix="/api/v1")


_WORKFLOW_OPERATION_ORDER = {
    "extract": 0,
    "transcribe": 1,
    "full": 1,
    "translate": 2,
    "draft_translate": 2,
    "review_translate": 3,
    "external_review": 4,
}


def _job_list_payloads(service: Any, jobs: list[Any]) -> list[dict[str, Any]]:
    items = jobs_payload(jobs)
    for item, job in zip(items, jobs, strict=True):
        try:
            metadata = service.library.media_display_metadata(job.source_rel)
        except (OSError, ValueError):
            metadata = {"nfo_title": None, "poster_path": None}
        item.update(metadata)
    return items


def _job_artifact(job: Any, kind: str) -> tuple[Path, str, str]:
    fields = {
        "transcript": (job.transcript_path, "application/json"),
        "translation": (job.translation_path, "application/json"),
    }
    if kind not in fields:
        raise HTTPException(status_code=404, detail="artifact not found")
    raw_path, media_type = fields[kind]
    if not raw_path or not Path(raw_path).is_file():
        raise HTTPException(status_code=404, detail="artifact not found")
    return Path(raw_path), artifact_filename(job.source_rel, kind), media_type


def _safe_generation_artifact(
    jobs_dir: Path,
    job_id: str,
    raw_path: object,
) -> Path:
    root = (jobs_dir / job_id).resolve()
    artifact = Path(str(raw_path)).resolve()
    try:
        artifact.relative_to(root)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="generation not found") from error
    if not artifact.is_file():
        raise HTTPException(status_code=404, detail="generation not found")
    return artifact


def _translation_source_texts(
    jobs_dir: Path,
    job: Any,
    generation: dict[str, Any],
) -> dict[str, str]:
    root = jobs_dir.resolve()
    candidates = [generation.get("transcript_artifact_path"), job.transcript_path]
    for raw_path in dict.fromkeys(value for value in candidates if value):
        try:
            path = Path(str(raw_path)).resolve()
            path.relative_to(root)
            if not path.is_file() or sha256_file(path) != generation["transcript_hash"]:
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
            segments = document.get("segments", [])
            if not isinstance(segments, list):
                continue
            return {
                str(segment["id"]): str(segment["text"])
                for segment in segments
                if isinstance(segment, dict) and "id" in segment and "text" in segment
            }
        except (KeyError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
            continue
    return {}


def _current_subtitle_validation(service: Any, job: Any) -> dict[str, Any] | None:
    try:
        external = service.library.external_subtitles(job.source_rel)
        candidate = next(
            (
                Path(value)
                for value in (job.srt_path, job.ass_path)
                if value and Path(value).is_file()
            ),
            None,
        )
        if not external or candidate is None:
            return None
        return service.store.get_subtitle_validation(
            job_id=job.id,
            external_hash=subtitle_asset_hash(external),
            candidate_hash=sha256_file(candidate),
        )
    except OSError:
        return None


def _workflow_root_id(job: Any, jobs_by_id: dict[str, Any]) -> str:
    """Return the transcription/root execution that owns a phase-job chain."""

    current = job
    seen = {str(job.id)}
    while True:
        parent_id = str(
            current.options.get("pipeline_parent_job_id", "") or ""
        )
        if not parent_id or parent_id in seen:
            return str(current.id)
        parent = jobs_by_id.get(parent_id)
        if parent is None:
            return str(current.id)
        seen.add(parent_id)
        current = parent


def _workflow_parent_id(job: Any) -> str:
    return str(job.options.get("pipeline_parent_job_id", "") or "")


def _workflow_depth(job: Any, jobs_by_id: dict[str, Any]) -> int:
    depth = 0
    current = job
    seen = {str(job.id)}
    while True:
        parent_id = _workflow_parent_id(current)
        if not parent_id or parent_id in seen:
            return depth
        parent = jobs_by_id.get(parent_id)
        if parent is None:
            return depth
        seen.add(parent_id)
        current = parent
        depth += 1


def _workflow_job_rank(job: Any, jobs_by_id: dict[str, Any]) -> tuple[Any, ...]:
    return (
        job.state != "done",
        _workflow_depth(job, jobs_by_id),
        _WORKFLOW_OPERATION_ORDER.get(job.operation, 0),
        job.updated_at,
        job.created_at,
        str(job.id),
    )


def _workflow_primary_path(workflow_jobs: list[Any]) -> list[Any]:
    """Select the current/deepest branch and return its root-to-leaf lineage."""

    if not workflow_jobs:
        return []
    jobs_by_id = {str(candidate.id): candidate for candidate in workflow_jobs}
    parent_ids = {
        parent_id
        for candidate in workflow_jobs
        if (parent_id := _workflow_parent_id(candidate)) in jobs_by_id
    }
    leaves = [
        candidate
        for candidate in workflow_jobs
        if str(candidate.id) not in parent_ids
    ]
    leaf = max(
        leaves or workflow_jobs,
        key=lambda candidate: _workflow_job_rank(candidate, jobs_by_id),
    )
    lineage = [leaf]
    seen = {str(leaf.id)}
    current = leaf
    while True:
        parent_id = _workflow_parent_id(current)
        if not parent_id or parent_id in seen:
            break
        parent = jobs_by_id.get(parent_id)
        if parent is None:
            break
        lineage.append(parent)
        seen.add(parent_id)
        current = parent
    return list(reversed(lineage))


def _workflow_list_payloads(
    service: Any,
    projections: list[tuple[Any, str]],
) -> list[dict[str, Any]]:
    representatives = [projection[0] for projection in projections]
    items = _job_list_payloads(service, representatives)
    for item, (_, root_id) in zip(
        items,
        projections,
        strict=True,
    ):
        item["workflow_root_job_id"] = root_id
    return items


def _previous_subtitle_workflow_payloads(
    service: Any,
    job: Any,
    all_jobs: list[Any],
    jobs_by_id: dict[str, Any],
) -> list[dict[str, Any]]:
    """Summarize subtitle-producing runs outside the current workflow."""
    current_root_id = _workflow_root_id(job, jobs_by_id)
    generations_by_root: dict[str, list[tuple[dict[str, Any], Any]]] = {}
    for generation in service.store.list_subtitle_generations_for_source(
        job.source_rel
    ):
        generation_job = jobs_by_id.get(str(generation["job_id"]))
        if generation_job is None:
            continue
        root_id = _workflow_root_id(generation_job, jobs_by_id)
        if root_id == current_root_id:
            continue
        generations_by_root.setdefault(root_id, []).append(
            (generation, generation_job)
        )

    legacy_jobs_by_root: dict[str, list[Any]] = {}
    for candidate in all_jobs:
        if candidate.source_rel != job.source_rel:
            continue
        root_id = _workflow_root_id(candidate, jobs_by_id)
        if root_id == current_root_id:
            continue
        if candidate.status == "completed" and (
            candidate.srt_path or candidate.ass_path
        ):
            legacy_jobs_by_root.setdefault(root_id, []).append(candidate)

    historical_root_ids = (
        generations_by_root.keys() | legacy_jobs_by_root.keys()
    )
    historical_jobs = [
        candidate
        for candidate in all_jobs
        if candidate.source_rel == job.source_rel
        and _workflow_root_id(candidate, jobs_by_id) in historical_root_ids
    ]
    completion_summaries = service.store.job_completion_summaries(
        [str(candidate.id) for candidate in historical_jobs]
    )
    summaries: list[dict[str, Any]] = []
    for root_id in historical_root_ids:
        generations = generations_by_root.get(root_id, [])
        if generations:
            latest_generation, latest_job = max(
                generations,
                key=lambda item: (
                    float(item[0]["created_at"]),
                    int(item[0]["generation_number"]),
                    str(item[0]["id"]),
                ),
            )
            generated_at = float(latest_generation["created_at"])
        else:
            latest_job = max(
                legacy_jobs_by_root[root_id],
                key=lambda candidate: (
                    candidate.status_updated_at,
                    candidate.updated_at,
                    str(candidate.id),
                ),
            )
            generated_at = float(latest_job.status_updated_at)
        workflow_jobs = [
            candidate
            for candidate in all_jobs
            if candidate.source_rel == job.source_rel
            and _workflow_root_id(candidate, jobs_by_id) == root_id
        ]
        workflow_jobs.sort(
            key=lambda candidate: (
                candidate.updated_at,
                candidate.created_at,
                str(candidate.id),
            ),
            reverse=True,
        )
        transcript_job = next(
            (
                candidate
                for candidate in workflow_jobs
                if candidate.transcript_path
            ),
            None,
        )
        translation_job = next(
            (
                candidate
                for candidate in workflow_jobs
                if candidate.translation_path
            ),
            None,
        )
        completion = completion_summaries.get(
            str(transcript_job.id) if transcript_job is not None else "",
            completion_summaries.get(str(latest_job.id), {}),
        )
        prompt_job = next(
            (
                candidate
                for candidate in workflow_jobs
                if isinstance(
                    candidate.options.get("translation_prompt"),
                    Mapping,
                )
            ),
            latest_job,
        )
        prompt_snapshot = prompt_job.options.get("translation_prompt")
        if not isinstance(prompt_snapshot, Mapping):
            prompt_snapshot = {}
        prompt_revision = prompt_snapshot.get("revision_number")
        if not isinstance(prompt_revision, int) or isinstance(
            prompt_revision,
            bool,
        ):
            prompt_revision = None
        prompt_category_id = str(
            prompt_snapshot.get("category_id", "")
        ).strip()
        prompt_name = {
            "jav": "JAV",
            "variety": "버라이어티",
        }.get(prompt_category_id)
        prompt_name = (
            prompt_name
            or str(prompt_snapshot.get("category_name", "")).strip()
            or prompt_job.prompt_category_name
            or "미기록"
        )
        transcription_backend = completion.get("transcription_backend")
        summaries.append(
            {
                "workflow_root_job_id": root_id,
                "latest_job_id": str(latest_job.id),
                "latest_generation_created_at": generated_at,
                "transcription_backend": str(
                    transcription_backend
                    or (
                        transcript_job.options.get("backend")
                        if transcript_job is not None
                        else None
                    )
                    or latest_job.options.get("backend")
                    or "기본"
                ),
                "transcription_model_revision": completion.get(
                    "transcription_model_revision"
                ),
                "translation_prompt_name": prompt_name,
                "translation_prompt_version": prompt_revision,
                "is_test": any(
                    candidate.is_test for candidate in workflow_jobs
                ),
                "transcript_job_id": (
                    str(transcript_job.id)
                    if transcript_job is not None
                    else None
                ),
                "translation_job_id": (
                    str(translation_job.id)
                    if translation_job is not None
                    else None
                ),
            }
        )
    summaries.sort(
        key=lambda item: (
            float(item["latest_generation_created_at"]),
            str(item["workflow_root_job_id"]),
        ),
        reverse=True,
    )
    return summaries


@router.get("/jobs")
def list_jobs(
    request: Request,
    operation: list[str] | None = Query(default=None),
    phase: list[str] | None = Query(default=None),
    state: list[str] | None = Query(default=None),
    reason_code: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    include_comparisons: bool = True,
) -> dict[str, Any]:
    service = service_from_request(request)
    filters = {
        "operations": operation,
        "phases": phase,
        "states": state,
        "reason_codes": reason_code,
        "include_comparison_transcriptions": include_comparisons,
    }
    all_jobs = service.store.list_jobs(
        limit=None,
        include_comparison_transcriptions=True,
    )
    jobs_by_id = {str(job.id): job for job in all_jobs}
    matching_jobs = service.store.list_jobs(limit=None, **filters)
    matching_ids = {str(job.id) for job in matching_jobs}
    grouped: dict[str, list[Any]] = {}
    for job in all_jobs:
        root_id = _workflow_root_id(job, jobs_by_id)
        grouped.setdefault(root_id, []).append(job)

    projections: list[tuple[Any, str]] = []
    for root_id, workflow_jobs in grouped.items():
        primary_path = _workflow_primary_path(workflow_jobs)
        representative = primary_path[-1]
        if str(representative.id) not in matching_ids:
            continue
        projections.append((representative, root_id))
    projections.sort(
        key=lambda projection: (
            projection[0].updated_at,
            projection[0].created_at,
            str(projection[0].id),
        ),
        reverse=True,
    )
    selected = projections[offset : offset + limit]
    return {
        "items": _workflow_list_payloads(service, selected),
        "total": len(projections),
        "limit": limit,
        "offset": offset,
    }


@router.post("/jobs", status_code=201)
def create_jobs(payload: JobCreateRequest, request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    if payload.operation in {
        "full",
        "translate",
        "draft_translate",
        "review_translate",
        "external_review",
    }:
        raise HTTPException(
            status_code=409,
            detail=(
                "통합 작업 생성은 지원하지 않습니다. 전사를 만든 뒤 "
                "각 단계 작업 버튼을 사용하세요."
            ),
        )
    job_options = dict(payload.options)
    if payload.operation == "transcribe":
        backend = str(job_options.get("backend", "hybrid")).strip().lower()
        if backend not in {
            "whisperjav",
            "hybrid",
        }:
            raise HTTPException(
                status_code=400,
                detail=(
                    "전사 모델은 WhisperJAV 또는 Hybrid여야 합니다."
                ),
            )
        job_options["backend"] = backend
    try:
        sources, skipped = service.expand_job_sources(
            payload.source_rels,
            payload.folder_rels,
            force_overwrite=payload.force_overwrite,
            operation=payload.operation,
        )
        jobs = service.create_jobs(
            sources,
            force_overwrite=payload.force_overwrite,
            is_test=payload.is_test,
            options=job_options,
            operation=payload.operation,
            prompt_category_id=payload.prompt_category_id,
        )
    except (FileExistsError, OSError, ValueError) as error:
        raise bad_request(error) from error
    return {
        "items": jobs_payload(jobs),
        "created": len(jobs),
        "skipped": skipped,
    }


@router.post("/jobs/actions/retry")
def retry_jobs(payload: JobIdsRequest, request: Request) -> dict[str, int]:
    try:
        count = service_from_request(request).retry_jobs(payload.job_ids)
    except ValueError as error:
        raise bad_request(error) from error
    return {"updated": count}


@router.post("/jobs/actions/stop")
def stop_jobs(payload: JobIdsRequest, request: Request) -> dict[str, int]:
    return {"updated": service_from_request(request).stop_jobs(payload.job_ids)}


@router.post("/jobs/actions/pause-translations")
def pause_translations(
    payload: JobIdsRequest,
    request: Request,
) -> dict[str, int]:
    service = service_from_request(request)
    updated = 0
    for job_id in dict.fromkeys(payload.job_ids):
        try:
            service.pause_translation(job_id)
        except ValueError:
            continue
        updated += 1
    return {"updated": updated}


@router.post("/jobs/actions/translate", status_code=201)
def translate_jobs(
    payload: TranslationSelectionRequest,
    request: Request,
) -> dict[str, Any]:
    translation_mode = payload.translation_mode
    if payload.target_stage is not None:
        translation_mode = (
            "draft_only"
            if payload.target_stage == "draft"
            else "review_existing"
        )
    if translation_mode == "draft_and_review":
        raise HTTPException(
            status_code=409,
            detail="1차와 2차 번역은 각각 별도 작업으로 요청하세요.",
        )
    try:
        jobs = service_from_request(request).create_phase_translation_jobs(
            payload.job_ids,
            prompt_category_id=payload.prompt_category_id,
            stage=(
                "draft" if translation_mode == "draft_only" else "review"
            ),
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise bad_request(error) from error
    return {"items": jobs_payload(jobs), "created": len(jobs)}


@router.post("/jobs/actions/draft-translate", status_code=201)
def draft_translate_jobs(
    payload: TranslationSelectionRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        jobs = service_from_request(request).create_phase_translation_jobs(
            payload.job_ids,
            prompt_category_id=payload.prompt_category_id,
            stage="draft",
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise bad_request(error) from error
    return {"items": jobs_payload(jobs), "created": len(jobs)}


@router.post("/jobs/actions/review-translate", status_code=201)
def review_translate_jobs(
    payload: TranslationSelectionRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        jobs = service_from_request(request).create_phase_translation_jobs(
            payload.job_ids,
            prompt_category_id=payload.prompt_category_id,
            stage="review",
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise bad_request(error) from error
    return {"items": jobs_payload(jobs), "created": len(jobs)}


@router.post("/jobs/actions/external-review", status_code=201)
def external_review_jobs(
    payload: ExternalReviewSelectionRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        jobs = service_from_request(request).create_external_review_jobs(
            payload.job_ids,
            provider=payload.provider,
            model=payload.model,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise bad_request(error) from error
    return {"items": jobs_payload(jobs), "created": len(jobs)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    job = require_job(service, job_id)
    all_jobs = service.store.list_jobs(limit=None)
    jobs_by_id = {candidate.id: candidate for candidate in all_jobs}
    parent_id = str(job.options.get("pipeline_parent_job_id", "") or "")
    parent = jobs_by_id.get(parent_id) if parent_id else None
    children = [
        candidate
        for candidate in all_jobs
        if candidate.options.get("pipeline_parent_job_id") == job.id
    ]
    workflow_root_id = _workflow_root_id(job, jobs_by_id)
    grouped_workflow_jobs = [
        candidate
        for candidate in all_jobs
        if _workflow_root_id(candidate, jobs_by_id) == workflow_root_id
    ]
    workflow_jobs = _workflow_primary_path(grouped_workflow_jobs)
    workflow_job_ids = {str(candidate.id) for candidate in workflow_jobs}
    workflow_history_jobs = sorted(
        (
            candidate
            for candidate in grouped_workflow_jobs
            if str(candidate.id) not in workflow_job_ids
        ),
        key=lambda candidate: (candidate.created_at, candidate.id),
    )
    previous_subtitle_workflows = _previous_subtitle_workflow_payloads(
        service,
        job,
        all_jobs,
        jobs_by_id,
    )
    return {
        "job": job_payload(job),
        "parent_job": job_payload(parent) if parent is not None else None,
        "child_jobs": jobs_payload(children),
        "workflow_root_job_id": workflow_root_id,
        "workflow_jobs": jobs_payload(workflow_jobs),
        "workflow_history_jobs": jobs_payload(workflow_history_jobs),
        "previous_subtitle_workflows": previous_subtitle_workflows,
        "events": public_value(service.store.events(job.id)),
        "transcript_revisions": public_value(
            service.store.transcript_revisions(job.id)
        ),
        "translation_generations": public_value(
            service.store.list_translation_generations(job.id)
        ),
        "subtitle_generations": public_value(
            service.store.list_subtitle_generations(job.id)
        ),
        "subtitle_validation": public_value(_current_subtitle_validation(service, job)),
        "external_subtitles": [
            path.name for path in service.library.external_subtitles(job.source_rel)
        ],
    }


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, request: Request) -> Response:
    try:
        service_from_request(request).delete_job_record(job_id)
    except ValueError as error:
        raise bad_request(error) from error
    return Response(status_code=204)


@router.get("/jobs/{job_id}/events")
def job_events(
    job_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    service = service_from_request(request)
    require_job(service, job_id)
    return {"items": public_value(service.store.events(job_id, limit=limit))}


@router.post("/jobs/{job_id}/retry")
def retry_job(job_id: str, request: Request) -> dict[str, Any]:
    try:
        job = service_from_request(request).retry(job_id)
    except ValueError as error:
        raise bad_request(error) from error
    return job_payload(job)


@router.post("/jobs/{job_id}/stop")
def stop_job(job_id: str, request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    require_job(service, job_id)
    if service.stop_jobs([job_id]) != 1:
        raise HTTPException(status_code=409, detail="job cannot be stopped")
    return job_payload(require_job(service, job_id))


@router.post("/jobs/{job_id}/pause-translation")
def pause_translation(job_id: str, request: Request) -> dict[str, Any]:
    try:
        job = service_from_request(request).pause_translation(job_id)
    except ValueError as error:
        raise bad_request(error) from error
    return job_payload(job)


@router.post("/jobs/{job_id}/resume-translation")
def resume_translation(job_id: str, request: Request) -> dict[str, Any]:
    try:
        job = service_from_request(request).resume_translation(job_id)
    except ValueError as error:
        raise bad_request(error) from error
    return job_payload(job)


@router.post("/jobs/{job_id}/restart-translation")
def restart_translation(
    job_id: str,
    payload: RestartTranslationRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        job = service_from_request(request).restart_translation(
            job_id,
            prompt_category_id=payload.prompt_category_id,
            transcript_revision_id=payload.transcript_revision_id,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise bad_request(error) from error
    return job_payload(job)


@router.post("/jobs/{job_id}/reprocess", status_code=201)
def reprocess_job(
    job_id: str,
    payload: ReprocessRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        job = service_from_request(request).reprocess(
            job_id,
            payload.operation,
            prompt_category_id=payload.prompt_category_id,
        )
    except (FileExistsError, OSError, ValueError) as error:
        raise bad_request(error) from error
    return job_payload(job)


@router.get("/jobs/{job_id}/artifacts/{kind}")
def download_artifact(
    job_id: str,
    kind: str,
    request: Request,
    inline: Annotated[
        bool,
        Query(description="브라우저에서 JSON 산출물을 바로 표시합니다."),
    ] = False,
) -> FileResponse:
    service = service_from_request(request)
    job = require_job(service, job_id)
    path, filename, media_type = _job_artifact(job, kind)
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        content_disposition_type="inline" if inline else "attachment",
    )


@router.put("/jobs/{job_id}/artifacts/{kind}")
def update_artifact(
    job_id: str,
    kind: str,
    payload: ArtifactUpdateRequest,
    request: Request,
) -> dict[str, str]:
    try:
        path = service_from_request(request).save_artifact(
            job_id,
            kind,
            payload.content,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise bad_request(error) from error
    return {"path": path.name}


@router.api_route("/jobs/{job_id}/video", methods=["GET", "HEAD"])
def job_video(job_id: str, request: Request) -> Response:
    service = service_from_request(request)
    job = require_job(service, job_id)
    try:
        source = service.library.resolve_file(job.source_rel)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="media file not found") from error
    size = source.stat().st_size
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-cache"}
    try:
        byte_range = parse_byte_range(request.headers.get("range"), size)
    except (OverflowError, ValueError):
        return Response(
            status_code=416,
            headers={**headers, "Content-Range": f"bytes */{size}"},
        )
    start, end = byte_range if byte_range is not None else (0, size - 1)
    status_code = 206 if byte_range is not None else 200
    headers["Content-Length"] = str(max(0, end - start + 1))
    if byte_range is not None:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    if request.method == "HEAD":
        return Response(
            status_code=status_code,
            media_type=guess_media_type(source.name),
            headers=headers,
        )
    return StreamingResponse(
        iter_file_range(source, start, end),
        status_code=status_code,
        media_type=guess_media_type(source.name),
        headers=headers,
    )


@router.get("/jobs/{job_id}/subtitles.vtt")
def playback_subtitles(job_id: str, request: Request) -> Response:
    service = service_from_request(request)
    job = require_job(service, job_id)
    try:
        external = service.library.external_subtitles(job.source_rel)
        published = service.store.published_subtitle_generation(job.id)
        if external:
            content = (
                srt_to_webvtt(read_subtitle_text(external[0]))
                if external[0].suffix.lower() == ".srt"
                else render_webvtt(parse_subtitle(external[0]))
            )
        elif published is not None and Path(
            str(published["srt_artifact_path"])
        ).is_file():
            content = srt_to_webvtt(
                read_subtitle_text(Path(str(published["srt_artifact_path"])))
            )
        elif job.srt_path and Path(job.srt_path).is_file():
            content = srt_to_webvtt(read_subtitle_text(Path(job.srt_path)))
        else:
            raise FileNotFoundError("subtitle not found")
    except (OSError, UnicodeError, ValueError) as error:
        raise HTTPException(status_code=404, detail="subtitle not found") from error
    return Response(content, media_type="text/vtt")


@router.get("/jobs/{job_id}/subtitle.{subtitle_format}")
def subtitle_file(
    job_id: str,
    subtitle_format: Literal["srt", "ass"],
    request: Request,
) -> FileResponse:
    service = service_from_request(request)
    job = require_job(service, job_id)
    published = service.store.published_subtitle_generation(job.id)
    raw_path = (
        published.get(f"{subtitle_format}_artifact_path")
        if published is not None
        else job.srt_path if subtitle_format == "srt" else job.ass_path
    )
    if not raw_path or not Path(raw_path).is_file():
        raise HTTPException(status_code=404, detail="subtitle not found")
    media_type = "application/x-subrip" if subtitle_format == "srt" else "text/x-ssa"
    return FileResponse(
        raw_path,
        media_type=media_type,
        filename=f"{Path(job.source_rel).stem}.ko.{subtitle_format}",
    )


@router.post("/jobs/{job_id}/validate-external-subtitle")
def validate_external_subtitle(job_id: str, request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    job = require_job(service, job_id)
    try:
        external = service.library.external_subtitles(job.source_rel)
        if not external:
            raise ValueError("외부 자막이 없습니다.")
        candidate = next(
            (
                Path(value)
                for value in (job.srt_path, job.ass_path)
                if value and Path(value).is_file()
            ),
            None,
        )
        if candidate is None:
            raise ValueError("비교할 시스템 생성 자막이 없습니다.")
        metrics = compare_subtitles(parse_subtitle(external[0]), parse_subtitle(candidate))
        validation = service.store.save_subtitle_validation(
            job_id=job.id,
            source_rel=job.source_rel,
            external_path=str(external[0]),
            external_hash=subtitle_asset_hash(external),
            candidate_path=str(candidate),
            candidate_hash=sha256_file(candidate),
            metrics=metrics,
        )
        service.record_subtitle_validation("local", "completed")
    except (OSError, UnicodeError, ValueError) as error:
        service.record_subtitle_validation("local", "failed")
        raise bad_request(error) from error
    return public_value(validation)


@router.post("/jobs/{job_id}/validate-external-subtitle/llm")
def validate_external_subtitle_with_llm(
    job_id: str,
    request: Request,
) -> dict[str, Any]:
    service = service_from_request(request)
    require_job(service, job_id)
    job = require_job(service, job_id)
    validation = _current_subtitle_validation(service, job)
    if validation is None:
        raise HTTPException(status_code=400, detail="비교 검증을 먼저 실행하세요.")
    try:
        updated, cached = service.validate_subtitles_with_llm(validation["id"])
    except (ExternalServiceError, ValueError) as error:
        raise bad_request(ValueError(service.sanitize_external_error(str(error)))) from error
    return {"validation": public_value(updated), "cached": cached}


@router.get("/jobs/{job_id}/translation-generations")
def translation_generations(job_id: str, request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    require_job(service, job_id)
    return {"items": public_value(service.store.list_translation_generations(job_id))}


@router.get("/jobs/{job_id}/translation-comparison")
def compare_translation_generations(
    job_id: str,
    request: Request,
    base_generation_id: str = "",
    candidate_generation_id: str = "",
    change: Literal[
        "changes",
        "all",
        "changed",
        "added",
        "removed",
        "source_changed",
        "unchanged",
    ] = "changes",
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    service = service_from_request(request)
    job = require_job(service, job_id)
    generations = service.store.list_translation_generations(job_id)
    if len(generations) < 2:
        raise HTTPException(status_code=400, detail="비교할 번역 버전이 부족합니다.")
    base_generation_id = base_generation_id or str(generations[-2]["id"])
    candidate_generation_id = candidate_generation_id or str(generations[-1]["id"])
    if base_generation_id == candidate_generation_id:
        raise HTTPException(status_code=400, detail="서로 다른 번역 버전을 선택하세요.")
    by_id = {str(item["id"]): item for item in generations}
    base = by_id.get(base_generation_id)
    candidate = by_id.get(candidate_generation_id)
    if base is None or candidate is None:
        raise HTTPException(status_code=404, detail="translation generation not found")
    try:
        comparison = compare_translation_items(
            service.store.translation_items(base_generation_id),
            service.store.translation_items(candidate_generation_id),
            base_source_texts=_translation_source_texts(
                service.settings.jobs_dir, job, base
            ),
            candidate_source_texts=_translation_source_texts(
                service.settings.jobs_dir, job, candidate
            ),
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    predicates = {
        "changes": lambda row: bool(row["has_change"]),
        "all": lambda _row: True,
        "changed": lambda row: row["state"] == "changed",
        "added": lambda row: row["state"] == "added",
        "removed": lambda row: row["state"] == "removed",
        "source_changed": lambda row: bool(row["source_changed"]),
        "unchanged": lambda row: not bool(row["has_change"]),
    }
    rows = [row for row in comparison["rows"] if predicates[change](row)]
    summary = {key: value for key, value in comparison.items() if key != "rows"}
    return {
        "base_generation": public_value(base),
        "candidate_generation": public_value(candidate),
        "summary": public_value(summary),
        "items": public_value(rows[offset : offset + limit]),
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "filter": change,
    }


@router.get("/jobs/{job_id}/translation-generations/{generation_id}")
def translation_generation_file(
    job_id: str,
    generation_id: str,
    request: Request,
) -> FileResponse:
    service = service_from_request(request)
    generation = service.store.get_translation_generation(generation_id)
    if generation is None or generation["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="generation not found")
    path = _safe_generation_artifact(
        service.settings.jobs_dir,
        job_id,
        generation["artifact_path"],
    )
    return FileResponse(path, media_type="application/json", filename=path.name)


@router.get("/jobs/{job_id}/translation-generations/{generation_id}/items")
def translation_generation_items(
    job_id: str,
    generation_id: str,
    request: Request,
) -> dict[str, Any]:
    service = service_from_request(request)
    generation = service.store.get_translation_generation(generation_id)
    if generation is None or generation["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="generation not found")
    return {
        "generation": public_value(generation),
        "items": public_value(service.store.translation_items(generation_id)),
        "batches": public_value(service.store.translation_batches(generation_id)),
    }


@router.put(
    "/jobs/{job_id}/translation-generations/{generation_id}/items/{segment_id}"
)
def update_translation_generation_item(
    job_id: str,
    generation_id: str,
    segment_id: str,
    payload: TranslationItemUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        result = service_from_request(request).edit_translation_item(
            job_id,
            generation_id=generation_id,
            segment_id=segment_id,
            text=payload.text,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise bad_request(error) from error
    return public_value(result)


@router.put(
    "/jobs/{job_id}/translation-generations/{generation_id}/timeline"
)
def update_subtitle_timeline(
    job_id: str,
    generation_id: str,
    payload: SubtitleTimelineUpdateRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        result = service_from_request(request).edit_subtitle_timeline(
            job_id,
            generation_id=generation_id,
            cues=[cue.model_dump() for cue in payload.cues],
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise bad_request(error) from error
    return public_value(result)


@router.get("/jobs/{job_id}/transcript-revisions/{revision_id}")
def transcript_revision_file(
    job_id: str,
    revision_id: str,
    request: Request,
) -> FileResponse:
    service = service_from_request(request)
    revision = service.store.get_transcript_revision(job_id, revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="revision not found")
    path = _safe_generation_artifact(
        service.settings.jobs_dir,
        job_id,
        revision["artifact_path"],
    )
    if sha256_file(path) != revision["content_hash"]:
        raise HTTPException(status_code=409, detail="revision integrity check failed")
    return FileResponse(path, media_type="application/json", filename=path.name)


@router.get("/jobs/{job_id}/subtitle-generations")
def subtitle_generations(job_id: str, request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    require_job(service, job_id)
    return {"items": public_value(service.store.list_subtitle_generations(job_id))}


@router.get("/jobs/{job_id}/subtitle-generations/{generation_id}.{subtitle_format}")
def subtitle_generation_file(
    job_id: str,
    generation_id: str,
    subtitle_format: Literal["srt", "ass"],
    request: Request,
) -> FileResponse:
    service = service_from_request(request)
    generation = service.store.get_subtitle_generation(generation_id)
    if generation is None or generation["job_id"] != job_id:
        raise HTTPException(status_code=404, detail="generation not found")
    field = f"{subtitle_format}_artifact_path"
    path = _safe_generation_artifact(service.settings.jobs_dir, job_id, generation[field])
    media_type = "application/x-subrip" if subtitle_format == "srt" else "text/x-ssa"
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.post("/jobs/{job_id}/subtitle-generations/publish")
def publish_subtitle_generation(
    job_id: str,
    payload: SubtitleGenerationPublishRequest,
    request: Request,
) -> dict[str, Any]:
    service = service_from_request(request)
    try:
        service.publish_subtitle_generation(job_id, payload.generation_id)
    except (OSError, ValueError) as error:
        raise bad_request(error) from error
    generation = service.store.get_subtitle_generation(payload.generation_id)
    return public_value(generation)
