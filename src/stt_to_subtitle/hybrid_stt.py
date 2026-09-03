"""Structural quality rescue for combined WhisperX and Kotoba transcripts."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from functools import lru_cache
from math import isfinite
from typing import Any

from .stt_quality import normalize_transcript, repetition_diagnostics
from .stt_options import HybridRescueOptions, OWSMAuditOptions, RESCUE_SCOPES

HYBRID_POLICY_VERSION = "hybrid-rescue-v1"
RECALL_UNION_POLICY_VERSION = "hybrid-recall-union-v1"
MIN_SPEAKER_MAPPING_CONFIDENCE = 0.5
# "full" decodes the whole file with Kotoba; "windows" decodes only the
# padded rescue spans. Window scope diarizes each span in isolation, so
# speaker labels are local to the window. Keep "full" as an explicit
# compatibility option, but prefer the faster window-scoped rescue.
FATAL_ISSUE_CODES = {
    "INVALID_WORD_TIMESTAMP",
    "LONG_WORD_ALIGNMENT",
    "MISSING_WORD_TIMESTAMP",
    "REPEATED_TRANSCRIPT",
    "REPLACEMENT_CHARACTER",
    "OWSM_TEXT_COVERAGE_GAP",
}


def detect_owsm_coverage_issues(
    audit_windows: Sequence[Mapping[str, Any]],
    primary_words: Sequence[Mapping[str, Any]],
    primary_segments: Sequence[Mapping[str, Any]],
    *,
    options: OWSMAuditOptions,
) -> list[dict[str, Any]]:
    """Flag windows where OWSM hears materially more text than WhisperX.

    OWSM is only an omission detector. Its text has no subtitle-safe word
    timestamps, so a flagged span is sent through the existing Kotoba rescue
    path instead of being inserted into the transcript directly.
    """
    primary_records = primary_words or primary_segments
    text_key = "word" if primary_words else "text"
    issues: list[dict[str, Any]] = []
    for window in audit_windows:
        span = _span(window)
        if span is None:
            continue
        start, end = span
        overlapping = [
            record
            for record in primary_records
            if (record_span := _span(record)) is not None
            and record_span[1] > start
            and record_span[0] < end
        ]
        primary_text = "".join(
            str(record.get(text_key, "")) for record in overlapping
        )
        audit_text = str(window.get("text", ""))
        primary_length = len(normalize_transcript(primary_text))
        audit_length = len(normalize_transcript(audit_text))
        extra_characters = audit_length - primary_length
        length_ratio = audit_length / max(primary_length, 1)
        if (
            extra_characters < options.minimum_extra_characters
            or length_ratio < options.minimum_length_ratio
        ):
            continue
        issues.append(
            _issue(
                sequence=len(issues) + 1,
                start=start,
                end=end,
                reason_code="OWSM_TEXT_COVERAGE_GAP",
                segments=([] if primary_words else overlapping),
                words=(overlapping if primary_words else []),
                evidence={
                    "primary_normalized_characters": primary_length,
                    "owsm_normalized_characters": audit_length,
                    "extra_characters": extra_characters,
                    "length_ratio": round(length_ratio, 6),
                    "owsm_text": audit_text,
                },
            )
        )
    return issues


def _timestamp(value: Any) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if isfinite(converted) else None


def _span(record: Mapping[str, Any]) -> tuple[float, float] | None:
    start = _timestamp(record.get("start"))
    end = _timestamp(record.get("end"))
    if start is None or end is None or end < start:
        return None
    return start, end


def _record_ids(
    records: Sequence[Mapping[str, Any]],
    key: str,
) -> list[str]:
    return [str(record[key]) for record in records if record.get(key)]


def _issue(
    *,
    sequence: int,
    start: float,
    end: float,
    reason_code: str,
    segments: Sequence[Mapping[str, Any]] = (),
    words: Sequence[Mapping[str, Any]] = (),
    evidence: Mapping[str, Any] | None = None,
    severity: str = "fatal",
) -> dict[str, Any]:
    normalized_start = max(0.0, start)
    normalized_end = max(normalized_start, end)
    return {
        "issue_id": f"issue-{sequence:06d}",
        "start": round(normalized_start, 3),
        "end": round(normalized_end, 3),
        "severity": severity,
        "reason_codes": [reason_code],
        "segment_ids": _record_ids(segments, "id"),
        "word_ids": _record_ids(words, "word_id"),
        "evidence": dict(evidence or {}),
        "rule_version": HYBRID_POLICY_VERSION,
    }


def debounce_word_speakers(
    words: Sequence[Mapping[str, Any]],
    *,
    maximum_flash_duration_sec: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Relabel only a short A-B-A word-speaker flash; retain its lineage."""
    if maximum_flash_duration_sec <= 0:
        raise ValueError("maximum_flash_duration_sec must be positive")
    ordered = [
        dict(word)
        for word in sorted(
            words,
            key=lambda word: (
                float(word.get("start", 0.0)),
                float(word.get("end", 0.0)),
                str(word.get("word_id", "")),
            ),
        )
    ]
    if len(ordered) < 3:
        return ordered, []

    runs: list[dict[str, Any]] = []
    for index, word in enumerate(ordered):
        speaker = str(word.get("speaker", "UNKNOWN"))
        span = _span(word)
        if span is None:
            continue
        if runs and runs[-1]["speaker"] == speaker:
            runs[-1]["last_index"] = index
            runs[-1]["end"] = max(runs[-1]["end"], span[1])
        else:
            runs.append(
                {
                    "speaker": speaker,
                    "first_index": index,
                    "last_index": index,
                    "start": span[0],
                    "end": span[1],
                }
            )

    changes: list[dict[str, Any]] = []
    for position in range(1, len(runs) - 1):
        previous, current, following = runs[position - 1 : position + 2]
        duration = current["end"] - current["start"]
        if (
            previous["speaker"] != following["speaker"]
            or current["speaker"] == previous["speaker"]
            or duration > maximum_flash_duration_sec
            or current["start"] < previous["end"]
            or following["start"] < current["end"]
        ):
            continue
        changed_word_ids: list[str] = []
        for index in range(current["first_index"], current["last_index"] + 1):
            word = ordered[index]
            word["original_speaker"] = str(word.get("speaker", "UNKNOWN"))
            word["speaker"] = previous["speaker"]
            reason_codes = list(word.get("reason_codes", []))
            if "HYBRID_SPEAKER_DEBOUNCE" not in reason_codes:
                reason_codes.append("HYBRID_SPEAKER_DEBOUNCE")
            word["reason_codes"] = reason_codes
            if word.get("word_id"):
                changed_word_ids.append(str(word["word_id"]))
        changes.append(
            {
                "start": round(current["start"], 3),
                "end": round(current["end"], 3),
                "from_speaker": current["speaker"],
                "to_speaker": previous["speaker"],
                "word_ids": changed_word_ids,
                "rule_version": HYBRID_POLICY_VERSION,
            }
        )
    return ordered, changes


def detect_hybrid_issues(
    segments: Sequence[Mapping[str, Any]],
    words: Sequence[Mapping[str, Any]],
    *,
    options: HybridRescueOptions,
    repetition_min_count: int,
    maximum_repetition_gap_sec: float = 1.0,
) -> list[dict[str, Any]]:
    """Locate structural failures without claiming semantic accuracy."""
    issues: list[dict[str, Any]] = []

    def append_issue(**kwargs: Any) -> None:
        issues.append(_issue(sequence=len(issues) + 1, **kwargs))

    ordered_segments = [
        segment
        for segment in sorted(
            segments,
            key=lambda segment: (
                float(segment.get("start", 0.0)),
                float(segment.get("end", 0.0)),
                str(segment.get("id", "")),
            ),
        )
        if _span(segment) is not None
    ]
    for segment in ordered_segments:
        span = _span(segment)
        assert span is not None
        text = str(segment.get("text", ""))
        if "\ufffd" in text:
            append_issue(
                start=span[0],
                end=span[1],
                reason_code="REPLACEMENT_CHARACTER",
                segments=[segment],
                evidence={
                    "positions": [
                        index for index, value in enumerate(text) if value == "\ufffd"
                    ]
                },
            )
        repetition = repetition_diagnostics(
            [normalize_transcript(text)],
            minimum_count=repetition_min_count,
        )
        if repetition["flagged"]:
            append_issue(
                start=span[0],
                end=span[1],
                reason_code="REPEATED_TRANSCRIPT",
                segments=[segment],
                evidence=repetition,
            )

    for index, first in enumerate(ordered_segments):
        first_span = _span(first)
        assert first_span is not None
        group: list[Mapping[str, Any]] = [first]
        text = normalize_transcript(str(first.get("text", "")))
        previous_end = first_span[1]
        for following in ordered_segments[index + 1 : index + 12]:
            following_span = _span(following)
            assert following_span is not None
            if following_span[0] - previous_end > maximum_repetition_gap_sec:
                break
            if following_span[1] - first_span[0] > 30.0:
                break
            group.append(following)
            text += normalize_transcript(str(following.get("text", "")))
            previous_end = max(previous_end, following_span[1])
            if len(group) < 2:
                continue
            repetition = repetition_diagnostics(
                [text], minimum_count=repetition_min_count
            )
            if repetition["flagged"]:
                append_issue(
                    start=first_span[0],
                    end=previous_end,
                    reason_code="REPEATED_TRANSCRIPT",
                    segments=group,
                    evidence={**repetition, "scope": "cross_segment"},
                )
                break

    for word in words:
        start = _timestamp(word.get("start"))
        end = _timestamp(word.get("end"))
        if start is None or end is None or end < start:
            point = max(0.0, start or end or 0.0)
            append_issue(
                start=point,
                end=point,
                reason_code="INVALID_WORD_TIMESTAMP",
                words=[word],
                evidence={"start": start, "end": end},
            )
            continue
        if word.get("timestamp_fallback") is True:
            append_issue(
                start=start,
                end=end,
                reason_code="MISSING_WORD_TIMESTAMP",
                words=[word],
                evidence={
                    "timestamp_source": str(
                        word.get("timestamp_source", "unknown")
                    ),
                    "word": str(word.get("word", "")),
                },
            )
        duration = end - start
        if duration > options.max_word_duration_sec:
            append_issue(
                start=start,
                end=end,
                reason_code="LONG_WORD_ALIGNMENT",
                words=[word],
                evidence={
                    "duration_sec": round(duration, 3),
                    "maximum_sec": options.max_word_duration_sec,
                    "word": str(word.get("word", "")),
                },
            )

    short_segments = [
        segment
        for segment in ordered_segments
        if (_span(segment) or (0.0, 0.0))[1]
        - (_span(segment) or (0.0, 0.0))[0]
        < options.short_segment_duration_sec
    ]
    index = 0
    while index < len(short_segments):
        first_span = _span(short_segments[index])
        assert first_span is not None
        cluster = [short_segments[index]]
        cursor = index + 1
        while cursor < len(short_segments):
            candidate_span = _span(short_segments[cursor])
            assert candidate_span is not None
            if (
                candidate_span[0] - first_span[0]
                > options.short_segment_cluster_window_sec
            ):
                break
            cluster.append(short_segments[cursor])
            cursor += 1
        if len(cluster) >= options.short_segment_cluster_count:
            final_span = _span(cluster[-1])
            assert final_span is not None
            append_issue(
                start=first_span[0],
                end=final_span[1],
                reason_code="MICRO_SEGMENT_CLUSTER",
                segments=cluster,
                evidence={
                    "count": len(cluster),
                    "maximum_duration_sec": options.short_segment_duration_sec,
                    "window_sec": options.short_segment_cluster_window_sec,
                },
                severity="warning",
            )
            index = cursor
        else:
            index += 1
    return issues


def merge_issue_windows(
    issues: Sequence[Mapping[str, Any]],
    *,
    padding_seconds: float,
    audio_duration: float,
) -> list[dict[str, Any]]:
    """Pad, clamp, and merge issue windows before transcript replacement."""
    if padding_seconds < 0:
        raise ValueError("padding_seconds must not be negative")
    if audio_duration < 0:
        raise ValueError("audio_duration must not be negative")
    prepared: list[dict[str, Any]] = []
    for issue in issues:
        if str(issue.get("severity", "fatal")) != "fatal":
            continue
        span = _span(issue)
        if span is None:
            continue
        start = max(0.0, min(audio_duration, span[0] - padding_seconds))
        end = max(
            start,
            min(audio_duration, span[1] + padding_seconds),
        )
        prepared.append(
            {
                "start": start,
                "end": end,
                "target_start": span[0],
                "target_end": span[1],
                "reason_codes": list(issue.get("reason_codes", [])),
                "issue_ids": [str(issue.get("issue_id", "unknown"))],
                "segment_ids": list(issue.get("segment_ids", [])),
                "word_ids": list(issue.get("word_ids", [])),
            }
        )
    prepared.sort(key=lambda window: (window["start"], window["end"]))
    merged: list[dict[str, Any]] = []
    for window in prepared:
        if not merged or window["start"] > merged[-1]["end"]:
            merged.append(dict(window))
            continue
        current = merged[-1]
        current["end"] = max(current["end"], window["end"])
        current["target_start"] = min(
            current["target_start"], window["target_start"]
        )
        current["target_end"] = max(
            current["target_end"], window["target_end"]
        )
        for key in ("reason_codes", "issue_ids", "segment_ids", "word_ids"):
            current[key] = list(dict.fromkeys([*current[key], *window[key]]))
    for index, window in enumerate(merged, start=1):
        window["window_id"] = f"rescue-window-{index:06d}"
        window["start"] = round(window["start"], 3)
        window["end"] = round(window["end"], 3)
        window["target_start"] = round(window["target_start"], 3)
        window["target_end"] = round(window["target_end"], 3)
        window["rule_version"] = HYBRID_POLICY_VERSION
    return merged


def _overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> float:
    left_span = _span(left)
    right_span = _span(right)
    if left_span is None or right_span is None:
        return 0.0
    return max(
        0.0,
        min(left_span[1], right_span[1])
        - max(left_span[0], right_span[0]),
    )


def _speaker_overlap_scores(
    primary_segments: Sequence[Mapping[str, Any]],
    fallback_segments: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, float]], list[str]]:
    """Accumulate temporal overlap with a sweep instead of an N x M scan."""
    scores: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    fallback_speakers = {
        str(segment.get("speaker", "UNKNOWN"))
        for segment in fallback_segments
    }
    primary = sorted(
        (
            segment
            for segment in primary_segments
            if _span(segment) is not None
        ),
        key=lambda segment: (_span(segment) or (0.0, 0.0))[0],
    )
    fallback = sorted(
        (
            segment
            for segment in fallback_segments
            if _span(segment) is not None
        ),
        key=lambda segment: (_span(segment) or (0.0, 0.0))[0],
    )
    left = 0
    for fallback_segment in fallback:
        fallback_span = _span(fallback_segment)
        assert fallback_span is not None
        while left < len(primary):
            primary_span = _span(primary[left])
            assert primary_span is not None
            if primary_span[1] > fallback_span[0]:
                break
            left += 1
        cursor = left
        fallback_speaker = str(
            fallback_segment.get("speaker", "UNKNOWN")
        )
        while cursor < len(primary):
            primary_segment = primary[cursor]
            primary_span = _span(primary_segment)
            assert primary_span is not None
            if primary_span[0] >= fallback_span[1]:
                break
            overlap = _overlap(fallback_segment, primary_segment)
            if overlap > 0:
                scores[fallback_speaker][
                    str(primary_segment.get("speaker", "UNKNOWN"))
                ] += overlap
            cursor += 1
    return {
        speaker: dict(candidates) for speaker, candidates in scores.items()
    }, sorted(fallback_speakers)


def _one_to_one_speaker_assignment(
    scores: Mapping[str, Mapping[str, float]],
    fallback_speakers: Sequence[str],
) -> dict[str, str]:
    primary_speakers = sorted(
        {
            primary_speaker
            for fallback_speaker in fallback_speakers
            for primary_speaker, score in scores.get(
                fallback_speaker, {}
            ).items()
            if score > 0
            and score
            / sum(scores.get(fallback_speaker, {}).values())
            >= MIN_SPEAKER_MAPPING_CONFIDENCE
        }
    )
    if len(primary_speakers) > 12:
        assignment: dict[str, str] = {}
        used_fallback: set[str] = set()
        used_primary: set[str] = set()
        pairs = sorted(
            (
                (-score, fallback_speaker, primary_speaker)
                for fallback_speaker in fallback_speakers
                for primary_speaker, score in scores.get(
                    fallback_speaker, {}
                ).items()
                if score > 0
                and score
                / sum(scores.get(fallback_speaker, {}).values())
                >= MIN_SPEAKER_MAPPING_CONFIDENCE
            )
        )
        for _negative_score, fallback_speaker, primary_speaker in pairs:
            if (
                fallback_speaker in used_fallback
                or primary_speaker in used_primary
            ):
                continue
            assignment[fallback_speaker] = primary_speaker
            used_fallback.add(fallback_speaker)
            used_primary.add(primary_speaker)
        return assignment

    sentinel = len(primary_speakers)

    @lru_cache(maxsize=None)
    def solve(
        fallback_index: int,
        used_mask: int,
    ) -> tuple[float, int, tuple[int, ...]]:
        if fallback_index >= len(fallback_speakers):
            return 0.0, 0, ()
        fallback_speaker = fallback_speakers[fallback_index]
        tail_score, tail_count, tail_choices = solve(
            fallback_index + 1, used_mask
        )
        best = (tail_score, tail_count, (sentinel, *tail_choices))
        for primary_index, primary_speaker in enumerate(primary_speakers):
            score = float(
                scores.get(fallback_speaker, {}).get(primary_speaker, 0.0)
            )
            total_score = sum(scores.get(fallback_speaker, {}).values())
            if (
                score <= 0
                or score / total_score < MIN_SPEAKER_MAPPING_CONFIDENCE
                or used_mask & (1 << primary_index)
            ):
                continue
            tail_score, tail_count, tail_choices = solve(
                fallback_index + 1,
                used_mask | (1 << primary_index),
            )
            candidate = (
                score + tail_score,
                tail_count + 1,
                (primary_index, *tail_choices),
            )
            if candidate[0] > best[0] + 1e-9 or (
                abs(candidate[0] - best[0]) <= 1e-9
                and (
                    candidate[1] > best[1]
                    or (
                        candidate[1] == best[1]
                        and candidate[2] < best[2]
                    )
                )
            ):
                best = candidate
        return best

    _score, _count, choices = solve(0, 0)
    return {
        fallback_speaker: primary_speakers[primary_index]
        for fallback_speaker, primary_index in zip(
            fallback_speakers, choices, strict=True
        )
        if primary_index != sentinel
    }


def _speaker_mapping_with_diagnostics(
    primary_segments: Sequence[Mapping[str, Any]],
    fallback_segments: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    scores, fallback_speakers = _speaker_overlap_scores(
        primary_segments, fallback_segments
    )
    assignment = _one_to_one_speaker_assignment(scores, fallback_speakers)
    mapping: dict[str, str] = {}
    diagnostics: list[dict[str, Any]] = []
    for fallback_speaker in fallback_speakers:
        candidates = scores.get(fallback_speaker, {})
        mapped_speaker = assignment.get(
            fallback_speaker, f"KOTOBA_{fallback_speaker}"
        )
        matched_overlap = float(candidates.get(mapped_speaker, 0.0))
        total_overlap = sum(float(value) for value in candidates.values())
        mapping[fallback_speaker] = mapped_speaker
        diagnostics.append(
            {
                "fallback_speaker": fallback_speaker,
                "mapped_speaker": mapped_speaker,
                "status": (
                    "assigned"
                    if fallback_speaker in assignment
                    else "unmapped"
                ),
                "matched_overlap_sec": round(matched_overlap, 3),
                "total_overlap_sec": round(total_overlap, 3),
                "confidence": round(
                    matched_overlap / total_overlap, 4
                )
                if total_overlap > 0
                else 0.0,
                "minimum_confidence": MIN_SPEAKER_MAPPING_CONFIDENCE,
            }
        )
    return mapping, diagnostics


def map_fallback_speakers(
    primary_segments: Sequence[Mapping[str, Any]],
    fallback_segments: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Map backend-local labels one-to-one, preserving unmatched speakers."""
    mapping, _diagnostics = _speaker_mapping_with_diagnostics(
        primary_segments, fallback_segments
    )
    return mapping


def _intersects(record: Mapping[str, Any], window: Mapping[str, Any]) -> bool:
    span = _span(record)
    window_span = _span(window)
    if span is None or window_span is None:
        return False
    if span[0] == span[1]:
        return window_span[0] <= span[0] < window_span[1]
    return span[0] < window_span[1] and window_span[0] < span[1]


def _snap_windows_to_primary(
    windows: Sequence[Mapping[str, Any]],
    primary_segments: Sequence[Mapping[str, Any]],
    *,
    audio_duration: float | None,
) -> list[dict[str, Any]]:
    snapped: list[dict[str, Any]] = []
    for window in windows:
        item = dict(window)
        segment_ids = {str(value) for value in window.get("segment_ids", [])}
        word_ids = {str(value) for value in window.get("word_ids", [])}
        explicit_hits = [
            segment
            for segment in primary_segments
            if str(segment.get("id", "")) in segment_ids
            or bool(
                word_ids.intersection(
                    str(value) for value in segment.get("word_ids", [])
                )
            )
        ]
        target_window = {
            "start": float(window.get("target_start", window["start"])),
            "end": float(window.get("target_end", window["end"])),
        }
        hits = explicit_hits or [
            segment
            for segment in primary_segments
            if _intersects(segment, target_window)
        ]
        spans = [_span(segment) for segment in hits]
        valid_spans = [span for span in spans if span is not None]
        if valid_spans:
            replacement_start = min(span[0] for span in valid_spans)
            replacement_end = max(span[1] for span in valid_spans)
        else:
            replacement_start = float(target_window["start"])
            replacement_end = float(target_window["end"])
        if audio_duration is not None:
            replacement_start = max(
                0.0, min(audio_duration, replacement_start)
            )
            replacement_end = max(
                replacement_start, min(audio_duration, replacement_end)
            )
            item["start"] = max(
                0.0, min(audio_duration, float(item["start"]))
            )
            item["end"] = max(
                float(item["start"]),
                min(audio_duration, float(item["end"])),
            )
        item["replacement_start"] = replacement_start
        item["replacement_end"] = replacement_end
        item["primary_segment_ids"] = _record_ids(hits, "id")
        item["primary_word_ids"] = list(
            dict.fromkeys(
                str(word_id)
                for segment in hits
                for word_id in segment.get("word_ids", [])
            )
        )
        snapped.append(item)
    snapped.sort(
        key=lambda window: (float(window["start"]), float(window["end"]))
    )
    for index, window in enumerate(snapped, start=1):
        window["window_id"] = f"rescue-window-{index:06d}"
        window["start"] = round(float(window["start"]), 3)
        window["end"] = round(float(window["end"]), 3)
        window["replacement_start"] = round(
            float(window["replacement_start"]), 3
        )
        window["replacement_end"] = round(
            float(window["replacement_end"]), 3
        )
    return snapped


def _midpoint_in_replacement(
    segment: Mapping[str, Any],
    window: Mapping[str, Any],
) -> bool:
    span = _span(segment)
    if span is None:
        return False
    midpoint = (span[0] + span[1]) / 2
    return (
        float(window["replacement_start"])
        <= midpoint
        < float(window["replacement_end"])
    )


def mark_rescued_words(
    words: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Mark primary words replaced by Kotoba without erasing diagnostics."""
    superseded: dict[str, list[str]] = defaultdict(list)
    for decision in decisions:
        if decision.get("outcome") != "replaced_with_kotoba":
            continue
        window_id = str(decision.get("window_id", "unknown"))
        for word_id in decision.get("superseded_word_ids", []):
            superseded[str(word_id)].append(window_id)
    output: list[dict[str, Any]] = []
    for source in words:
        word = dict(source)
        word_id = str(word.get("word_id", ""))
        window_ids = superseded.get(word_id, [])
        if window_ids:
            word["decision"] = "superseded"
            reason_codes = list(word.get("reason_codes", []))
            if "HYBRID_KOTOBA_RESCUE" not in reason_codes:
                reason_codes.append("HYBRID_KOTOBA_RESCUE")
            word["reason_codes"] = reason_codes
            word["superseded_by_window_ids"] = list(
                dict.fromkeys(window_ids)
            )
        output.append(word)
    return output


def _segment_words(
    segment: Mapping[str, Any],
    words_by_id: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]] | None:
    raw_word_ids = segment.get("word_ids", [])
    if not isinstance(raw_word_ids, Sequence) or isinstance(
        raw_word_ids, (str, bytes, bytearray)
    ):
        return None
    word_ids = [str(value) for value in raw_word_ids]
    if not word_ids or any(word_id not in words_by_id for word_id in word_ids):
        return None
    words = [words_by_id[word_id] for word_id in word_ids]
    if any(_span(word) is None for word in words):
        return None
    return words


def _fallback_intervals_by_speaker(
    candidates: Sequence[Mapping[str, Any]],
    speaker_mapping: Mapping[str, str],
) -> dict[str, list[dict[str, float]]]:
    intervals: dict[str, list[dict[str, float]]] = defaultdict(list)
    for candidate in candidates:
        span = _span(candidate)
        if span is None:
            continue
        fallback_speaker = str(candidate.get("speaker", "UNKNOWN"))
        mapped_speaker = speaker_mapping.get(
            fallback_speaker, fallback_speaker
        )
        intervals[mapped_speaker].append(
            {"start": span[0], "end": span[1]}
        )
    return dict(intervals)


def _word_intersects_any(
    word: Mapping[str, Any],
    intervals: Sequence[Mapping[str, Any]],
) -> bool:
    return any(_intersects(word, interval) for interval in intervals)


def _gap_crosses_interval(
    previous: Mapping[str, Any],
    following: Mapping[str, Any],
    intervals: Sequence[Mapping[str, Any]],
) -> bool:
    previous_span = _span(previous)
    following_span = _span(following)
    if previous_span is None or following_span is None:
        return False
    return any(
        (interval_span := _span(interval)) is not None
        and previous_span[1] <= interval_span[0]
        and interval_span[1] <= following_span[0]
        for interval in intervals
    )


def _residual_primary_segments(
    segment: Mapping[str, Any],
    words: Sequence[Mapping[str, Any]],
    intervals: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    kept_runs: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    removed_word_ids: list[str] = []
    for word in words:
        if _word_intersects_any(word, intervals):
            if current:
                kept_runs.append(current)
                current = []
            if word.get("word_id"):
                removed_word_ids.append(str(word["word_id"]))
            continue
        if current and _gap_crosses_interval(current[-1], word, intervals):
            kept_runs.append(current)
            current = []
        current.append(word)
    if current:
        kept_runs.append(current)

    source_id = str(segment.get("id", segment.get("span_id", "unknown")))
    residuals: list[dict[str, Any]] = []
    for index, run in enumerate(kept_runs, start=1):
        spans = [_span(word) for word in run]
        valid_spans = [span for span in spans if span is not None]
        text = "".join(str(word.get("word", "")) for word in run).strip()
        if not valid_spans or not text:
            continue
        reason_codes = list(segment.get("reason_codes", []))
        if "HYBRID_SAME_SPEAKER_BOUNDARY" not in reason_codes:
            reason_codes.append("HYBRID_SAME_SPEAKER_BOUNDARY")
        residual_id = f"{source_id}-residual-{index:02d}"
        residuals.append(
            {
                **dict(segment),
                "id": residual_id,
                "span_id": residual_id,
                "start": round(min(span[0] for span in valid_spans), 3),
                "end": round(max(span[1] for span in valid_spans), 3),
                "text": text,
                "word_ids": _record_ids(run, "word_id"),
                "parent_span_ids": list(
                    dict.fromkeys(
                        [*segment.get("parent_span_ids", []), source_id]
                    )
                ),
                "decision": "trim",
                "reason_codes": reason_codes,
            }
        )
    return residuals, removed_word_ids


def fuse_hybrid_segments(
    primary_segments: Sequence[Mapping[str, Any]],
    fallback_segments: Sequence[Mapping[str, Any]],
    windows: Sequence[Mapping[str, Any]],
    *,
    fallback_issues: Sequence[Mapping[str, Any]] = (),
    audio_duration: float | None = None,
    primary_words: Sequence[Mapping[str, Any]] = (),
    fallback_speakers_preassigned: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Replace only structurally failed primary windows with safe Kotoba spans."""
    snapped = _snap_windows_to_primary(
        windows,
        primary_segments,
        audio_duration=audio_duration,
    )
    if fallback_speakers_preassigned:
        fallback_speakers = sorted(
            {
                str(segment.get("speaker", "UNKNOWN"))
                for segment in fallback_segments
            }
        )
        speaker_mapping = {
            speaker: speaker for speaker in fallback_speakers
        }
        speaker_mapping_diagnostics = [
            {
                "fallback_speaker": speaker,
                "mapped_speaker": speaker,
                "status": "preassigned_per_window",
            }
            for speaker in fallback_speakers
        ]
    else:
        speaker_mapping, speaker_mapping_diagnostics = (
            _speaker_mapping_with_diagnostics(
                primary_segments,
                fallback_segments,
            )
        )
    words_by_id = {
        str(word["word_id"]): word
        for word in primary_words
        if word.get("word_id")
    }
    active: list[tuple[dict[str, Any], list[Mapping[str, Any]]]] = []
    decisions: list[dict[str, Any]] = []
    for window in snapped:
        candidates = [
            segment
            for segment in fallback_segments
            if _midpoint_in_replacement(segment, window)
        ]
        candidate_spans = [
            span for segment in candidates if (span := _span(segment)) is not None
        ]
        aligned_window = dict(window)
        if candidate_spans:
            aligned_window["replacement_start"] = min(
                float(window["replacement_start"]),
                *(span[0] for span in candidate_spans),
            )
            aligned_window["replacement_end"] = max(
                float(window["replacement_end"]),
                *(span[1] for span in candidate_spans),
            )
        fallback_out_of_bounds = bool(
            audio_duration is not None
            and any(
                span[0] < 0 or span[1] > audio_duration
                for span in candidate_spans
            )
        )
        applied_window = {
            "start": float(aligned_window["replacement_start"]),
            "end": float(aligned_window["replacement_end"]),
        }
        target_segment_ids = {
            str(value)
            for value in window.get("primary_segment_ids", [])
        }
        candidate_intervals = _fallback_intervals_by_speaker(
            candidates, speaker_mapping
        )
        boundary_segments = [
            primary
            for primary in primary_segments
            if str(primary.get("id", "")) not in target_segment_ids
            and any(
                _intersects(primary, interval)
                for interval in candidate_intervals.get(
                    str(primary.get("speaker", "UNKNOWN")), []
                )
            )
        ]
        unsafe_boundary_ids: list[str] = []
        boundary_word_ids: list[str] = []
        for boundary_segment in boundary_segments:
            segment_id = str(boundary_segment.get("id", "unknown"))
            segment_words = _segment_words(boundary_segment, words_by_id)
            if segment_words is None:
                unsafe_boundary_ids.append(segment_id)
                continue
            _residuals, removed_word_ids = _residual_primary_segments(
                boundary_segment,
                segment_words,
                candidate_intervals.get(
                    str(boundary_segment.get("speaker", "UNKNOWN")), []
                ),
            )
            boundary_word_ids.extend(removed_word_ids)
        aligned_window["boundary_primary_ids"] = _record_ids(
            boundary_segments, "id"
        )
        aligned_window["boundary_word_ids"] = list(
            dict.fromkeys(boundary_word_ids)
        )
        fatal_fallback = [
            issue
            for issue in fallback_issues
            if _intersects(issue, applied_window)
            and any(
                code in FATAL_ISSUE_CODES
                for code in issue.get("reason_codes", [])
            )
        ]
        if fallback_out_of_bounds:
            outcome = "needs_review_fallback_out_of_audio_bounds"
        elif fatal_fallback:
            outcome = "needs_review_fallback_structural_failure"
        elif not candidates:
            outcome = "needs_review_no_fallback_segments"
        elif unsafe_boundary_ids:
            outcome = "needs_review_unsafe_same_speaker_boundary"
        else:
            outcome = "replaced_with_kotoba"
            active.append((aligned_window, candidates))
        decisions.append(
            {
                "window_id": window["window_id"],
                "context_start": window["start"],
                "context_end": window["end"],
                "start": round(float(applied_window["start"]), 3),
                "end": round(float(applied_window["end"]), 3),
                "outcome": outcome,
                "reason_codes": list(window.get("reason_codes", [])),
                "fallback_segment_count": len(candidates),
                "fallback_fatal_issue_ids": _record_ids(
                    fatal_fallback, "issue_id"
                ),
                "superseded_segment_ids": list(
                    dict.fromkeys(
                        [
                            *window.get("primary_segment_ids", []),
                            *aligned_window.get(
                                "boundary_primary_ids", []
                            ),
                        ]
                    )
                ),
                "superseded_word_ids": list(
                    dict.fromkeys(
                        [
                            *window.get("primary_word_ids", []),
                            *aligned_window.get("boundary_word_ids", []),
                        ]
                    )
                ),
                "unsafe_boundary_segment_ids": unsafe_boundary_ids,
            }
        )

    target_segment_ids = {
        str(segment_id)
        for window, _candidates in active
        for segment_id in window.get("primary_segment_ids", [])
    }
    boundary_segment_ids = {
        str(segment_id)
        for window, _candidates in active
        for segment_id in window.get("boundary_primary_ids", [])
    }
    active_intervals_by_speaker: dict[str, list[dict[str, float]]] = (
        defaultdict(list)
    )
    for _window, candidates in active:
        for speaker, intervals in _fallback_intervals_by_speaker(
            candidates, speaker_mapping
        ).items():
            active_intervals_by_speaker[speaker].extend(intervals)
    output: list[dict[str, Any]] = []
    for primary in primary_segments:
        primary_id = str(primary.get("id", ""))
        if primary_id in target_segment_ids:
            continue
        if primary_id in boundary_segment_ids:
            segment_words = _segment_words(primary, words_by_id)
            assert segment_words is not None
            residuals, _removed_word_ids = _residual_primary_segments(
                primary,
                segment_words,
                active_intervals_by_speaker.get(
                    str(primary.get("speaker", "UNKNOWN")), []
                ),
            )
            output.extend(residuals)
            continue
        output.append(dict(primary))

    emitted_fallback_ids: set[str] = set()
    for window, candidates in active:
        for fallback in candidates:
            span = _span(fallback)
            if span is None:
                continue
            source_id = str(
                fallback.get("id", fallback.get("span_id", "unknown"))
            )
            if source_id in emitted_fallback_ids:
                continue
            emitted_fallback_ids.add(source_id)
            reason_codes = list(window.get("reason_codes", []))
            if "HYBRID_KOTOBA_RESCUE" not in reason_codes:
                reason_codes.append("HYBRID_KOTOBA_RESCUE")
            output.append(
                {
                    **dict(fallback),
                    "start": round(span[0], 3),
                    "end": round(span[1], 3),
                    "speaker": speaker_mapping.get(
                        str(fallback.get("speaker", "UNKNOWN")),
                        str(fallback.get("speaker", "UNKNOWN")),
                    ),
                    "provider": HYBRID_POLICY_VERSION,
                    "decision": "replace",
                    "reason_codes": reason_codes,
                    "parent_span_ids": list(
                        dict.fromkeys(
                            [
                                *fallback.get("parent_span_ids", []),
                                source_id,
                                *window.get("primary_segment_ids", []),
                                *window.get("boundary_primary_ids", []),
                            ]
                        )
                    ),
                    "source_segment_id": source_id,
                    "rescue_window_id": window["window_id"],
                }
            )
    output.sort(
        key=lambda segment: (
            float(segment.get("start", 0.0)),
            float(segment.get("end", 0.0)),
            str(segment.get("speaker", "UNKNOWN")),
            str(segment.get("text", "")),
        )
    )
    diagnostics = {
        "policy_version": HYBRID_POLICY_VERSION,
        "windows": snapped,
        "decisions": decisions,
        "speaker_mapping": speaker_mapping,
        "speaker_mapping_diagnostics": speaker_mapping_diagnostics,
        "primary_segment_count": len(primary_segments),
        "fallback_segment_count": len(fallback_segments),
        "output_segment_count": len(output),
        "replaced_window_count": len(active),
        "needs_review": any(
            decision["outcome"].startswith("needs_review")
            for decision in decisions
        ),
    }
    return output, diagnostics


def build_recall_union_segments(
    primary_segments: Sequence[Mapping[str, Any]],
    fused_segments: Sequence[Mapping[str, Any]],
    diagnostics: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Keep every WhisperX segment and add every accepted Kotoba rescue.

    This policy intentionally optimizes recall instead of precision.  The
    normal hybrid fusion is still used to validate Kotoba candidates and to
    reject structurally unsafe rescue windows, but no accepted rescue is
    allowed to supersede primary transcript content.
    """
    primary = [dict(segment) for segment in primary_segments]
    primary_ids = {
        str(segment.get("id", ""))
        for segment in primary
        if segment.get("id")
    }
    emitted_source_ids: set[str] = set()
    additions: list[dict[str, Any]] = []
    for segment in fused_segments:
        if (
            segment.get("provider") != HYBRID_POLICY_VERSION
            or segment.get("decision") != "replace"
        ):
            continue
        source_id = str(
            segment.get(
                "source_segment_id",
                segment.get("id", segment.get("span_id", "unknown")),
            )
        )
        if source_id in primary_ids or source_id in emitted_source_ids:
            continue
        emitted_source_ids.add(source_id)
        reason_codes = list(segment.get("reason_codes", []))
        if "HYBRID_KOTOBA_RECALL_UNION" not in reason_codes:
            reason_codes.append("HYBRID_KOTOBA_RECALL_UNION")
        additions.append(
            {
                **dict(segment),
                "decision": "augment",
                "reason_codes": reason_codes,
                "fusion_policy": RECALL_UNION_POLICY_VERSION,
            }
        )

    output = [*primary, *additions]
    output.sort(
        key=lambda segment: (
            float(segment.get("start", 0.0)),
            float(segment.get("end", 0.0)),
            str(segment.get("speaker", "UNKNOWN")),
            str(segment.get("text", "")),
        )
    )

    decisions: list[dict[str, Any]] = []
    augmented_window_count = 0
    for source in diagnostics.get("decisions", []):
        decision = dict(source)
        if decision.get("outcome") == "replaced_with_kotoba":
            decision["outcome"] = "augmented_with_kotoba"
            decision["would_supersede_segment_ids"] = list(
                decision.get("superseded_segment_ids", [])
            )
            decision["would_supersede_word_ids"] = list(
                decision.get("superseded_word_ids", [])
            )
            decision["superseded_segment_ids"] = []
            decision["superseded_word_ids"] = []
            augmented_window_count += 1
        decisions.append(decision)

    recall_diagnostics = {
        **dict(diagnostics),
        "policy_version": RECALL_UNION_POLICY_VERSION,
        "merge_policy": "recall_union",
        "decisions": decisions,
        "output_segment_count": len(output),
        "replaced_window_count": 0,
        "augmented_window_count": augmented_window_count,
        "preserved_primary_segment_count": len(primary),
        "added_rescue_segment_count": len(additions),
    }
    return output, recall_diagnostics
