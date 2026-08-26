"""Stable internal-stage progress shared by STT workers and the web UI."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .files import write_json_atomic


TRANSCRIPTION_STAGE_LABELS = {
    "model_loading": "모델 준비",
    "scene_detection": "장면 분석",
    "primary_transcription": "1차 전사",
    "secondary_transcription": "2차 전사",
    "forced_alignment": "강제 정렬",
    "speaker_diarization": "화자 분리",
    "quality_analysis": "문제 구간 분석",
    "rescue_transcription": "문제 구간 재전사",
    "transcription_merge": "전사 결과 병합",
    "subtitle_normalization": "자막 구간 구성",
}

StageProgressCallback = Callable[[str, int, int], None]


def validate_stage_progress(
    stage: object,
    index: object,
    total: object,
) -> tuple[str, int, int]:
    stage_value = str(stage).strip()
    try:
        index_value = int(index)
        total_value = int(total)
    except (TypeError, ValueError) as error:
        raise ValueError("invalid transcription stage progress") from error
    if stage_value not in TRANSCRIPTION_STAGE_LABELS:
        raise ValueError("unsupported transcription stage")
    if total_value < 1 or not 1 <= index_value <= total_value:
        raise ValueError("invalid transcription stage position")
    return stage_value, index_value, total_value


def report_stage_progress(
    callback: StageProgressCallback | None,
    stage: str,
    index: int,
    total: int,
) -> None:
    if callback is None:
        return
    callback(*validate_stage_progress(stage, index, total))


def write_stage_progress(
    path: Path | None,
    stage: str,
    index: int,
    total: int,
) -> None:
    if path is None:
        return
    stage_value, index_value, total_value = validate_stage_progress(
        stage,
        index,
        total,
    )
    write_json_atomic(
        path,
        {
            "stage": stage_value,
            "index": index_value,
            "total": total_value,
        },
    )


def parse_stage_progress(payload: Mapping[str, Any]) -> tuple[str, int, int]:
    return validate_stage_progress(
        payload.get("stage"),
        payload.get("index"),
        payload.get("total"),
    )
