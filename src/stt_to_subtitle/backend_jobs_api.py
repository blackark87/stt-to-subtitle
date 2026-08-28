"""Job, comparison, artifact, and playback JSON API routes."""

from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any, Literal

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
    ComparisonRerunRequest,
    ComparisonTranslationRequest,
    JobCreateRequest,
    JobIdsRequest,
    ReprocessRequest,
    RestartTranslationRequest,
    SubtitleGenerationPublishRequest,
    TranslationSelectionRequest,
)
from .files import sha256_file
from .media_preview import guess_media_type, iter_file_range, parse_byte_range, srt_to_webvtt
from .orchestrator import COMPARISON_AUDIO_SOURCE_JOB_ID_OPTION
from .service_clients import ExternalServiceError
from .subtitle_validation import (
    compare_subtitles,
    parse_subtitle,
    render_webvtt,
    subtitle_asset_hash,
)
from .translation_comparison import compare_translation_items


router = APIRouter(prefix="/api/v1")


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


@router.get("/jobs")
def list_jobs(
    request: Request,
    operation: list[str] | None = Query(default=None),
    phase: list[str] | None = Query(default=None),
    state: list[str] | None = Query(default=None),
    reason_code: list[str] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    include_comparisons: bool = False,
) -> dict[str, Any]:
    service = service_from_request(request)
    filters = {
        "operations": operation,
        "phases": phase,
        "states": state,
        "reason_codes": reason_code,
        "include_comparison_transcriptions": include_comparisons,
    }
    jobs = service.store.list_jobs(limit=limit, offset=offset, **filters)
    return {
        "items": jobs_payload(jobs),
        "total": service.store.count_jobs(**filters),
        "limit": limit,
        "offset": offset,
    }


@router.post("/jobs", status_code=201)
def create_jobs(payload: JobCreateRequest, request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    try:
        sources, skipped = service.expand_job_sources(
            payload.source_rels,
            payload.folder_rels,
            force_overwrite=payload.force_overwrite,
            operation=payload.operation,
        )
        if payload.operation == "compare":
            comparison_id, jobs = service.create_transcription_comparison(
                sources,
                options=payload.options,
            )
        else:
            comparison_id = None
            jobs = service.create_jobs(
                sources,
                force_overwrite=payload.force_overwrite,
                options=payload.options,
                operation=payload.operation,
                prompt_category_id=payload.prompt_category_id,
            )
    except (FileExistsError, OSError, ValueError) as error:
        raise bad_request(error) from error
    return {
        "items": jobs_payload(jobs),
        "created": len(jobs),
        "skipped": skipped,
        "comparison_id": comparison_id,
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
            else "draft_and_review"
        )
    try:
        jobs = service_from_request(request).create_selected_translation_jobs(
            payload.job_ids,
            prompt_category_id=payload.prompt_category_id,
            translation_mode=translation_mode,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise bad_request(error) from error
    return {"items": jobs_payload(jobs), "created": len(jobs)}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    service = service_from_request(request)
    job = require_job(service, job_id)
    return {
        "job": job_payload(job),
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
def download_artifact(job_id: str, kind: str, request: Request) -> FileResponse:
    service = service_from_request(request)
    job = require_job(service, job_id)
    path, filename, media_type = _job_artifact(job, kind)
    return FileResponse(path, media_type=media_type, filename=filename)


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
        if external:
            content = render_webvtt(parse_subtitle(external[0]))
        elif job.srt_path and Path(job.srt_path).is_file():
            content = srt_to_webvtt(Path(job.srt_path).read_text(encoding="utf-8-sig"))
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
    job = require_job(service_from_request(request), job_id)
    raw_path = job.srt_path if subtitle_format == "srt" else job.ass_path
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


def _comparison_groups(request: Request) -> dict[str, list[Any]]:
    groups: dict[str, list[Any]] = {}
    for job in service_from_request(request).store.list_jobs(limit=None):
        comparison_id = str(job.options.get("comparison_id", "")).strip()
        if comparison_id:
            groups.setdefault(comparison_id, []).append(job)
    return groups


@router.get("/comparisons")
def list_comparisons(request: Request) -> dict[str, Any]:
    items = []
    for comparison_id, jobs in _comparison_groups(request).items():
        items.append(
            {
                "id": comparison_id,
                "source_rels": list(dict.fromkeys(job.source_rel for job in jobs)),
                "jobs": jobs_payload(jobs),
                "updated_at": max(job.updated_at for job in jobs),
            }
        )
    items.sort(key=lambda item: float(item["updated_at"]), reverse=True)
    return {"items": items, "total": len(items)}


@router.get("/comparisons/{comparison_id}")
def get_comparison(comparison_id: str, request: Request) -> dict[str, Any]:
    jobs = _comparison_groups(request).get(comparison_id)
    if not jobs:
        raise HTTPException(status_code=404, detail="comparison not found")
    return {"id": comparison_id, "jobs": jobs_payload(jobs)}


@router.post("/comparisons/{comparison_id}/retry")
def retry_comparison(comparison_id: str, request: Request) -> dict[str, int]:
    service = service_from_request(request)
    jobs = _comparison_groups(request).get(comparison_id)
    if not jobs:
        raise HTTPException(status_code=404, detail="comparison not found")
    return {"updated": service.retry_jobs([job.id for job in jobs])}


@router.post("/comparisons/{comparison_id}/rerun", status_code=201)
def rerun_comparison(
    comparison_id: str,
    payload: ComparisonRerunRequest,
    request: Request,
) -> dict[str, Any]:
    service = service_from_request(request)
    jobs = _comparison_groups(request).get(comparison_id)
    if not jobs:
        raise HTTPException(status_code=404, detail="comparison not found")
    try:
        new_id, new_jobs = service.create_transcription_comparison(
            list(dict.fromkeys(job.source_rel for job in jobs)),
            options=payload.options,
            reuse_audio_from=jobs,
            parent_comparison_id=comparison_id,
        )
    except (OSError, ValueError) as error:
        raise bad_request(error) from error
    return {
        "id": new_id,
        "jobs": jobs_payload(new_jobs),
        "reused_audio": sum(
            COMPARISON_AUDIO_SOURCE_JOB_ID_OPTION in job.options for job in new_jobs
        ),
    }


@router.post("/comparisons/{comparison_id}/translate", status_code=201)
def translate_comparison(
    comparison_id: str,
    payload: ComparisonTranslationRequest,
    request: Request,
) -> dict[str, Any]:
    try:
        jobs = service_from_request(request).create_comparison_translation_jobs(
            comparison_id,
            payload.job_ids,
            prompt_category_id=payload.prompt_category_id,
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise bad_request(error) from error
    return {"items": jobs_payload(jobs), "created": len(jobs)}
