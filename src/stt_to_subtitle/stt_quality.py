"""Versioned STT quality metrics and non-destructive diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
import unicodedata
from typing import Any

NORMALIZATION_VERSION = "normalization-v1"
SIMILARITY_VERSION = "sequence-matcher-v1"
DUPLICATE_METRIC_VERSION = "overlap-dup-v1"
REPETITION_RULE_VERSION = "repetition-v1"


def normalize_transcript(text: str) -> str:
    """Normalize transcript text without discarding the Japanese long mark."""
    normalized = unicodedata.normalize("NFKC", text)
    return "".join(
        character
        for character in normalized
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def transcript_similarity(base: str, candidate: str) -> float:
    """Measure transcript stability; this is not an accuracy or CER score."""
    return SequenceMatcher(
        None,
        normalize_transcript(base),
        normalize_transcript(candidate),
        autojunk=False,
    ).ratio()


def interval_durations(
    spans: Sequence[Mapping[str, Any]],
) -> tuple[float, float]:
    """Return the summed and union duration of timestamped spans."""
    intervals: list[tuple[float, float]] = []
    for span in spans:
        try:
            start = float(span["start"])
            end = float(span["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            intervals.append((start, end))
    duration_sum = sum(end - start for start, end in intervals)
    if not intervals:
        return round(duration_sum, 3), 0.0

    intervals.sort()
    union_duration = 0.0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        union_duration += current_end - current_start
        current_start, current_end = start, end
    union_duration += current_end - current_start
    return round(duration_sum, 3), round(union_duration, 3)


def annotate_span_diagnostics(
    spans: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Add duration and neighbor context without changing span decisions."""
    ordered = sorted(
        enumerate(spans),
        key=lambda item: (
            float(item[1].get("start", 0.0)),
            float(item[1].get("end", 0.0)),
            item[0],
        ),
    )
    diagnostics: list[dict[str, Any]] = []
    for position, (original_index, span) in enumerate(ordered):
        try:
            start = float(span["start"])
            end = float(span["end"])
        except (KeyError, TypeError, ValueError):
            continue
        duration = max(0.0, end - start)
        previous = ordered[position - 1][1] if position > 0 else None
        following = (
            ordered[position + 1][1]
            if position + 1 < len(ordered)
            else None
        )
        speaker = str(span.get("speaker", "UNKNOWN"))
        diagnostics.append(
            {
                **dict(span),
                "span_id": str(
                    span.get("span_id", f"span-{original_index + 1:06d}")
                ),
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(duration, 3),
                "is_short_span": duration < 0.2,
                "short_span_bucket": (
                    "lt_0_2"
                    if duration < 0.2
                    else "0_2_to_0_5"
                    if duration < 0.5
                    else "gte_0_5"
                ),
                "prev_gap_sec": (
                    round(max(0.0, start - float(previous["end"])), 3)
                    if previous is not None
                    else None
                ),
                "next_gap_sec": (
                    round(max(0.0, float(following["start"]) - end), 3)
                    if following is not None
                    else None
                ),
                "same_speaker_prev": (
                    str(previous.get("speaker", "UNKNOWN")) == speaker
                    if previous is not None
                    else None
                ),
                "same_speaker_next": (
                    str(following.get("speaker", "UNKNOWN")) == speaker
                    if following is not None
                    else None
                ),
                "rms_energy": None,
                "vad_score": None,
            }
        )
    return diagnostics


def short_span_diagnostics(
    spans: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return only spans shorter than 0.5 seconds for compact summaries."""
    return [
        span
        for span in annotate_span_diagnostics(spans)
        if float(span["duration"]) < 0.5
    ]


def find_replacement_chars(text: str) -> list[int]:
    """Return every U+FFFD position without modifying the source text."""
    return [index for index, character in enumerate(text) if character == "\ufffd"]


def replacement_char_occurrences(value: Any, path: str = "$") -> list[dict[str, Any]]:
    """Find U+FFFD occurrences in a JSON-like value."""
    if isinstance(value, str):
        positions = find_replacement_chars(value)
        return [{"path": path, "positions": positions}] if positions else []
    if isinstance(value, Mapping):
        occurrences: list[dict[str, Any]] = []
        for key, item in value.items():
            occurrences.extend(
                replacement_char_occurrences(item, f"{path}.{key}")
            )
        return occurrences
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        occurrences = []
        for index, item in enumerate(value):
            occurrences.extend(
                replacement_char_occurrences(item, f"{path}[{index}]")
            )
        return occurrences
    return []


def repetition_diagnostics(
    texts: Sequence[str],
    *,
    minimum_count: int = 8,
    maximum_ngram_length: int = 12,
) -> dict[str, Any]:
    """Find the longest consecutive repeated substring without altering text."""
    if minimum_count < 2:
        raise ValueError("minimum_count must be at least 2")
    if maximum_ngram_length < 1:
        raise ValueError("maximum_ngram_length must be positive")

    best_ngram = ""
    best_count = 1
    best_ratio = 0.0
    for text in texts:
        if not text:
            continue
        length = len(text)
        for start in range(length):
            remaining = length - start
            for size in range(1, min(maximum_ngram_length, remaining // 2) + 1):
                if remaining // size <= best_count:
                    continue
                ngram = text[start : start + size]
                count = 1
                cursor = start + size
                while text[cursor : cursor + size] == ngram:
                    count += 1
                    cursor += size
                ratio = (count * size) / length
                if count > best_count or (
                    count == best_count and ratio > best_ratio
                ):
                    best_ngram = ngram
                    best_count = count
                    best_ratio = ratio

    return {
        "max_consecutive_token_run": best_count,
        "max_repeated_ngram": best_ngram or None,
        "max_repeated_ngram_count": best_count,
        "repeated_text_ratio": round(best_ratio, 6),
        "flagged": best_count >= minimum_count,
        "minimum_count": minimum_count,
        "rule_version": REPETITION_RULE_VERSION,
    }


def overlap_duplicate_metrics(
    segments: Sequence[Mapping[str, Any]],
    *,
    similarity_threshold: float = 0.6,
) -> dict[str, Any]:
    """Calculate versioned overlap duplicate pairs and components."""
    if not 0.0 <= similarity_threshold <= 1.0:
        raise ValueError("similarity_threshold must be between 0 and 1")
    prepared: list[dict[str, Any]] = []
    for index, segment in enumerate(segments):
        try:
            start = float(segment["start"])
            end = float(segment["end"])
        except (KeyError, TypeError, ValueError):
            continue
        normalized = normalize_transcript(str(segment.get("text", "")))
        if end <= start or not normalized:
            continue
        prepared.append(
            {
                "index": index,
                "id": str(segment.get("id", f"segment-{index + 1:06d}")),
                "start": start,
                "end": end,
                "speaker": str(segment.get("speaker", "UNKNOWN")),
                "text": normalized,
            }
        )
    prepared.sort(key=lambda item: (item["start"], item["end"], item["id"]))

    adjacency: dict[int, set[int]] = {}
    pair_count = 0
    same_speaker_pair_count = 0
    for left_index, left in enumerate(prepared):
        for right_index in range(left_index + 1, len(prepared)):
            right = prepared[right_index]
            if right["start"] >= left["end"]:
                break
            overlap = min(left["end"], right["end"]) - max(
                left["start"], right["start"]
            )
            if overlap <= 0:
                continue
            similarity = SequenceMatcher(
                None,
                left["text"],
                right["text"],
                autojunk=False,
            ).ratio()
            if similarity < similarity_threshold:
                continue
            pair_count += 1
            if left["speaker"] == right["speaker"]:
                same_speaker_pair_count += 1
            adjacency.setdefault(left_index, set()).add(right_index)
            adjacency.setdefault(right_index, set()).add(left_index)

    visited: set[int] = set()
    cluster_count = 0
    duplicate_nodes: set[int] = set()
    for node in adjacency:
        if node in visited:
            continue
        cluster_count += 1
        pending = [node]
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            duplicate_nodes.add(current)
            pending.extend(adjacency.get(current, set()) - visited)

    return {
        "duplicate_metric_version": DUPLICATE_METRIC_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "similarity_version": SIMILARITY_VERSION,
        "similarity_threshold": similarity_threshold,
        "pair_count": pair_count,
        "cluster_count": cluster_count,
        "unique_segment_count": (
            len(prepared) - len(duplicate_nodes) + cluster_count
        ),
        "same_speaker_pair_count": same_speaker_pair_count,
        "cross_speaker_pair_count": pair_count - same_speaker_pair_count,
    }
