"""External subtitle discovery, normalization, playback, and comparison."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
from pathlib import Path
import re
import unicodedata
from typing import Iterable, Sequence


EXTERNAL_SUBTITLE_EXTENSIONS = (".vtt", ".srt", ".ass")
SUBTITLE_COMPARISON_SCHEMA_VERSION = 1
SUBTITLE_VALIDATOR_PROMPT_VERSION = 1
SUBTITLE_VALIDATOR_MAX_ALIGNMENTS = 120
_CUE_TIMING = re.compile(
    r"^(?P<start>\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3})\s+-->\s+"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[,.]\d{3})(?:\s+.*)?$"
)
_ASS_TAG = re.compile(r"\{[^}]*\}")
_TEXT_TOKEN = re.compile(r"[^0-9a-zA-Z가-힣]+")
_ISSUE_LABELS = {
    "missing_candidate": "대응 자막 없음",
    "low_time_coverage": "시간 구간 부족",
    "text_mismatch": "문장 차이",
    "boundary_drift": "자막 경계 차이",
}


@dataclass(frozen=True)
class SubtitleCue:
    index: int
    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def public_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "start": self.start,
            "end": self.end,
            "text": self.text,
        }


def discover_external_subtitles(media_path: Path) -> tuple[Path, ...]:
    """Return plain sidecars for a media stem in playback priority order."""
    try:
        entries = {
            entry.name.casefold(): entry
            for entry in media_path.parent.iterdir()
            if entry.is_file() and not entry.is_symlink()
        }
    except OSError:
        return ()
    paths: list[Path] = []
    for extension in EXTERNAL_SUBTITLE_EXTENSIONS:
        candidate = entries.get(f"{media_path.stem}{extension}".casefold())
        if candidate is not None:
            paths.append(candidate)
    return tuple(paths)


def subtitle_asset_hash(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.suffix.lower().encode("ascii"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def parse_subtitle(path: Path) -> list[SubtitleCue]:
    text = _read_subtitle_text(path)
    suffix = path.suffix.lower()
    if suffix == ".srt":
        cues = _parse_timed_blocks(text, webvtt=False)
    elif suffix == ".vtt":
        cues = _parse_timed_blocks(text, webvtt=True)
    elif suffix == ".ass":
        cues = _parse_ass(text)
    else:
        raise ValueError(f"지원하지 않는 자막 형식입니다: {suffix}")
    if not cues:
        raise ValueError("외부 자막에서 유효한 cue를 찾을 수 없습니다.")
    return cues


def render_webvtt(cues: Sequence[SubtitleCue]) -> str:
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines.extend(
            [
                str(cue.index),
                f"{_format_webvtt_time(cue.start)} --> "
                f"{_format_webvtt_time(cue.end)}",
                cue.text,
                "",
            ]
        )
    return "\n".join(lines)


def compare_subtitles(
    reference: Sequence[SubtitleCue],
    candidate: Sequence[SubtitleCue],
) -> dict[str, object]:
    """Align Korean subtitle cues by time and return deterministic metrics."""
    if not reference:
        raise ValueError("외부 자막 cue가 없습니다.")
    if not candidate:
        raise ValueError("비교할 생성 자막 cue가 없습니다.")

    matched_candidates: set[int] = set()
    alignments: list[dict[str, object]] = []
    issues: list[dict[str, object]] = []
    covered_seconds = 0.0
    total_reference_seconds = sum(cue.duration for cue in reference)
    similarities: list[float] = []
    start_deltas: list[float] = []
    end_deltas: list[float] = []

    for ref in reference:
        overlapping = [
            (index, cue)
            for index, cue in enumerate(candidate)
            if _overlap_seconds(ref, cue) > 0.0
        ]
        candidate_indices = [index for index, _cue in overlapping]
        matched_candidates.update(candidate_indices)
        combined_text = " ".join(cue.text for _index, cue in overlapping).strip()
        coverage_seconds = _covered_seconds(ref, (cue for _index, cue in overlapping))
        coverage = (
            min(1.0, coverage_seconds / ref.duration)
            if ref.duration > 0.0
            else 0.0
        )
        covered_seconds += coverage_seconds
        similarity = _text_similarity(ref.text, combined_text)
        if combined_text:
            similarities.append(similarity)
            start_delta = abs(overlapping[0][1].start - ref.start)
            end_delta = abs(overlapping[-1][1].end - ref.end)
            start_deltas.append(start_delta)
            end_deltas.append(end_delta)
        else:
            start_delta = None
            end_delta = None

        alignment = {
            "reference": ref.public_dict(),
            "candidate_indices": candidate_indices,
            "candidate_text": combined_text,
            "coverage": round(coverage, 4),
            "text_similarity": round(similarity, 4),
            "start_delta_seconds": (
                round(start_delta, 3) if start_delta is not None else None
            ),
            "end_delta_seconds": (
                round(end_delta, 3) if end_delta is not None else None
            ),
        }
        alignments.append(alignment)
        if not overlapping:
            issues.append(
                _issue("missing_candidate", ref, "대응하는 생성 자막 cue가 없습니다.")
            )
        elif coverage < 0.5:
            issues.append(
                _issue("low_time_coverage", ref, "시간 구간 coverage가 50% 미만입니다.")
            )
        if combined_text and similarity < 0.45:
            issues.append(
                _issue("text_mismatch", ref, "한국어 문장 유사도가 낮습니다.")
            )
        if (
            start_delta is not None
            and end_delta is not None
            and max(start_delta, end_delta) > 1.5
        ):
            issues.append(
                _issue("boundary_drift", ref, "cue 경계 차이가 1.5초를 초과합니다.")
            )

    unmatched_candidates = [
        cue.public_dict()
        for index, cue in enumerate(candidate)
        if index not in matched_candidates
    ]
    coverage_ratio = (
        min(1.0, covered_seconds / total_reference_seconds)
        if total_reference_seconds > 0.0
        else 0.0
    )
    summary = {
        "reference_cues": len(reference),
        "candidate_cues": len(candidate),
        "matched_reference_cues": sum(
            bool(item["candidate_indices"]) for item in alignments
        ),
        "unmatched_reference_cues": sum(
            not item["candidate_indices"] for item in alignments
        ),
        "unmatched_candidate_cues": len(unmatched_candidates),
        "time_coverage": round(coverage_ratio, 4),
        "average_text_similarity": round(_average(similarities), 4),
        "average_start_delta_seconds": round(_average(start_deltas), 3),
        "average_end_delta_seconds": round(_average(end_deltas), 3),
        "issue_count": len(issues),
    }
    return {
        "schema_version": SUBTITLE_COMPARISON_SCHEMA_VERSION,
        "summary": summary,
        "issues": issues,
        "alignments": alignments,
        "unmatched_candidates": unmatched_candidates,
    }


def build_subtitle_validator_payload(
    metrics: dict[str, object],
) -> dict[str, object]:
    """Build a bounded, deterministic payload for an explicit model review."""
    raw_alignments = metrics.get("alignments", [])
    raw_issues = metrics.get("issues", [])
    if not isinstance(raw_alignments, list) or not isinstance(raw_issues, list):
        raise ValueError("자막 비교 결과 형식이 올바르지 않습니다.")
    issue_indices = {
        int(issue["reference_index"])
        for issue in raw_issues
        if isinstance(issue, dict)
        and isinstance(issue.get("reference_index"), int)
    }
    ranked = sorted(
        (item for item in raw_alignments if isinstance(item, dict)),
        key=lambda item: (
            0
            if isinstance(item.get("reference"), dict)
            and item["reference"].get("index") in issue_indices
            else 1,
            float(item.get("text_similarity", 0.0)),
            int(
                item["reference"].get("index", 0)
                if isinstance(item.get("reference"), dict)
                else 0
            ),
        ),
    )[:SUBTITLE_VALIDATOR_MAX_ALIGNMENTS]
    segments: list[dict[str, object]] = []
    for item in sorted(
        ranked,
        key=lambda value: int(
            value["reference"].get("index", 0)
            if isinstance(value.get("reference"), dict)
            else 0
        ),
    ):
        reference = item.get("reference")
        if not isinstance(reference, dict):
            continue
        segments.append(
            {
                "reference_index": int(reference.get("index", 0)),
                "start": reference.get("start"),
                "end": reference.get("end"),
                "external_text": str(reference.get("text", ""))[:600],
                "generated_text": str(item.get("candidate_text", ""))[:600],
                "time_coverage": item.get("coverage"),
                "text_similarity": item.get("text_similarity"),
            }
        )
    return {
        "prompt_version": SUBTITLE_VALIDATOR_PROMPT_VERSION,
        "summary": metrics.get("summary", {}),
        "segments": segments,
        "selection": {
            "total_alignments": len(raw_alignments),
            "included_alignments": len(segments),
            "issue_alignments_first": True,
        },
    }


def _read_subtitle_text(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("자막 파일 인코딩은 UTF-8 또는 CP949여야 합니다.")


def _parse_timed_blocks(text: str, *, webvtt: bool) -> list[SubtitleCue]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n{2,}", normalized)
    cues: list[SubtitleCue] = []
    for block in blocks:
        lines = [line.strip("\ufeff") for line in block.split("\n")]
        lines = [line for line in lines if line.strip()]
        if not lines:
            continue
        if webvtt and lines[0].strip().upper().startswith(("WEBVTT", "NOTE", "STYLE")):
            continue
        timing_index = next(
            (index for index, line in enumerate(lines) if _CUE_TIMING.match(line.strip())),
            None,
        )
        if timing_index is None:
            continue
        match = _CUE_TIMING.match(lines[timing_index].strip())
        if match is None:
            continue
        start = _parse_clock(match.group("start"))
        end = _parse_clock(match.group("end"))
        cue_text = "\n".join(line.strip() for line in lines[timing_index + 1 :]).strip()
        if end <= start or not cue_text:
            continue
        cues.append(SubtitleCue(len(cues) + 1, start, end, cue_text))
    return cues


def _parse_ass(text: str) -> list[SubtitleCue]:
    fields = [
        "layer",
        "start",
        "end",
        "style",
        "name",
        "marginl",
        "marginr",
        "marginv",
        "effect",
        "text",
    ]
    in_events = False
    cues: list[SubtitleCue] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_events = line.casefold() == "[events]"
            continue
        if not in_events:
            continue
        if line.casefold().startswith("format:"):
            parsed = [item.strip().casefold() for item in line.split(":", 1)[1].split(",")]
            if "start" in parsed and "end" in parsed and "text" in parsed:
                fields = parsed
            continue
        if not line.casefold().startswith("dialogue:"):
            continue
        values = line.split(":", 1)[1].lstrip().split(",", len(fields) - 1)
        if len(values) != len(fields):
            continue
        item = dict(zip(fields, values, strict=True))
        try:
            start = _parse_ass_clock(item["start"])
            end = _parse_ass_clock(item["end"])
        except (KeyError, ValueError):
            continue
        cue_text = _ASS_TAG.sub("", item["text"])
        cue_text = cue_text.replace(r"\N", "\n").replace(r"\n", "\n")
        cue_text = cue_text.replace(r"\h", " ").strip()
        if end <= start or not cue_text:
            continue
        cues.append(SubtitleCue(len(cues) + 1, start, end, cue_text))
    return cues


def _parse_clock(value: str) -> float:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError("invalid subtitle timestamp")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _parse_ass_clock(value: str) -> float:
    parts = value.strip().split(":")
    if len(parts) != 3:
        raise ValueError("invalid ASS timestamp")
    hours, minutes, seconds = parts
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _format_webvtt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def _overlap_seconds(left: SubtitleCue, right: SubtitleCue) -> float:
    return max(0.0, min(left.end, right.end) - max(left.start, right.start))


def _covered_seconds(reference: SubtitleCue, candidates: Iterable[SubtitleCue]) -> float:
    intervals = sorted(
        (
            max(reference.start, cue.start),
            min(reference.end, cue.end),
        )
        for cue in candidates
        if _overlap_seconds(reference, cue) > 0.0
    )
    if not intervals:
        return 0.0
    covered = 0.0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        covered += current_end - current_start
        current_start, current_end = start, end
    return covered + current_end - current_start


def _text_similarity(left: str, right: str) -> float:
    normalized_left = _normalize_text(left)
    normalized_right = _normalize_text(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return _TEXT_TOKEN.sub("", normalized)


def _average(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _issue(code: str, cue: SubtitleCue, message: str) -> dict[str, object]:
    return {
        "code": code,
        "label": _ISSUE_LABELS[code],
        "reference_index": cue.index,
        "start": cue.start,
        "end": cue.end,
        "message": message,
    }
