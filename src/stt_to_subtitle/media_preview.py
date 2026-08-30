"""Helpers for authenticated browser playback of web media and subtitles."""

from __future__ import annotations

from collections.abc import Iterator
from html import escape, unescape
import mimetypes
from pathlib import Path
import re

from .subtitle import SPEAKER_COLORS


_SRT_TIMING = re.compile(
    r"^(?P<start>\d+:\d{2}:\d{2})[,.](?P<start_ms>\d{3})"
    r"\s*-->\s*"
    r"(?P<end>\d+:\d{2}:\d{2})[,.](?P<end_ms>\d{3})"
)
_SRT_FONT_LINE = re.compile(
    r'^\s*<font\s+color=["\'](?P<color>#[0-9a-fA-F]{6})["\']>'
    r"(?P<text>.*)</font>\s*$",
    re.IGNORECASE,
)
_SPEAKER_CLASS_BY_COLOR = {
    color.casefold(): f"speaker-{index + 1}"
    for index, color in enumerate(SPEAKER_COLORS)
}
MEDIA_TYPE_OVERRIDES = {
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
}


def guess_media_type(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    return (
        MEDIA_TYPE_OVERRIDES.get(suffix)
        or mimetypes.guess_type(file_name)[0]
        or "application/octet-stream"
    )


def parse_byte_range(
    range_header: str | None,
    file_size: int,
) -> tuple[int, int] | None:
    """Parse one RFC 7233 byte range and clamp its end to the file."""
    if range_header is None:
        return None
    if file_size < 0:
        raise ValueError("file size cannot be negative")
    unit, separator, value = range_header.partition("=")
    if separator != "=" or unit.strip().lower() != "bytes" or "," in value:
        raise ValueError("unsupported byte range")
    start_text, dash, end_text = value.strip().partition("-")
    if dash != "-" or (not start_text and not end_text) or file_size == 0:
        raise ValueError("invalid byte range")

    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("invalid suffix byte range")
        start = max(0, file_size - suffix_length)
        return start, file_size - 1

    start = int(start_text)
    if start < 0 or start >= file_size:
        raise ValueError("byte range starts beyond the file")
    if not end_text:
        return start, file_size - 1

    end = min(int(end_text), file_size - 1)
    if end < start:
        raise ValueError("byte range ends before it starts")
    return start, end


def iter_file_range(
    path: Path,
    start: int,
    end: int,
    *,
    chunk_size: int = 1024 * 1024,
) -> Iterator[bytes]:
    """Yield only the inclusive byte range needed by an HTML5 player."""
    remaining = max(0, end - start + 1)
    with path.open("rb") as stream:
        stream.seek(start)
        while remaining:
            chunk = stream.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def read_subtitle_text(path: Path) -> str:
    """Read a browser subtitle using the encodings accepted by the library."""

    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("자막 파일 인코딩은 UTF-8 또는 CP949여야 합니다.")


def srt_to_webvtt(srt_text: str) -> str:
    """Convert generated SRT cues to safe, speaker-styled WebVTT."""
    normalized = (
        srt_text.removeprefix("\ufeff")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )
    if not normalized:
        return "WEBVTT\n"

    cues: list[str] = []
    for block in re.split(r"\n{2,}", normalized):
        lines = block.splitlines()
        timing_index = next(
            (
                index
                for index, line in enumerate(lines[:2])
                if _SRT_TIMING.match(line.strip())
            ),
            None,
        )
        if timing_index is None:
            continue
        match = _SRT_TIMING.match(lines[timing_index].strip())
        if match is None:
            continue
        timing = (
            f"{match.group('start')}.{match.group('start_ms')} --> "
            f"{match.group('end')}.{match.group('end_ms')}"
        )
        cue_lines = []
        for line in lines[timing_index + 1 :]:
            font_line = _SRT_FONT_LINE.match(line)
            if font_line is None:
                cue_lines.append(escape(line, quote=False))
                continue
            text = escape(unescape(font_line.group("text")), quote=False)
            speaker_class = _SPEAKER_CLASS_BY_COLOR.get(
                font_line.group("color").casefold()
            )
            cue_lines.append(
                f"<c.{speaker_class}>{text}</c>" if speaker_class else text
            )
        cue_text = "\n".join(cue_lines)
        cues.append(f"{timing}\n{cue_text}")
    return "WEBVTT\n\n" + "\n\n".join(cues) + ("\n" if cues else "")
