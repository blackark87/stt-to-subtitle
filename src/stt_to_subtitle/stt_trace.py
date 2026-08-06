"""Opt-in stage artifact recording for native STT diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .files import write_json_atomic
from .stt_quality import replacement_char_occurrences

TRACE_SCHEMA_VERSION = "stt-trace-v1"


def json_safe(value: Any) -> Any:
    """Convert common model result values into deterministic JSON data."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if is_dataclass(value):
        return json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [json_safe(item) for item in value]
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return json_safe(item())
        except (TypeError, ValueError):
            pass
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        try:
            return json_safe(tolist())
        except (TypeError, ValueError):
            pass
    return str(value)


class StageArtifactRecorder:
    """Track encoding warnings and optionally persist stage payloads."""

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        self.first_replacement_stage: str | None = None
        self.replacement_occurrences: list[dict[str, Any]] = []

    @property
    def enabled(self) -> bool:
        return self.directory is not None

    def record(self, filename: str, stage: str, payload: Any) -> None:
        safe_payload = json_safe(payload)
        occurrences = replacement_char_occurrences(safe_payload)
        if occurrences and self.first_replacement_stage is None:
            self.first_replacement_stage = stage
        for occurrence in occurrences:
            self.replacement_occurrences.append(
                {"stage": stage, **occurrence}
            )
        if self.directory is None:
            return
        write_json_atomic(
            self.directory / filename,
            {
                "trace_schema_version": TRACE_SCHEMA_VERSION,
                "stage": stage,
                "encoding_warning": {
                    "contains_replacement_char": bool(occurrences),
                    "occurrences": occurrences,
                    "first_detected_stage": self.first_replacement_stage,
                },
                "payload": safe_payload,
            },
        )

    def encoding_warning(self) -> dict[str, Any]:
        return {
            "contains_replacement_char": bool(self.replacement_occurrences),
            "first_detected_stage": self.first_replacement_stage,
            "occurrences": list(self.replacement_occurrences),
        }
