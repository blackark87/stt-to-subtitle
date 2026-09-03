"""Isolated Kotoba worker used by the hybrid transcription runtime."""

from __future__ import annotations

import argparse
from dataclasses import fields
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence
import wave

from .files import write_json_atomic
from .kotoba import load_pipeline, normalize_segments, run_pipeline
from .stt_options import TranscriptionOptions
from .transcription_progress import write_stage_progress


def _transcription_options(options: Mapping[str, Any]) -> TranscriptionOptions:
    names = {field.name for field in fields(TranscriptionOptions)}
    values = {name: value for name, value in options.items() if name in names}
    parsed = TranscriptionOptions(**values)
    parsed.validate()
    return parsed


def _write_wav_slice(
    source: Path,
    destination: Path,
    start_seconds: float,
    end_seconds: float,
) -> float:
    with wave.open(str(source), "rb") as reader:
        frame_rate = reader.getframerate()
        total_frames = reader.getnframes()
        first = max(0, min(total_frames, int(start_seconds * frame_rate)))
        last = max(first, min(total_frames, int(end_seconds * frame_rate)))
        if last <= first:
            return 0.0
        reader.setpos(first)
        frames = reader.readframes(last - first)
        with wave.open(str(destination), "wb") as writer:
            writer.setnchannels(reader.getnchannels())
            writer.setsampwidth(reader.getsampwidth())
            writer.setframerate(frame_rate)
            writer.writeframes(frames)
    return (last - first) / float(frame_rate)


def _validated_windows(raw: object) -> list[dict[str, Any]] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("Kotoba windows must be a JSON list or null")
    windows: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise ValueError("each Kotoba window must be a JSON object")
        start = float(item.get("start", 0.0))
        end = float(item.get("end", 0.0))
        if start < 0 or end <= start:
            raise ValueError("Kotoba window end must be greater than start")
        windows.append(
            {
                **dict(item),
                "start": start,
                "end": end,
                "window_id": str(
                    item.get("window_id", f"rescue-window-{index:06d}")
                ),
            }
        )
    return windows


def run_kotoba(
    audio_path: Path,
    options: Mapping[str, Any],
    *,
    windows: Sequence[Mapping[str, Any]] | None = None,
    debug_artifact_dir: Path | None = None,
) -> dict[str, Any]:
    """Load Kotoba once and decode a full file or a set of rescue windows."""
    parsed = _transcription_options(options)
    pipeline = load_pipeline(
        os.environ.get("HF_TOKEN", ""),
        batch_size=parsed.batch_size,
        device=os.environ.get("STT_DEVICE", "cuda"),
        diarization_device=os.environ.get("STT_DIARIZATION_DEVICE", "cuda"),
        threads=parsed.threads,
    )
    if windows is None:
        result = run_pipeline(
            pipeline,
            audio_path,
            parsed,
            debug_artifact_dir=debug_artifact_dir,
        )
        return {
            "mode": "full",
            "result": dict(result),
            "segments": normalize_segments(result),
        }

    output_windows: list[dict[str, Any]] = []
    with TemporaryDirectory(prefix="stt-kotoba-rescue-") as directory:
        work_dir = Path(directory)
        for index, window in enumerate(windows):
            start = float(window["start"])
            end = float(window["end"])
            slice_path = work_dir / f"window-{index:03d}.wav"
            duration = _write_wav_slice(
                audio_path,
                slice_path,
                start,
                end,
            )
            if duration <= 0:
                continue
            result = run_pipeline(
                pipeline,
                slice_path,
                parsed,
                debug_artifact_dir=(
                    debug_artifact_dir / f"window-{index:03d}"
                    if debug_artifact_dir is not None
                    else None
                ),
            )
            chunks = result.get("chunks", [])
            output_windows.append(
                {
                    "window": dict(window),
                    "duration": round(duration, 3),
                    "segments": normalize_segments(
                        result,
                        offset_seconds=start,
                    ),
                    "chunk_count": len(chunks) if isinstance(chunks, list) else 0,
                    "noise_filter": result.get("noise_filter"),
                    "encoding_warning": result.get("encoding_warning"),
                    "timestamp_postprocessor": result.get(
                        "timestamp_postprocessor",
                        "model-default",
                    ),
                }
            )
    return {"mode": "windows", "windows": output_windows}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one isolated Kotoba job")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--options", required=True)
    parser.add_argument("--windows", default="null")
    parser.add_argument("--debug-dir", type=Path)
    parser.add_argument("--progress", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    options = json.loads(args.options)
    if not isinstance(options, Mapping):
        raise ValueError("Kotoba options must be a JSON object")
    windows = _validated_windows(json.loads(args.windows))
    write_stage_progress(
        args.progress,
        "rescue_transcription",
        5,
        7,
    )
    payload = run_kotoba(
        args.audio,
        options,
        windows=windows,
        debug_artifact_dir=args.debug_dir,
    )
    write_json_atomic(args.output, payload)


if __name__ == "__main__":
    main()
