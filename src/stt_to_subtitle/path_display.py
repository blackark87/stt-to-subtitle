"""Configurable display-only shortening for persisted media paths."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Mapping, Sequence

PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)\}")


@dataclass(frozen=True)
class PathDisplayRule:
    id: str
    source_pattern: str
    display_pattern: str
    created_at: float
    updated_at: float


def _normalize_pattern(value: str, label: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or normalized.startswith("/")
        or normalized.endswith("/")
        or "\\" in normalized
    ):
        raise ValueError(f"{label}은 슬래시 없는 상대 경로여야 합니다.")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{label}에 빈 단계나 점 경로를 사용할 수 없습니다.")
    without_placeholders = PLACEHOLDER_PATTERN.sub("", normalized)
    if "{" in without_placeholders or "}" in without_placeholders:
        raise ValueError(f"{label}의 변수 형식은 {{이름}}이어야 합니다.")
    return normalized


def normalize_path_display_patterns(
    source_pattern: str,
    display_pattern: str,
) -> tuple[str, str]:
    source = _normalize_pattern(source_pattern, "원본 패턴")
    display = _normalize_pattern(display_pattern, "표시 패턴")
    source_names = set(PLACEHOLDER_PATTERN.findall(source))
    if not source_names:
        raise ValueError("원본 패턴에는 하나 이상의 {변수}가 필요합니다.")
    unknown_names = set(PLACEHOLDER_PATTERN.findall(display)) - source_names
    if unknown_names:
        names = ", ".join(sorted(unknown_names))
        raise ValueError(f"표시 패턴에 원본에 없는 변수가 있습니다: {names}")
    if source == display:
        raise ValueError("원본 패턴과 표시 패턴이 같습니다.")
    return source, display


@lru_cache(maxsize=128)
def _compiled_source_pattern(pattern: str) -> re.Pattern[str]:
    chunks: list[str] = []
    captured: set[str] = set()
    position = 0
    for match in PLACEHOLDER_PATTERN.finditer(pattern):
        chunks.append(re.escape(pattern[position : match.start()]))
        name = match.group(1)
        if name in captured:
            chunks.append(f"(?P={name})")
        else:
            chunks.append(f"(?P<{name}>[^/]+)")
            captured.add(name)
        position = match.end()
    chunks.append(re.escape(pattern[position:]))
    return re.compile("^" + "".join(chunks) + "$", re.IGNORECASE)


def shorten_display_path(
    value: object,
    rules: Sequence[PathDisplayRule | Mapping[str, object]],
) -> str:
    path = str(value)
    candidates = [(path, "")]
    if path.startswith("/"):
        absolute = path[1:]
        candidates.append((absolute, "/"))
        mount, separator, relative = absolute.partition("/")
        if separator and relative:
            candidates.append((relative, f"/{mount}/"))
    for rule in rules:
        if isinstance(rule, Mapping):
            source_pattern = str(rule["source_pattern"])
            display_pattern = str(rule["display_pattern"])
        else:
            source_pattern = rule.source_pattern
            display_pattern = rule.display_pattern
        compiled = _compiled_source_pattern(source_pattern)
        for candidate, preserved_prefix in candidates:
            matched = compiled.fullmatch(candidate)
            if matched is None:
                continue
            values = matched.groupdict()
            shortened = PLACEHOLDER_PATTERN.sub(
                lambda token: values[token.group(1)],
                display_pattern,
            )
            return preserved_prefix + shortened
    return path
