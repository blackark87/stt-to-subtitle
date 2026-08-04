"""Isolated WhisperX worker used by the native transcription API."""

from __future__ import annotations

import argparse
import gc
from importlib.metadata import version
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .files import write_json_atomic

WHISPERX_PACKAGE_VERSION = "3.8.6"
DEFAULT_WHISPERX_MODEL = "large-v3"
DEFAULT_WHISPERX_LANGUAGE = "ja"
DEFAULT_WHISPERX_COMPUTE_TYPE = "float16"
WHISPERX_TIMESTAMP_POSTPROCESSOR = "whisperx-forced-alignment"


def normalize_whisperx_segments(
    raw_segments: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize aligned WhisperX segments for the shared transcript contract."""
    normalized: list[dict[str, Any]] = []
    for raw in raw_segments:
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        try:
            start = max(0.0, float(raw["start"]))
            end = max(start, float(raw["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        normalized.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "speaker": str(raw.get("speaker", "UNKNOWN")),
                "text": text,
            }
        )
    normalized.sort(key=lambda item: (item["start"], item["end"], item["text"]))
    return normalized


def _split_device(device: str) -> tuple[str, int]:
    if device == "cuda":
        return "cuda", 0
    if device.startswith("cuda:"):
        return "cuda", int(device.split(":", maxsplit=1)[1])
    if device == "cpu":
        return "cpu", 0
    raise ValueError("WhisperX supports only cpu, cuda, or cuda:<index>")


def _release_cuda() -> None:
    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        ipc_collect = getattr(torch.cuda, "ipc_collect", None)
        if callable(ipc_collect):
            ipc_collect()


def run_whisperx(
    audio_path: Path,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    """Run WhisperX ASR, Japanese alignment, and speaker diarization."""
    import whisperx
    from whisperx.diarize import DiarizationPipeline

    installed_version = version("whisperx")
    if installed_version != WHISPERX_PACKAGE_VERSION:
        raise RuntimeError(
            "WhisperX version mismatch: expected "
            f"{WHISPERX_PACKAGE_VERSION}, found {installed_version}"
        )

    hf_token = os.environ.get("HF_TOKEN", "").strip()
    if not hf_token:
        raise ValueError("HF_TOKEN is required for WhisperX diarization")

    device = os.environ.get("STT_DEVICE", "cuda").strip()
    diarization_device = os.environ.get(
        "STT_DIARIZATION_DEVICE",
        device,
    ).strip()
    device_kind, device_index = _split_device(device)
    _split_device(diarization_device)

    model_name = os.environ.get(
        "WHISPERX_MODEL",
        DEFAULT_WHISPERX_MODEL,
    ).strip()
    language = os.environ.get(
        "WHISPERX_LANGUAGE",
        DEFAULT_WHISPERX_LANGUAGE,
    ).strip()
    compute_type = os.environ.get(
        "WHISPERX_COMPUTE_TYPE",
        DEFAULT_WHISPERX_COMPUTE_TYPE,
    ).strip()
    cache_dir = Path(
        os.environ.get("WHISPERX_CACHE_DIR", "./var/cuda-cache/whisperx")
    ).expanduser()
    cache_dir.mkdir(parents=True, exist_ok=True)
    batch_size = int(options.get("batch_size", 1))
    threads = options.get("threads")
    started = time.monotonic()

    audio = whisperx.load_audio(str(audio_path))
    model = whisperx.load_model(
        model_name,
        device_kind,
        device_index=device_index,
        compute_type=compute_type,
        language=language,
        download_root=str(cache_dir),
        threads=int(threads) if threads is not None else 4,
        use_auth_token=hf_token,
    )
    try:
        result = model.transcribe(
            audio,
            batch_size=batch_size,
            language=language,
            chunk_size=int(options["chunk_length_seconds"]),
        )
    finally:
        del model
        _release_cuda()

    align_model, align_metadata = whisperx.load_align_model(
        language_code=language,
        device=device,
        model_dir=str(cache_dir),
    )
    try:
        result = whisperx.align(
            result["segments"],
            align_model,
            align_metadata,
            audio,
            device,
            return_char_alignments=False,
        )
    finally:
        del align_model
        _release_cuda()

    diarization = DiarizationPipeline(
        token=hf_token,
        device=diarization_device,
        cache_dir=str(cache_dir),
    )
    try:
        diarization_segments = diarization(
            audio,
            num_speakers=options.get("num_speakers"),
            min_speakers=options.get("min_speakers"),
            max_speakers=options.get("max_speakers"),
        )
        result = whisperx.assign_word_speakers(
            diarization_segments,
            result,
            fill_nearest=True,
        )
    finally:
        del diarization
        _release_cuda()

    return {
        "model": {
            "id": model_name,
            "revision": f"whisperx-{installed_version}",
        },
        "timing": {
            "postprocessor": WHISPERX_TIMESTAMP_POSTPROCESSOR,
        },
        "runtime": {
            "backend": "whisperx",
            "device": device,
            "diarization_device": diarization_device,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        },
        "noise_filter": {
            "enabled": True,
            "provider": "whisperx-vad",
            "removed_count": 0,
            "removed_spans": [],
        },
        "segments": normalize_whisperx_segments(result.get("segments", [])),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one isolated WhisperX job")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--options", required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    options = json.loads(args.options)
    if not isinstance(options, Mapping):
        raise ValueError("WhisperX options must be a JSON object")
    payload = run_whisperx(args.audio, options)
    write_json_atomic(args.output, payload)


if __name__ == "__main__":
    main()
