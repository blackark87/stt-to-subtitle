"""Command-line entry point for local Docker transcription tests."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
import sys
import threading
import time
from pathlib import Path
from typing import Iterator

from .audio import AudioExtraction, extract_audio
from .kotoba import (
    MODEL_ID,
    MODEL_REVISION,
    TranscriptionOptions,
    normalize_segments,
    speaker_transcripts,
    transcribe,
)
from .output import write_outputs

PROGRESS_INTERVAL_SECONDS = 30.0


def _format_elapsed(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


def _report_progress(
    stop_event: threading.Event,
    label: str,
    started: float,
    interval_seconds: float,
) -> None:
    while not stop_event.wait(interval_seconds):
        elapsed = _format_elapsed(time.monotonic() - started)
        print(f"{label} still running (elapsed {elapsed})", flush=True)


@contextmanager
def _log_stage(
    label: str,
    *,
    detail: str | None = None,
    interval_seconds: float = PROGRESS_INTERVAL_SECONDS,
) -> Iterator[None]:
    if interval_seconds <= 0:
        raise ValueError("progress interval must be greater than zero")

    suffix = f": {detail}" if detail else ""
    print(f"{label} started{suffix}", flush=True)
    started = time.monotonic()
    stop_event = threading.Event()
    progress_thread = threading.Thread(
        target=_report_progress,
        args=(stop_event, label, started, interval_seconds),
        name="stt-progress",
        daemon=True,
    )
    progress_thread.start()

    try:
        yield
    except BaseException:
        stop_event.set()
        progress_thread.join()
        elapsed = _format_elapsed(time.monotonic() - started)
        print(f"{label} failed after {elapsed}", file=sys.stderr, flush=True)
        raise
    else:
        stop_event.set()
        progress_thread.join()
        elapsed = _format_elapsed(time.monotonic() - started)
        print(f"{label} completed in {elapsed}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract audio and transcribe it with Kotoba-Whisper v2.2.",
    )
    parser.add_argument("input", type=Path, help="input video or audio path")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/output"),
        help="directory for WAV, JSON, and TXT outputs (default: /output)",
    )
    parser.add_argument("--audio-stream", type=int, default=0)
    parser.add_argument("--start-seconds", type=float, default=0.0)
    parser.add_argument("--duration-seconds", type=float)
    parser.add_argument("--chunk-length-seconds", type=int, default=15)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--threads", type=int)
    parser.add_argument("--num-speakers", type=int)
    parser.add_argument("--min-speakers", type=int)
    parser.add_argument("--max-speakers", type=int)
    parser.add_argument(
        "--add-punctuation",
        action="store_true",
        help="run the optional punctuation model for per-speaker text",
    )
    return parser


def run(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    source = args.input.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"input file does not exist: {source}")

    token = os.environ.get("HF_TOKEN", "")
    if not token.strip():
        raise ValueError(
            "HF_TOKEN is required; accept both Pyannote model terms first"
        )

    extraction = AudioExtraction(
        audio_stream=args.audio_stream,
        start_seconds=args.start_seconds,
        duration_seconds=args.duration_seconds,
    )
    transcription = TranscriptionOptions(
        batch_size=args.batch_size,
        chunk_length_seconds=args.chunk_length_seconds,
        num_speakers=args.num_speakers,
        min_speakers=args.min_speakers,
        max_speakers=args.max_speakers,
        add_punctuation=args.add_punctuation,
        threads=args.threads,
    )
    extraction.validate()
    transcription.validate()

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_base = output_dir / f"{source.stem}.stt"
    audio_path = output_dir / f"{source.stem}.16k.wav"

    with _log_stage("[1/3] Audio extraction", detail=str(audio_path)):
        extract_audio(source, audio_path, extraction)

    started = time.monotonic()
    with _log_stage("[2/3] Model loading and CPU transcription"):
        raw_result = transcribe(audio_path, token, transcription)
    elapsed = time.monotonic() - started
    segments = normalize_segments(
        raw_result,
        offset_seconds=extraction.start_seconds,
    )

    metadata = {
        "source": str(source),
        "extracted_audio": str(audio_path),
        "audio_extraction": {
            "stream": extraction.audio_stream,
            "sample_rate": 16000,
            "channels": 1,
            "source_offset_seconds": extraction.start_seconds,
            "requested_duration_seconds": extraction.duration_seconds,
        },
        "model": {
            "id": MODEL_ID,
            "revision": MODEL_REVISION,
        },
        "transcription": {
            "chunk_length_seconds": transcription.chunk_length_seconds,
            "num_speakers": transcription.num_speakers,
            "min_speakers": transcription.min_speakers,
            "max_speakers": transcription.max_speakers,
            "add_punctuation": transcription.add_punctuation,
        },
        "runtime": {
            "device": "cpu",
            "batch_size": transcription.batch_size,
            "threads": transcription.threads,
            "elapsed_seconds": round(elapsed, 3),
        },
    }
    with _log_stage("[3/3] Transcript output"):
        json_path, text_path = write_outputs(
            output_base,
            metadata,
            segments,
            speaker_transcripts(raw_result),
        )
    return audio_path, json_path, text_path


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        audio_path, json_path, text_path = run(args)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(f"Audio: {audio_path}")
    print(f"JSON:  {json_path}")
    print(f"Text:  {text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
