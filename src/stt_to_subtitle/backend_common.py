"""Shared Backend HTTP serialization and lookup helpers."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from fastapi import HTTPException, Request

from .job_store import PipelineJob
from .orchestrator import SubtitleOrchestrator
from .time_display import format_kst_iso


def service_from_request(request: Request) -> SubtitleOrchestrator:
    service = getattr(request.app.state, "orchestrator", None)
    if not isinstance(service, SubtitleOrchestrator):
        raise HTTPException(status_code=503, detail="backend is not ready")
    return service


def public_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return public_value(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): public_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list | set):
        return [public_value(item) for item in value]
    return value


def job_payload(job: PipelineJob) -> dict[str, Any]:
    payload = asdict(job)
    payload["created_at"] = format_kst_iso(job.created_at)
    payload["updated_at"] = format_kst_iso(job.updated_at)
    return public_value(payload)


def jobs_payload(jobs: Sequence[PipelineJob]) -> list[dict[str, Any]]:
    return [job_payload(job) for job in jobs]


def require_job(service: SubtitleOrchestrator, job_id: str) -> PipelineJob:
    job = service.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


def bad_request(error: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(error))
