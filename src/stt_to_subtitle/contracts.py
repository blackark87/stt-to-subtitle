"""Stable JSON contracts shared by the transcription and NAS services."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

TRANSCRIPT_SCHEMA_VERSION = 1
TRANSLATION_SCHEMA_VERSION = 1


def add_segment_ids(
    segments: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Copy normalized segments and add deterministic identifiers."""
    return [
        {
            "id": f"segment-{index:06d}",
            "start": float(segment["start"]),
            "end": float(segment["end"]),
            "speaker": str(segment.get("speaker", "UNKNOWN")),
            "text": str(segment["text"]),
        }
        for index, segment in enumerate(segments, start=1)
    ]


def validate_transcript(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate and normalize the segment portion of an STT API result."""
    if payload.get("schema_version") != TRANSCRIPT_SCHEMA_VERSION:
        raise ValueError("unsupported transcript schema_version")
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise ValueError("transcript segments must be a list")

    segments: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_segments):
        if not isinstance(item, Mapping):
            raise ValueError(f"transcript segment {index} must be an object")
        segment_id = str(item.get("id", "")).strip()
        text = str(item.get("text", "")).strip()
        if not segment_id or segment_id in seen_ids:
            raise ValueError(f"transcript segment {index} has an invalid id")
        if not text:
            raise ValueError(f"transcript segment {segment_id} has empty text")
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(
                f"transcript segment {segment_id} has invalid timestamps"
            ) from error
        if start < 0 or end < start:
            raise ValueError(
                f"transcript segment {segment_id} has invalid timestamps"
            )
        seen_ids.add(segment_id)
        segments.append(
            {
                "id": segment_id,
                "start": start,
                "end": end,
                "speaker": str(item.get("speaker", "UNKNOWN")),
                "text": text,
            }
        )
    return segments


def validate_translation_items(
    items: Any,
    expected_ids: Sequence[str],
) -> list[dict[str, str]]:
    """Require exactly one non-empty Korean translation per requested id."""
    if not isinstance(items, list):
        raise ValueError("translation response must contain a translations list")

    translations: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"translation item {index} must be an object")
        segment_id = str(item.get("id", "")).strip()
        text = str(item.get("text", "")).strip()
        if not segment_id or segment_id in seen_ids:
            raise ValueError(f"translation item {index} has an invalid id")
        if not text:
            raise ValueError(f"translation item {segment_id} has empty text")
        seen_ids.add(segment_id)
        translations.append({"id": segment_id, "text": text})

    expected = list(expected_ids)
    received = [item["id"] for item in translations]
    if received != expected:
        raise ValueError(
            "translation response ids do not exactly match the request"
        )
    return translations
