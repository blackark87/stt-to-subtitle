"""Isolated WhisperX worker used by the native transcription API."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gc
from importlib.metadata import version
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping, Sequence

from .files import write_json_atomic
from .stt_quality import repetition_diagnostics
from .stt_trace import StageArtifactRecorder

WHISPERX_PACKAGE_VERSION = "3.8.6"
DEFAULT_WHISPERX_MODEL = "large-v3"
DEFAULT_WHISPERX_LANGUAGE = "ja"
DEFAULT_WHISPERX_COMPUTE_TYPE = "float16"
WHISPERX_TIMESTAMP_POSTPROCESSOR = "whisperx-forced-alignment"


@dataclass(frozen=True)
class WhisperXSegmentationOptions:
    """Configurable word-level subtitle boundaries."""

    split_on_speaker_change: bool = True
    max_gap_sec: float | None = None
    max_duration_sec: float | None = None
    max_chars: int | None = None
    prefer_punctuation_boundary: bool = True

    @classmethod
    def from_options(
        cls,
        options: Mapping[str, Any],
    ) -> WhisperXSegmentationOptions:
        raw = options.get("subtitle_segmentation", {})
        if not isinstance(raw, Mapping):
            raise ValueError("subtitle_segmentation must be a JSON object")
        allowed = {
            "split_on_speaker_change",
            "max_gap_sec",
            "max_duration_sec",
            "max_chars",
            "prefer_punctuation_boundary",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"unsupported subtitle_segmentation options: {sorted(unknown)}"
            )

        def optional_float(name: str) -> float | None:
            value = raw.get(name)
            if value is None:
                return None
            converted = float(value)
            if converted <= 0:
                raise ValueError(f"{name} must be positive or null")
            return converted

        max_chars_value = raw.get("max_chars")
        max_chars = int(max_chars_value) if max_chars_value is not None else None
        if max_chars is not None and max_chars < 1:
            raise ValueError("max_chars must be positive or null")
        for name in (
            "split_on_speaker_change",
            "prefer_punctuation_boundary",
        ):
            if name in raw and not isinstance(raw[name], bool):
                raise ValueError(f"{name} must be a JSON boolean")
        return cls(
            split_on_speaker_change=raw.get(
                "split_on_speaker_change", True
            ),
            max_gap_sec=optional_float("max_gap_sec"),
            max_duration_sec=optional_float("max_duration_sec"),
            max_chars=max_chars,
            prefer_punctuation_boundary=raw.get(
                "prefer_punctuation_boundary", True
            ),
        )


def _timestamp(value: Any) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, converted), 3)


def extract_whisperx_words(
    raw_segments: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Preserve aligned word timestamps, speakers, scores, and lineage."""
    words: list[dict[str, Any]] = []
    for segment_index, segment in enumerate(raw_segments, start=1):
        raw_words = segment.get("words", [])
        if not isinstance(raw_words, Sequence) or isinstance(
            raw_words, (str, bytes, bytearray)
        ):
            continue
        parent_id = f"whisperx-segment-{segment_index:06d}"
        segment_start = _timestamp(segment.get("start"))
        segment_end = _timestamp(segment.get("end"))
        for raw_word in raw_words:
            if not isinstance(raw_word, Mapping):
                continue
            text = str(raw_word.get("word", ""))
            if not text.strip():
                continue
            start = _timestamp(raw_word.get("start"))
            end = _timestamp(raw_word.get("end"))
            start_from_parent = start is None
            end_from_parent = end is None
            if start is None:
                start = segment_start
            if end is None:
                end = segment_end
            if start is None or end is None:
                continue
            end = max(start, end)
            word_id = f"word-{len(words) + 1:06d}"
            score = raw_word.get("score")
            try:
                normalized_score = float(score) if score is not None else None
            except (TypeError, ValueError):
                normalized_score = None
            words.append(
                {
                    "word_id": word_id,
                    "span_id": word_id,
                    "parent_span_ids": [parent_id],
                    "stage": "speaker_assigned",
                    "word": text,
                    "start": start,
                    "end": end,
                    "duration": round(max(0.0, end - start), 3),
                    "timestamp_fallback": (
                        start_from_parent or end_from_parent
                    ),
                    "timestamp_source": (
                        "parent_segment_fallback"
                        if start_from_parent or end_from_parent
                        else "word_alignment"
                    ),
                    "speaker": str(
                        raw_word.get(
                            "speaker",
                            segment.get("speaker", "UNKNOWN"),
                        )
                    ),
                    "score": normalized_score,
                    "provider": "whisperx-word-alignment",
                    "decision": "keep",
                    "reason_codes": [],
                }
            )
    return words


def traced_whisperx_segments(
    raw_segments: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach stable IDs to speaker-assigned parent segments for artifacts."""
    return [
        {
            **dict(segment),
            "span_id": f"whisperx-segment-{index:06d}",
            "parent_span_ids": [],
            "stage": "speaker_assigned",
            "provider": "whisperx",
        }
        for index, segment in enumerate(raw_segments, start=1)
        if isinstance(segment, Mapping)
    ]


def _joined_word_text(words: Sequence[Mapping[str, Any]]) -> str:
    return "".join(str(word.get("word", "")) for word in words).strip()


def _ends_with_punctuation(text: str) -> bool:
    return text.rstrip().endswith(
        ("。", "！", "？", "!", "?", ".", "…", "、", ",")
    )


def rebuild_whisperx_segments(
    words: Sequence[Mapping[str, Any]],
    config: WhisperXSegmentationOptions,
) -> list[dict[str, Any]]:
    """Build subtitle segments from words while retaining their IDs."""
    if not words:
        return []
    ordered = sorted(
        words,
        key=lambda word: (
            float(word["start"]),
            float(word["end"]),
            str(word["word_id"]),
        ),
    )
    output: list[dict[str, Any]] = []
    current: list[Mapping[str, Any]] = []

    def flush() -> None:
        if not current:
            return
        text = _joined_word_text(current)
        if not text:
            current.clear()
            return
        start = float(current[0]["start"])
        end = max(float(word["end"]) for word in current)
        speakers = {str(word.get("speaker", "UNKNOWN")) for word in current}
        output.append(
            {
                "start": round(start, 3),
                "end": round(max(start, end), 3),
                "speaker": (
                    next(iter(speakers)) if len(speakers) == 1 else "MULTIPLE"
                ),
                "text": text,
                "word_ids": [str(word["word_id"]) for word in current],
                "parent_span_ids": [str(word["word_id"]) for word in current],
                "provider": "whisperx-word-segmentation-v1",
                "decision": "keep",
                "reason_codes": [],
            }
        )
        current.clear()

    for word in ordered:
        if not current:
            current.append(word)
            continue
        previous = current[-1]
        candidate_text = _joined_word_text([*current, word])
        should_split = bool(
            config.split_on_speaker_change
            and str(word.get("speaker", "UNKNOWN"))
            != str(previous.get("speaker", "UNKNOWN"))
        )
        if (
            not should_split
            and config.max_gap_sec is not None
            and float(word["start"]) - float(previous["end"])
            > config.max_gap_sec
        ):
            should_split = True
        if (
            not should_split
            and config.max_duration_sec is not None
            and float(word["end"]) - float(current[0]["start"])
            > config.max_duration_sec
        ):
            should_split = True
        if (
            not should_split
            and config.max_chars is not None
            and len(candidate_text) > config.max_chars
        ):
            should_split = True
        if (
            not should_split
            and config.prefer_punctuation_boundary
            and _ends_with_punctuation(str(previous.get("word", "")))
        ):
            should_split = True
        if should_split:
            flush()
        current.append(word)
    flush()
    return output


def diarization_records(diarization: Any) -> list[dict[str, Any]]:
    """Convert the WhisperX diarization table to JSON trace records."""
    records: list[dict[str, Any]] = []
    iterrows = getattr(diarization, "iterrows", None)
    if not callable(iterrows):
        return records
    for index, (time_span, row) in enumerate(iterrows(), start=1):
        row_get = getattr(row, "get", None)
        start = getattr(time_span, "start", None)
        end = getattr(time_span, "end", None)
        if callable(row_get):
            start = row_get("start", start)
            end = row_get("end", end)
            speaker = row_get("speaker", row_get("label", "UNKNOWN"))
        else:
            speaker = "UNKNOWN"
        start_value = _timestamp(start)
        end_value = _timestamp(end)
        if start_value is None or end_value is None:
            continue
        records.append(
            {
                "span_id": f"whisperx-dia-{index:06d}",
                "parent_span_ids": [],
                "stage": "diarization_raw",
                "start": start_value,
                "end": max(start_value, end_value),
                "duration": round(max(0.0, end_value - start_value), 3),
                "speaker": str(speaker),
                "provider": "whisperx-diarization",
                "decision": "keep",
                "reason_codes": [],
            }
        )
    return records


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
    *,
    debug_artifact_dir: Path | None = None,
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
    segmentation = WhisperXSegmentationOptions.from_options(options)
    repetition_policy = str(options.get("repetition_policy", "flag"))
    if repetition_policy not in {"flag", "reject"}:
        raise ValueError("repetition_policy must be 'flag' or 'reject'")
    repetition_min_count = int(options.get("repetition_min_count", 8))
    if repetition_min_count < 2:
        raise ValueError("repetition_min_count must be at least 2")
    started = time.monotonic()
    recorder = StageArtifactRecorder(debug_artifact_dir)

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
        recorder.record(
            "11_transcribe_raw.json",
            "whisperx_transcribe_raw",
            result,
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
        recorder.record(
            "12_aligned.json",
            "whisperx_aligned",
            result,
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
        recorder.record(
            "13_diarization_raw.json",
            "whisperx_diarization_raw",
            diarization_records(diarization_segments),
        )
        result = whisperx.assign_word_speakers(
            diarization_segments,
            result,
            fill_nearest=True,
        )
    finally:
        del diarization
        _release_cuda()

    raw_segments = result.get("segments", [])
    if not isinstance(raw_segments, list):
        raw_segments = []
    words = extract_whisperx_words(raw_segments)
    recorder.record(
        "14_word_speakers.json",
        "whisperx_word_speakers",
        {
            "segments": traced_whisperx_segments(raw_segments),
            "words": words,
        },
    )
    segments = rebuild_whisperx_segments(words, segmentation)
    if not segments:
        segments = normalize_whisperx_segments(raw_segments)
    raw_repetition = repetition_diagnostics(
        [str(segment.get("text", "")) for segment in raw_segments],
        minimum_count=repetition_min_count,
    )
    final_repetition = repetition_diagnostics(
        [str(segment.get("text", "")) for segment in segments],
        minimum_count=repetition_min_count,
    )
    repetition_flagged = bool(
        raw_repetition["flagged"] or final_repetition["flagged"]
    )
    quality = {
        "repetition": {
            "policy": repetition_policy,
            "flagged": repetition_flagged,
            "raw": raw_repetition,
            "final": final_repetition,
        },
        "encoding_warning": recorder.encoding_warning(),
    }
    recorder.record(
        "15_subtitle_segments.json",
        "whisperx_subtitle_segments",
        {
            "segmentation": {
                "split_on_speaker_change": segmentation.split_on_speaker_change,
                "max_gap_sec": segmentation.max_gap_sec,
                "max_duration_sec": segmentation.max_duration_sec,
                "max_chars": segmentation.max_chars,
                "prefer_punctuation_boundary": (
                    segmentation.prefer_punctuation_boundary
                ),
            },
            "words": words,
            "segments": segments,
            "quality": quality,
        },
    )
    quality["encoding_warning"] = recorder.encoding_warning()
    if repetition_flagged and repetition_policy == "reject":
        raise RuntimeError(
            "WhisperX output rejected by repetition-v1 quality policy"
        )

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
            "execution_state": "not_observable",
            "candidate_count": None,
            "kept_count": None,
            "removed_count": None,
            "removed_duration_sum_sec": None,
            "removed_duration_union_sec": None,
            "removed_spans": [],
        },
        "words": words,
        "quality": quality,
        "segments": segments,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one isolated WhisperX job")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--options", required=True)
    parser.add_argument("--debug-dir", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    options = json.loads(args.options)
    if not isinstance(options, Mapping):
        raise ValueError("WhisperX options must be a JSON object")
    payload = run_whisperx(
        args.audio,
        options,
        debug_artifact_dir=args.debug_dir,
    )
    write_json_atomic(args.output, payload)


if __name__ == "__main__":
    main()
