"""Run OWSM CTC over overlapping windows for omission auditing."""

from __future__ import annotations

import argparse
import json
import math
import os
import time
import wave
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Iterator


MODEL_ID = "espnet/owsm_ctc_v4_1B"
MODEL_REVISION = "6db41715b887d65d2d6936a290e7844cb98f9d29"


@contextmanager
def _working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def window_ranges(
    duration_seconds: float,
    *,
    window_seconds: float,
    overlap_seconds: float,
) -> list[tuple[float, float]]:
    if duration_seconds <= 0:
        raise ValueError("audio duration must be positive")
    if window_seconds <= 0:
        raise ValueError("window_seconds must be positive")
    if overlap_seconds < 0 or overlap_seconds >= window_seconds:
        raise ValueError(
            "overlap_seconds must be non-negative and smaller than the window"
        )
    step = window_seconds - overlap_seconds
    starts: list[float] = []
    start = 0.0
    while start < duration_seconds:
        starts.append(start)
        if start + window_seconds >= duration_seconds:
            break
        start += step
    return [
        (round(start, 6), round(min(start + window_seconds, duration_seconds), 6))
        for start in starts
    ]


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        frame_rate = audio.getframerate()
        if frame_rate <= 0:
            raise ValueError(f"invalid WAV frame rate: {path}")
        return audio.getnframes() / frame_rate


@contextmanager
def padded_wav_window(
    source: Path,
    *,
    start_seconds: float,
    end_seconds: float,
    padded_duration_seconds: float,
) -> Iterator[Path]:
    with wave.open(str(source), "rb") as input_audio:
        channels = input_audio.getnchannels()
        sample_width = input_audio.getsampwidth()
        frame_rate = input_audio.getframerate()
        if channels != 1 or input_audio.getcomptype() != "NONE":
            raise ValueError("OWSM audit input must be uncompressed mono WAV")
        start_frame = max(0, round(start_seconds * frame_rate))
        end_frame = min(input_audio.getnframes(), round(end_seconds * frame_rate))
        input_audio.setpos(start_frame)
        frames = input_audio.readframes(max(0, end_frame - start_frame))

    padded_frames = round(padded_duration_seconds * frame_rate)
    current_frames = len(frames) // sample_width
    if current_frames < padded_frames:
        frames += b"\x00" * ((padded_frames - current_frames) * sample_width)
    elif current_frames > padded_frames:
        frames = frames[: padded_frames * sample_width]

    with TemporaryDirectory(prefix="owsm-audit-window-") as directory:
        output = Path(directory) / "window.wav"
        with wave.open(str(output), "wb") as output_audio:
            output_audio.setnchannels(1)
            output_audio.setsampwidth(sample_width)
            output_audio.setframerate(frame_rate)
            output_audio.writeframes(frames)
        yield output


def _owsm_text(result: Any) -> str:
    if not isinstance(result, Sequence) or isinstance(result, (str, bytes)):
        raise RuntimeError("OWSM returned an invalid transcription")
    if not result:
        raise RuntimeError("OWSM returned no transcription")
    hypothesis = result[0]
    if not isinstance(hypothesis, Sequence) or isinstance(
        hypothesis, (str, bytes)
    ) or not hypothesis:
        raise RuntimeError("OWSM returned an invalid hypothesis")
    text = hypothesis[0]
    return "" if text is None else str(text).removeprefix("<jpn><asr>").lstrip()


def load_model(device: str):
    import soundfile
    import yaml
    from espnet2.bin.s2t_inference_ctc import Speech2TextGreedySearch
    from huggingface_hub import snapshot_download

    model_path = Path(
        snapshot_download(repo_id=MODEL_ID, revision=MODEL_REVISION)
    )
    metadata = yaml.safe_load((model_path / "meta.yaml").read_text("utf-8"))
    if not isinstance(metadata, Mapping):
        raise RuntimeError("OWSM metadata is invalid")
    files = metadata.get("files")
    yaml_files = metadata.get("yaml_files")
    if not isinstance(files, Mapping) or not isinstance(yaml_files, Mapping):
        raise RuntimeError("OWSM metadata has no model paths")
    with _working_directory(model_path):
        model = Speech2TextGreedySearch(
            s2t_train_config=str(model_path / str(yaml_files["s2t_train_config"])),
            s2t_model_file=str(model_path / str(files["s2t_model_file"])),
            device=device,
            dtype="float32",
            lang_sym="<jpn>",
            task_sym="<asr>",
            use_flash_attn=False,
        )

    def recognize(audio_path: Path) -> str:
        speech, sample_rate = soundfile.read(str(audio_path), dtype="float32")
        if sample_rate != 16000:
            raise ValueError(f"OWSM requires 16 kHz audio, got {sample_rate}")
        with _working_directory(model_path):
            return _owsm_text(model(speech))

    return recognize


def run(
    audio_path: Path,
    *,
    output: Path,
    device: str,
    window_seconds: float,
    overlap_seconds: float,
) -> None:
    duration = _wav_duration(audio_path)
    ranges = window_ranges(
        duration,
        window_seconds=window_seconds,
        overlap_seconds=overlap_seconds,
    )
    load_started = time.monotonic()
    recognize = load_model(device)
    model_load_seconds = time.monotonic() - load_started
    windows: list[dict[str, Any]] = []
    inference_started = time.monotonic()
    for index, (start, end) in enumerate(ranges):
        started = time.monotonic()
        with padded_wav_window(
            audio_path,
            start_seconds=start,
            end_seconds=end,
            padded_duration_seconds=window_seconds,
        ) as window_path:
            text = recognize(window_path).strip()
        windows.append(
            {
                "index": index,
                "start": start,
                "end": end,
                "text": text,
                "elapsed_seconds": round(time.monotonic() - started, 6),
            }
        )
        print(f"[owsm-audit] {index + 1}/{len(ranges)}", flush=True)
    payload = {
        "schema_version": 1,
        "model": {"id": MODEL_ID, "revision": MODEL_REVISION},
        "window_seconds": window_seconds,
        "overlap_seconds": overlap_seconds,
        "duration_seconds": round(duration, 6),
        "windows": windows,
        "runtime": {
            "device": device,
            "model_load_seconds": round(model_load_seconds, 6),
            "inference_seconds": round(
                time.monotonic() - inference_started,
                6,
            ),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--window-seconds", type=float, default=30.0)
    parser.add_argument("--overlap-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.window_seconds) or not math.isfinite(
        args.overlap_seconds
    ):
        parser.error("window and overlap must be finite")
    return args


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    run(
        args.audio,
        output=args.output,
        device=args.device,
        window_seconds=args.window_seconds,
        overlap_seconds=args.overlap_seconds,
    )


if __name__ == "__main__":
    main()
