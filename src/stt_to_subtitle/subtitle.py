"""Speaker-aware Korean subtitle timeline and file rendering."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
import os
from pathlib import Path
from typing import Any

from .contracts import validate_translation_items

SPEAKER_COLORS = (
    "#67E8F9",
    "#FDE047",
    "#F9A8D4",
    "#86EFAC",
    "#C4B5FD",
    "#FDBA74",
    "#93C5FD",
    "#FCA5A5",
)
ASS_FONT_NAME = "Noto Sans CJK KR"
MINIMUM_CUE_SECONDS = 0.1


@dataclass(frozen=True)
class SubtitleLine:
    speaker: str
    label: str
    css_class: str
    color: str
    text: str


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    lines: tuple[SubtitleLine, ...]


@dataclass(frozen=True)
class SubtitleTimeline:
    cues: tuple[SubtitleCue, ...]
    repaired_segment_ids: tuple[str, ...]


def format_srt_timestamp(seconds: float) -> str:
    total_milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{milliseconds:03d}"


def format_webvtt_timestamp(seconds: float) -> str:
    return format_srt_timestamp(seconds).replace(",", ".")


def format_ass_timestamp(seconds: float) -> str:
    total_centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(total_centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    secs, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def _maximum_legacy_duration(text: str) -> float:
    visible_characters = len("".join(text.split()))
    return max(15.0, min(30.0, 2.0 + visible_characters / 5.0))


def _speaker_presentations(
    segments: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, str, str]]:
    speakers = list(
        dict.fromkeys(
            str(segment.get("speaker", "UNKNOWN"))
            for segment in segments
        )
    )
    return {
        speaker: (
            f"화자 {index + 1}",
            f"speaker-{index + 1}",
            SPEAKER_COLORS[index % len(SPEAKER_COLORS)],
        )
        for index, speaker in enumerate(speakers)
    }


def _speaker_order(
    presentations: Mapping[str, tuple[str, str, str]],
) -> dict[str, int]:
    return {
        speaker: index
        for index, speaker in enumerate(presentations)
    }


def build_subtitle_timeline(
    transcript_segments: Sequence[Mapping[str, Any]],
    translation_items: Sequence[Mapping[str, Any]],
) -> SubtitleTimeline:
    """Create non-overlapping cues while preserving real speaker overlap."""
    expected_ids = [str(segment["id"]) for segment in transcript_segments]
    translations = validate_translation_items(translation_items, expected_ids)
    text_by_id = {item["id"]: item["text"] for item in translations}
    presentations = _speaker_presentations(transcript_segments)
    speaker_order = _speaker_order(presentations)

    raw_segments: list[dict[str, Any]] = []
    for index, segment in enumerate(transcript_segments):
        segment_id = str(segment["id"])
        start = max(0.0, float(segment["start"]))
        end = max(start, float(segment["end"]))
        speaker = str(segment.get("speaker", "UNKNOWN"))
        raw_segments.append(
            {
                "id": segment_id,
                "index": index,
                "start": start,
                "end": end,
                "speaker": speaker,
                "text": text_by_id[segment_id],
            }
        )
    raw_segments.sort(
        key=lambda item: (
            item["start"],
            item["end"],
            item["speaker"],
            item["index"],
        )
    )

    repaired_ids: list[str] = []
    for index, segment in enumerate(raw_segments):
        maximum_duration = _maximum_legacy_duration(str(segment["text"]))
        if segment["end"] - segment["start"] <= maximum_duration:
            continue
        same_speaker_starts = [
            float(candidate["start"])
            for candidate in raw_segments[index + 1 :]
            if (
                candidate["speaker"] == segment["speaker"]
                and float(candidate["start"])
                > float(segment["start"]) + MINIMUM_CUE_SECONDS
            )
        ]
        repaired_end = min(
            float(segment["end"]),
            float(segment["start"]) + maximum_duration,
            (
                min(same_speaker_starts)
                if same_speaker_starts
                else float("inf")
            ),
        )
        segment["end"] = max(
            float(segment["start"]) + MINIMUM_CUE_SECONDS,
            repaired_end,
        )
        repaired_ids.append(str(segment["id"]))

    boundaries = sorted(
        {
            float(value)
            for segment in raw_segments
            for value in (segment["start"], segment["end"])
        }
    )
    cues: list[SubtitleCue] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end - start < MINIMUM_CUE_SECONDS:
            continue
        active_by_speaker: dict[str, dict[str, Any]] = {}
        for segment in raw_segments:
            if float(segment["start"]) >= end or float(segment["end"]) <= start:
                continue
            speaker = str(segment["speaker"])
            previous = active_by_speaker.get(speaker)
            if previous is None or (
                float(segment["start"]),
                int(segment["index"]),
            ) > (
                float(previous["start"]),
                int(previous["index"]),
            ):
                active_by_speaker[speaker] = segment
        if not active_by_speaker:
            continue

        active = sorted(
            active_by_speaker.values(),
            key=lambda segment: (
                speaker_order[str(segment["speaker"])],
                int(segment["index"]),
            ),
        )
        lines = tuple(
            SubtitleLine(
                speaker=str(segment["speaker"]),
                label=presentations[str(segment["speaker"])][0],
                css_class=presentations[str(segment["speaker"])][1],
                color=presentations[str(segment["speaker"])][2],
                text=str(segment["text"]),
            )
            for segment in active
        )
        if cues and cues[-1].lines == lines and abs(cues[-1].end - start) < 0.001:
            previous = cues[-1]
            cues[-1] = SubtitleCue(previous.start, end, previous.lines)
        else:
            cues.append(SubtitleCue(start, end, lines))

    return SubtitleTimeline(tuple(cues), tuple(repaired_ids))


def _render_srt_timeline(timeline: SubtitleTimeline) -> str:
    blocks: list[str] = []
    for index, cue in enumerate(timeline.cues, start=1):
        lines = []
        for line in cue.lines:
            text = escape(line.text, quote=False).replace("\r", "")
            lines.append(
                f'<font color="{line.color}"><b>{line.label}:</b> '
                f"{text}</font>"
            )
        blocks.append(
            f"{index}\n"
            f"{format_srt_timestamp(cue.start)} --> "
            f"{format_srt_timestamp(cue.end)}\n"
            + "\n".join(lines)
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_srt(
    transcript_segments: Sequence[Mapping[str, Any]],
    translation_items: Sequence[Mapping[str, Any]],
) -> str:
    return _render_srt_timeline(
        build_subtitle_timeline(transcript_segments, translation_items)
    )


def _ass_color(rgb: str) -> str:
    red, green, blue = rgb[1:3], rgb[3:5], rgb[5:7]
    return f"&H00{blue}{green}{red}&"


def _escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\r\n", r"\N")
        .replace("\r", r"\N")
        .replace("\n", r"\N")
    )


def _render_ass_timeline(timeline: SubtitleTimeline) -> str:
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{ASS_FONT_NAME},52,&H00FFFFFF,&H000000FF,"
        "&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2.4,0.8,2,70,70,48,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    events: list[str] = []
    for cue in timeline.cues:
        lines = []
        for line in cue.lines:
            lines.append(
                rf"{{\c{_ass_color(line.color)}\b1}}"
                f"{_escape_ass_text(line.label)}:"
                rf"{{\b0}} {_escape_ass_text(line.text)}"
            )
        events.append(
            "Dialogue: 0,"
            f"{format_ass_timestamp(cue.start)},"
            f"{format_ass_timestamp(cue.end)},"
            "Default,,0,0,0,,"
            + r"\N".join(lines)
        )
    return header + "\n".join(events) + ("\n" if events else "")


def render_ass(
    transcript_segments: Sequence[Mapping[str, Any]],
    translation_items: Sequence[Mapping[str, Any]],
) -> str:
    return _render_ass_timeline(
        build_subtitle_timeline(transcript_segments, translation_items)
    )


def _render_webvtt_timeline(timeline: SubtitleTimeline) -> str:
    cues: list[str] = []
    for cue in timeline.cues:
        lines = []
        for line in cue.lines:
            text = escape(line.text, quote=False).replace("\r", "")
            lines.append(
                f"<c.{line.css_class}><b>{line.label}:</b> {text}</c>"
            )
        cues.append(
            f"{format_webvtt_timestamp(cue.start)} --> "
            f"{format_webvtt_timestamp(cue.end)}\n"
            + "\n".join(lines)
        )
    return "WEBVTT\n\n" + "\n\n".join(cues) + ("\n" if cues else "")


def render_webvtt(
    transcript_segments: Sequence[Mapping[str, Any]],
    translation_items: Sequence[Mapping[str, Any]],
) -> str:
    return _render_webvtt_timeline(
        build_subtitle_timeline(transcript_segments, translation_items)
    )


def _stage_text(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return temporary


def _write_text_atomic(path: Path, content: str, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"subtitle already exists: {path}")
    temporary = _stage_text(path, content)
    try:
        if path.exists() and not overwrite:
            raise FileExistsError(f"subtitle already exists: {path}")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_srt_atomic(
    path: Path,
    transcript_segments: Sequence[Mapping[str, Any]],
    translation_items: Sequence[Mapping[str, Any]],
    *,
    overwrite: bool,
) -> None:
    _write_text_atomic(
        path,
        render_srt(transcript_segments, translation_items),
        overwrite=overwrite,
    )


def write_styled_subtitles_atomic(
    srt_path: Path,
    ass_path: Path,
    transcript_segments: Sequence[Mapping[str, Any]],
    translation_items: Sequence[Mapping[str, Any]],
    *,
    overwrite: bool,
) -> SubtitleTimeline:
    if not overwrite:
        for path in (srt_path, ass_path):
            if path.exists():
                raise FileExistsError(f"subtitle already exists: {path}")
    timeline = build_subtitle_timeline(
        transcript_segments,
        translation_items,
    )
    staged: list[tuple[Path, Path]] = []
    try:
        staged.append(
            (
                srt_path,
                _stage_text(srt_path, _render_srt_timeline(timeline)),
            )
        )
        staged.append(
            (
                ass_path,
                _stage_text(ass_path, _render_ass_timeline(timeline)),
            )
        )
        if not overwrite:
            for path, _temporary in staged:
                if path.exists():
                    raise FileExistsError(f"subtitle already exists: {path}")
        for path, temporary in staged:
            temporary.replace(path)
    finally:
        for _path, temporary in staged:
            temporary.unlink(missing_ok=True)
    return timeline
