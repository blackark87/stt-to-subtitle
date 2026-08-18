"""Assign Pyannote speakers to pre-aligned words in the WhisperX venv."""

from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .files import write_json_atomic
from .whisperx_worker import (
    WhisperXSegmentationOptions,
    diarization_records,
    rebuild_whisperx_segments,
)


def _release_cuda() -> None:
    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def _overlap(
    first_start: float,
    first_end: float,
    second_start: float,
    second_end: float,
) -> float:
    return max(0.0, min(first_end, second_end) - max(first_start, second_start))


def assign_word_speakers(
    words: Sequence[Mapping[str, Any]],
    diarization: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Use maximum overlap, then nearest diarization span as a fallback."""
    assigned: list[dict[str, Any]] = []
    fallback_count = 0
    for raw_word in words:
        word = dict(raw_word)
        start = float(word.get("start", 0.0))
        end = max(start, float(word.get("end", start)))
        ranked = sorted(
            (
                (
                    _overlap(
                        start,
                        end,
                        float(span["start"]),
                        float(span["end"]),
                    ),
                    str(span["speaker"]),
                    span,
                )
                for span in diarization
            ),
            key=lambda item: (item[0], item[1]),
            reverse=True,
        )
        if ranked and ranked[0][0] > 0:
            speaker = ranked[0][1]
            source = "pyannote_word_overlap"
        elif ranked:
            center = (start + end) / 2
            nearest = min(
                diarization,
                key=lambda span: min(
                    abs(center - float(span["start"])),
                    abs(center - float(span["end"])),
                ),
            )
            speaker = str(nearest["speaker"])
            source = "pyannote_nearest_fallback"
            fallback_count += 1
        else:
            speaker = "UNKNOWN"
            source = "diarization_unavailable"
            fallback_count += 1
        word["speaker"] = speaker
        word["speaker_source"] = source
        assigned.append(word)
    return assigned, fallback_count


def run_speaker_assignment(
    audio_path: Path,
    payload: Mapping[str, Any],
    options: Mapping[str, Any],
    *,
    debug_artifact_dir: Path | None = None,
) -> dict[str, Any]:
    import whisperx
    from whisperx.diarize import DiarizationPipeline

    started = time.monotonic()
    raw_words = payload.get("words", [])
    if not isinstance(raw_words, list):
        raise ValueError("WhisperJAV result has no words list")
    hf_token = os.environ.get("HF_TOKEN", "").strip()
    if not hf_token:
        raise ValueError("HF_TOKEN is required for WhisperJAV diarization")
    device = os.environ.get("STT_DIARIZATION_DEVICE", "cuda")
    cache_dir = Path(
        os.environ.get("WHISPERX_CACHE_DIR", "./var/cuda-cache/whisperx")
    ).expanduser()
    audio = whisperx.load_audio(str(audio_path))
    diarizer = DiarizationPipeline(
        token=hf_token,
        device=device,
        cache_dir=str(cache_dir),
    )
    try:
        raw_diarization = diarizer(
            audio,
            num_speakers=options.get("num_speakers"),
            min_speakers=options.get("min_speakers"),
            max_speakers=options.get("max_speakers"),
        )
        diarization = diarization_records(raw_diarization)
    finally:
        del diarizer
        _release_cuda()
    if not diarization and raw_words:
        raise RuntimeError("Pyannote returned no speaker spans")
    words, fallback_count = assign_word_speakers(raw_words, diarization)
    segments = rebuild_whisperx_segments(
        words,
        WhisperXSegmentationOptions.from_options(options),
    )
    if raw_words and not segments:
        raise RuntimeError("speaker assignment produced no subtitle segments")
    if debug_artifact_dir is not None:
        debug_artifact_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            debug_artifact_dir / "diarization.json",
            {"spans": diarization},
        )
        write_json_atomic(
            debug_artifact_dir / "speaker_words.json",
            {"words": words, "fallback_count": fallback_count},
        )
    result = dict(payload)
    result["words"] = words
    result["segments"] = segments
    runtime = result.get("runtime", {})
    result["runtime"] = {
        **(dict(runtime) if isinstance(runtime, Mapping) else {}),
        "diarization_device": device,
        "diarization_elapsed_seconds": round(time.monotonic() - started, 3),
        "diarization_pass_count": 1,
    }
    quality = result.get("quality", {})
    result["quality"] = {
        **(dict(quality) if isinstance(quality, Mapping) else {}),
        "speaker_assignment": {
            "provider": "pyannote-word-overlap-v1",
            "diarization_span_count": len(diarization),
            "fallback_count": fallback_count,
        },
    }
    result["noise_filter"] = {
        "enabled": True,
        "provider": "pyannote",
        "execution_state": "speaker_assignment",
        "candidate_count": len(diarization),
        "kept_count": len(diarization),
        "removed_count": 0,
        "removed_spans": [],
    }
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assign speakers to aligned words")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--options", required=True)
    parser.add_argument("--debug-dir", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    options = json.loads(args.options)
    if not isinstance(payload, Mapping) or not isinstance(options, Mapping):
        raise ValueError("speaker worker input must be JSON objects")
    write_json_atomic(
        args.output,
        run_speaker_assignment(
            args.audio,
            payload,
            options,
            debug_artifact_dir=args.debug_dir,
        ),
    )


if __name__ == "__main__":
    main()
