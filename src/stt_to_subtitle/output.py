"""Transcript output serialization."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence


def format_timestamp(seconds: float) -> str:
    milliseconds = round(max(0.0, seconds) * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def write_outputs(
    output_base: Path,
    metadata: Mapping[str, Any],
    segments: Sequence[Mapping[str, Any]],
    transcripts: Mapping[str, str],
) -> tuple[Path, Path]:
    """Write machine-readable JSON and a timestamped plain-text transcript."""
    json_path = output_base.parent / f"{output_base.name}.json"
    text_path = output_base.parent / f"{output_base.name}.txt"
    payload = {
        "schema_version": 1,
        **metadata,
        "segments": list(segments),
        "speaker_transcripts": dict(transcripts),
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    lines = [
        (
            f"[{format_timestamp(float(segment['start']))} --> "
            f"{format_timestamp(float(segment['end']))}] "
            f"{segment['speaker']}: {segment['text']}"
        )
        for segment in segments
    ]
    text_path.write_text(
        "\n".join(lines) + ("\n" if lines else ""),
        encoding="utf-8",
    )
    return json_path, text_path
