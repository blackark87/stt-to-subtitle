"""Audio extraction with FFmpeg."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudioExtraction:
    audio_stream: int = 0
    start_seconds: float = 0.0
    duration_seconds: float | None = None

    def validate(self) -> None:
        if self.audio_stream < 0:
            raise ValueError("audio_stream must be zero or greater")
        if self.start_seconds < 0:
            raise ValueError("start_seconds must be zero or greater")
        if self.duration_seconds is not None and self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be greater than zero")


def build_ffmpeg_command(
    source: Path,
    destination: Path,
    options: AudioExtraction,
) -> list[str]:
    """Build a deterministic 16 kHz mono PCM extraction command."""
    options.validate()
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    if options.start_seconds:
        command.extend(["-ss", str(options.start_seconds)])
    command.extend(["-i", str(source)])
    if options.duration_seconds is not None:
        command.extend(["-t", str(options.duration_seconds)])
    command.extend(
        [
            "-map",
            f"0:a:{options.audio_stream}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
    )
    return command


def extract_audio(
    source: Path,
    destination: Path,
    options: AudioExtraction,
) -> None:
    """Extract audio and fail with FFmpeg's diagnostic if conversion fails."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = build_ffmpeg_command(source, destination, options)
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError("ffmpeg executable was not found") from error

    if completed.returncode != 0:
        detail = completed.stderr.strip() or "unknown FFmpeg error"
        raise RuntimeError(f"audio extraction failed: {detail}")
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("audio extraction produced an empty file")
