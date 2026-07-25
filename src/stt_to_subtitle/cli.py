"""Command-line entry point for local Docker transcription tests."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

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

    print(f"[1/3] Extracting audio: {audio_path}", flush=True)
    extract_audio(source, audio_path, extraction)

    print("[2/3] Loading models and transcribing on CPU", flush=True)
    started = time.monotonic()
    raw_result = transcribe(audio_path, token, transcription)
    elapsed = time.monotonic() - started
    segments = normalize_segments(
        raw_result,
        offset_seconds=extraction.start_seconds,
    )

    print("[3/3] Writing transcript outputs", flush=True)
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
