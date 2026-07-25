"""Deterministic Korean SRT rendering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
from typing import Any

from .contracts import validate_translation_items


def format_srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def render_srt(
    transcript_segments: Sequence[Mapping[str, Any]],
    translation_items: Sequence[Mapping[str, Any]],
) -> str:
    expected_ids = [str(segment["id"]) for segment in transcript_segments]
    translations = validate_translation_items(translation_items, expected_ids)
    text_by_id = {item["id"]: item["text"] for item in translations}

    blocks: list[str] = []
    for index, segment in enumerate(transcript_segments, start=1):
        segment_id = str(segment["id"])
        start = float(segment["start"])
        end = max(start, float(segment["end"]))
        text = text_by_id[segment_id].replace("\r\n", "\n").replace("\r", "\n")
        blocks.append(
            f"{index}\n"
            f"{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n"
            f"{text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def write_srt_atomic(
    path: Path,
    transcript_segments: Sequence[Mapping[str, Any]],
    translation_items: Sequence[Mapping[str, Any]],
    *,
    overwrite: bool,
) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"subtitle already exists: {path}")
    content = render_srt(transcript_segments, translation_items)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() and not overwrite:
            raise FileExistsError(f"subtitle already exists: {path}")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
