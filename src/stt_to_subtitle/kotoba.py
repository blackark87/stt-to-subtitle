"""Kotoba-Whisper loading and result normalization."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import importlib
from pathlib import Path
from types import MethodType
from typing import Any, Mapping, Protocol, Sequence

MODEL_ID = "kotoba-tech/kotoba-whisper-v2.2"
MODEL_REVISION = "9d33482a0eb9b57f1ad80708e8ac5538246d8355"
TIMESTAMP_POSTPROCESSOR = "stt-to-subtitle/kotoba-speaker-span-v1"


@dataclass(frozen=True)
class TranscriptionOptions:
    batch_size: int = 1
    chunk_length_seconds: int = 15
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    add_punctuation: bool = False
    threads: int | None = None

    def validate(self) -> None:
        if self.batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        if self.chunk_length_seconds < 1:
            raise ValueError("chunk_length_seconds must be at least 1")
        if self.threads is not None and self.threads < 1:
            raise ValueError("threads must be at least 1")
        speaker_values = (
            self.num_speakers,
            self.min_speakers,
            self.max_speakers,
        )
        if any(value is not None and value < 1 for value in speaker_values):
            raise ValueError("speaker counts must be at least 1")
        if self.num_speakers is not None and (
            self.min_speakers is not None or self.max_speakers is not None
        ):
            raise ValueError(
                "num_speakers cannot be combined with min_speakers or max_speakers"
            )
        if (
            self.min_speakers is not None
            and self.max_speakers is not None
            and self.min_speakers > self.max_speakers
        ):
            raise ValueError("min_speakers cannot exceed max_speakers")


class SpeechPipeline(Protocol):
    def __call__(self, audio_path: str, **kwargs: Any) -> Mapping[str, Any]: ...


def _speaker_group_key(output: Mapping[str, Any]) -> tuple[str, float, float]:
    speaker = str(output.get("speaker_id", "UNKNOWN"))
    span = output.get("speaker_span")
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        raise ValueError("Kotoba output does not contain a valid speaker span")
    try:
        start = float(span[0])
        end = float(span[1])
    except (TypeError, ValueError) as error:
        raise ValueError("Kotoba output contains an invalid speaker span") from error
    if start < 0 or end < start:
        raise ValueError("Kotoba output contains an invalid speaker span")
    return speaker, start, end


def _decode_speaker_group(
    speech_pipeline: Any,
    model_outputs: Sequence[Mapping[str, Any]],
    *,
    speaker: str,
    span_start: float,
    span_end: float,
    return_language: bool,
    return_timestamps: bool,
) -> tuple[str, list[dict[str, Any]]]:
    sampling_rate = float(speech_pipeline.feature_extractor.sampling_rate)
    if sampling_rate <= 0:
        raise ValueError("Kotoba feature extractor has an invalid sampling rate")

    prepared_outputs: list[dict[str, Any]] = []
    for output in model_outputs:
        prepared = dict(output)
        stride = prepared.get("stride")
        if isinstance(stride, (list, tuple)) and len(stride) == 3:
            prepared["stride"] = tuple(
                float(value) / sampling_rate for value in stride
            )
        prepared_outputs.append(prepared)

    time_precision = (
        float(speech_pipeline.feature_extractor.chunk_length)
        / float(speech_pipeline.model.config.max_source_positions)
    )
    text, optional = speech_pipeline.tokenizer._decode_asr(
        prepared_outputs,
        return_language=return_language,
        return_timestamps=return_timestamps,
        time_precision=time_precision,
    )
    raw_chunks = optional.get("chunks", []) if isinstance(optional, Mapping) else []
    corrected: list[dict[str, Any]] = []
    for raw_chunk in raw_chunks:
        if not isinstance(raw_chunk, Mapping):
            continue
        timestamp = raw_chunk.get("timestamp")
        if not isinstance(timestamp, (list, tuple)) or len(timestamp) != 2:
            continue
        if timestamp[0] is None or timestamp[1] is None:
            continue
        relative_start = max(0.0, float(timestamp[0]))
        relative_end = max(relative_start, float(timestamp[1]))
        absolute_start = min(span_end, span_start + relative_start)
        absolute_end = min(span_end, span_start + relative_end)
        if absolute_end <= absolute_start and span_end > absolute_start:
            absolute_end = min(span_end, absolute_start + 0.1)
        corrected.append(
            {
                **dict(raw_chunk),
                "timestamp": [
                    round(absolute_start, 3),
                    round(absolute_end, 3),
                ],
                "speaker_id": speaker,
            }
        )
    if not corrected and str(text).strip() and span_end > span_start:
        corrected.append(
            {
                "text": str(text).strip(),
                "timestamp": [round(span_start, 3), round(span_end, 3)],
                "speaker_id": speaker,
            }
        )
    return str(text), corrected


def _load_punctuator(speech_pipeline: Any) -> Any:
    punctuator = getattr(speech_pipeline, "punctuator", None)
    if punctuator is not None:
        return punctuator
    module = importlib.import_module(type(speech_pipeline).__module__)
    factory = getattr(module, "Punctuator", None)
    if not callable(factory):
        raise RuntimeError("Kotoba punctuation model is unavailable")
    punctuator = factory()
    speech_pipeline.punctuator = punctuator
    return punctuator


def corrected_kotoba_postprocess(
    speech_pipeline: Any,
    model_outputs: Sequence[Mapping[str, Any]],
    **postprocess_parameters: Any,
) -> Mapping[str, Any]:
    """Stitch each speaker turn once and preserve decoded start/end timestamps."""
    grouped: dict[
        tuple[str, float, float],
        list[Mapping[str, Any]],
    ] = {}
    for output in model_outputs:
        if not isinstance(output, Mapping):
            continue
        grouped.setdefault(_speaker_group_key(output), []).append(output)

    output_chunks: list[dict[str, Any]] = []
    speaker_texts: dict[str, list[tuple[float, str]]] = {}
    return_language = bool(postprocess_parameters.get("return_language", False))
    return_timestamps = bool(
        postprocess_parameters.get("return_timestamps", True)
    )
    for (speaker, span_start, span_end), group in grouped.items():
        text, chunks = _decode_speaker_group(
            speech_pipeline,
            group,
            speaker=speaker,
            span_start=span_start,
            span_end=span_end,
            return_language=return_language,
            return_timestamps=return_timestamps,
        )
        output_chunks.extend(chunks)
        if text.strip():
            speaker_texts.setdefault(speaker, []).append((span_start, text))

    output_chunks.sort(
        key=lambda item: (
            float(item["timestamp"][0]),
            float(item["timestamp"][1]),
            str(item["speaker_id"]),
        )
    )
    speaker_ids = sorted(
        {
            str(chunk["speaker_id"])
            for chunk in output_chunks
        }
        | set(speaker_texts)
    )
    result: dict[str, Any] = {
        "chunks": output_chunks,
        "speaker_ids": speaker_ids,
        "timestamp_postprocessor": TIMESTAMP_POSTPROCESSOR,
    }
    add_punctuation = bool(
        postprocess_parameters.get("add_punctuation", False)
    )
    punctuator = _load_punctuator(speech_pipeline) if add_punctuation else None
    for speaker in speaker_ids:
        chunks = [
            chunk
            for chunk in output_chunks
            if str(chunk["speaker_id"]) == speaker
        ]
        result[f"chunks/{speaker}"] = chunks
        joined = "".join(
            text
            for _start, text in sorted(speaker_texts.get(speaker, []))
        )
        result[f"text/{speaker}"] = (
            punctuator.punctuate(joined)
            if punctuator is not None and joined
            else joined
        )
    return result


def install_corrected_kotoba_postprocess(speech_pipeline: Any) -> None:
    """Override the pinned remote postprocessor without editing model caches."""
    speech_pipeline.postprocess = MethodType(
        corrected_kotoba_postprocess,
        speech_pipeline,
    )


@dataclass(frozen=True)
class ChunkProgress:
    """Observed chunk counts inside the Kotoba preprocessing/inference pipeline."""

    created: int
    completed: int
    final: bool = False

    @property
    def in_progress(self) -> int:
        return max(0, self.created - self.completed)

    def public_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "completed": self.completed,
            "in_progress": self.in_progress,
        }


class _ChunkProgressTracker:
    def __init__(
        self,
        callback: Callable[[ChunkProgress], None],
        report_every: int,
    ) -> None:
        self.callback = callback
        self.report_every = report_every
        self.created = 0
        self.completed = 0
        self._last_emitted: tuple[int, int] | None = None

    def chunk_created(self) -> None:
        self.created += 1
        self._emit()

    def chunks_completed(self, count: int) -> None:
        self.completed = min(self.created, self.completed + count)
        self._emit()

    def finish(self) -> None:
        self._emit(final=True)

    def _emit(self, *, final: bool = False) -> None:
        current = (self.created, self.completed)
        if current == self._last_emitted and not final:
            return
        self._last_emitted = current
        self.callback(ChunkProgress(*current, final=final))


def _model_input_count(model_inputs: Any) -> int:
    if not isinstance(model_inputs, Mapping):
        return 1
    input_features = model_inputs.get("input_features")
    shape = getattr(input_features, "shape", None)
    if shape is None or len(shape) < 1:
        return 1
    try:
        return max(1, int(shape[0]))
    except (TypeError, ValueError):
        return 1


def _run_with_chunk_progress(
    speech_pipeline: SpeechPipeline,
    audio_path: Path,
    options: TranscriptionOptions,
    callback: Callable[[ChunkProgress], None],
    report_every: int,
) -> Mapping[str, Any]:
    preprocess = getattr(speech_pipeline, "preprocess", None)
    forward = getattr(speech_pipeline, "_forward", None)
    if not callable(preprocess) or not callable(forward):
        raise TypeError(
            "the loaded speech pipeline does not expose chunk progress hooks"
        )

    tracker = _ChunkProgressTracker(callback, report_every)
    instance_attributes = getattr(speech_pipeline, "__dict__", {})
    had_preprocess_override = "preprocess" in instance_attributes
    had_forward_override = "_forward" in instance_attributes
    previous_preprocess_override = instance_attributes.get("preprocess")
    previous_forward_override = instance_attributes.get("_forward")

    def tracked_preprocess(*args: Any, **kwargs: Any) -> Iterator[Any]:
        for item in preprocess(*args, **kwargs):
            tracker.chunk_created()
            yield item

    def tracked_forward(model_inputs: Any, **kwargs: Any) -> Any:
        chunk_count = _model_input_count(model_inputs)
        result = forward(model_inputs, **kwargs)
        tracker.chunks_completed(chunk_count)
        return result

    setattr(speech_pipeline, "preprocess", tracked_preprocess)
    setattr(speech_pipeline, "_forward", tracked_forward)
    try:
        return speech_pipeline(
            str(audio_path),
            chunk_length_s=options.chunk_length_seconds,
            add_punctuation=options.add_punctuation,
            num_speakers=options.num_speakers,
            min_speakers=options.min_speakers,
            max_speakers=options.max_speakers,
        )
    finally:
        try:
            tracker.finish()
        finally:
            if had_preprocess_override:
                setattr(
                    speech_pipeline,
                    "preprocess",
                    previous_preprocess_override,
                )
            else:
                delattr(speech_pipeline, "preprocess")
            if had_forward_override:
                setattr(
                    speech_pipeline,
                    "_forward",
                    previous_forward_override,
                )
            else:
                delattr(speech_pipeline, "_forward")


def load_pipeline(
    token: str,
    *,
    batch_size: int = 1,
    device: str = "cpu",
    diarization_device: str | None = None,
    threads: int | None = None,
) -> SpeechPipeline:
    """Load the pinned pipeline with independently selected model devices."""
    if not token.strip():
        raise ValueError("HF_TOKEN is required for gated Pyannote models")
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if threads is not None and threads < 1:
        raise ValueError("threads must be at least 1")

    import torch
    from transformers import pipeline

    if threads is not None:
        torch.set_num_threads(threads)

    torch_dtype = torch.float16 if device == "mps" else torch.float32
    pipeline_options: dict[str, Any] = {
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "token": token,
        "torch_dtype": torch_dtype,
        "device": device,
        "batch_size": batch_size,
        "trust_remote_code": True,
    }
    if diarization_device is not None:
        pipeline_options["device_pyannote"] = diarization_device

    speech_pipeline = pipeline(**pipeline_options)
    install_corrected_kotoba_postprocess(speech_pipeline)
    return speech_pipeline


def run_pipeline(
    speech_pipeline: SpeechPipeline,
    audio_path: Path,
    options: TranscriptionOptions,
    *,
    progress_callback: Callable[[ChunkProgress], None] | None = None,
    progress_every: int = 10,
) -> Mapping[str, Any]:
    """Run one validated transcription against an already loaded pipeline."""
    options.validate()
    if progress_every not in {10, 100}:
        raise ValueError("progress_every must be either 10 or 100")
    if progress_callback is not None:
        return _run_with_chunk_progress(
            speech_pipeline,
            audio_path,
            options,
            progress_callback,
            progress_every,
        )
    return speech_pipeline(
        str(audio_path),
        chunk_length_s=options.chunk_length_seconds,
        add_punctuation=options.add_punctuation,
        num_speakers=options.num_speakers,
        min_speakers=options.min_speakers,
        max_speakers=options.max_speakers,
    )


def transcribe(
    audio_path: Path,
    token: str,
    options: TranscriptionOptions,
) -> Mapping[str, Any]:
    """Load the pinned Kotoba pipeline and transcribe one WAV file on CPU."""
    options.validate()
    speech_pipeline = load_pipeline(
        token=token,
        batch_size=options.batch_size,
        device="cpu",
        threads=options.threads,
    )
    return run_pipeline(speech_pipeline, audio_path, options)


def normalize_segments(
    result: Mapping[str, Any],
    *,
    offset_seconds: float = 0.0,
) -> list[dict[str, Any]]:
    """Convert the custom pipeline's chunks into stable JSON records."""
    normalized: list[dict[str, Any]] = []
    chunks = result.get("chunks", [])
    if not isinstance(chunks, list):
        raise ValueError("Kotoba result does not contain a chunk list")

    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        text = str(chunk.get("text", "")).strip()
        if not text:
            continue
        timestamp = chunk.get("timestamp")
        if not isinstance(timestamp, (list, tuple)) or len(timestamp) != 2:
            continue
        if timestamp[0] is None or timestamp[1] is None:
            continue
        start = max(0.0, float(timestamp[0])) + offset_seconds
        end = max(float(timestamp[1]), float(timestamp[0])) + offset_seconds
        normalized.append(
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "speaker": str(chunk.get("speaker_id", "UNKNOWN")),
                "text": text,
            }
        )

    return sorted(
        normalized,
        key=lambda item: (item["start"], item["end"], item["speaker"]),
    )


def speaker_transcripts(result: Mapping[str, Any]) -> dict[str, str]:
    """Collect the pipeline's per-speaker text, including optional punctuation."""
    transcripts: dict[str, str] = {}
    speaker_ids = result.get("speaker_ids", [])
    if not isinstance(speaker_ids, list):
        return transcripts
    for speaker in speaker_ids:
        speaker_id = str(speaker)
        text = str(result.get(f"text/{speaker_id}", "")).strip()
        if text:
            transcripts[speaker_id] = text
    return transcripts
