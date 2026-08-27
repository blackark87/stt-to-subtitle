"""Kotoba-Whisper loading and result normalization."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import importlib
import logging
from pathlib import Path
import re
from types import MethodType
from typing import Any, Mapping, Protocol, Sequence

from .stt_quality import (
    annotate_span_diagnostics,
    interval_durations,
    short_span_diagnostics,
)
from .stt_trace import StageArtifactRecorder
from .stt_options import (
    DEFAULT_CHUNK_LENGTH_SECONDS,
    DEFAULT_NOISE_FILTER_TRIGGER_LEVEL,
    TranscriptionOptions,
)

MODEL_ID = "kotoba-tech/kotoba-whisper-v2.2"
MODEL_REVISION = "9d33482a0eb9b57f1ad80708e8ac5538246d8355"
DEVICE_PATTERN = re.compile(r"^(?:cpu|mps|cuda(?::\d+)?)$")
TIMESTAMP_POSTPROCESSOR = "stt-to-subtitle/kotoba-speaker-span-v1"
LOGGER = logging.getLogger(__name__)


class SpeechPipeline(Protocol):
    def __call__(self, audio_path: str, **kwargs: Any) -> Mapping[str, Any]: ...


def validate_device(device: str, *, setting: str = "device") -> None:
    """Validate a PyTorch device accepted by the native STT service."""
    if not DEVICE_PATTERN.fullmatch(device):
        raise ValueError(
            f"{setting} must be cpu, mps, cuda, or cuda:<non-negative index>"
        )


def corrected_kotoba_chunk_iter(
    inputs: Any,
    feature_extractor: Any,
    chunk_len: int,
    stride_left: int,
    stride_right: int,
    dtype: Any = None,
) -> Iterator[dict[str, Any]]:
    """Keep chunks longer than 30 seconds intact for Whisper long-form mode."""
    inputs_len = int(inputs.shape[0])
    step = chunk_len - stride_left - stride_right
    if step <= 0:
        raise ValueError("chunk length must exceed combined stride length")

    for chunk_start in range(0, inputs_len, step):
        chunk_end = chunk_start + chunk_len
        chunk = inputs[chunk_start:chunk_end]
        extractor_options: dict[str, Any] = {
            "sampling_rate": feature_extractor.sampling_rate,
            "return_tensors": "pt",
            "return_attention_mask": True,
        }
        maximum_samples = int(
            getattr(feature_extractor, "n_samples", chunk.shape[0])
        )
        if int(chunk.shape[0]) > maximum_samples:
            extractor_options.update(
                {
                    "truncation": False,
                    "padding": "longest",
                }
            )
        processed = feature_extractor(chunk, **extractor_options)
        if dtype is not None:
            processed = processed.to(dtype=dtype)

        actual_length = int(chunk.shape[0])
        current_stride_left = 0 if chunk_start == 0 else stride_left
        is_last = chunk_end >= inputs_len
        current_stride_right = 0 if is_last else stride_right
        if actual_length > current_stride_left:
            yield {
                "is_last": is_last,
                "stride": (
                    actual_length,
                    current_stride_left,
                    current_stride_right,
                ),
                **processed,
            }
        if is_last:
            break


def _contains_voice(
    samples: Any,
    sampling_rate: int,
    trigger_level: float,
) -> bool:
    """Use the pinned Torchaudio cepstral VAD as a second speech gate."""
    import numpy as np
    import torch
    from torch.nn import functional as torch_functional
    from torchaudio.functional import vad

    audio = np.asarray(samples, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return False
    audio = np.nan_to_num(audio, copy=True)
    if float(np.max(np.abs(audio))) < 1e-5:
        return False

    waveform = torch.from_numpy(audio)
    padding_samples = max(1, round(sampling_rate * 0.25))
    padded = torch_functional.pad(
        waveform,
        (padding_samples, padding_samples),
    )
    detected = vad(
        padded,
        sampling_rate,
        trigger_level=trigger_level,
        trigger_time=0.15,
        search_time=0.25,
        allowed_gap=0.1,
        boot_time=0.1,
    )
    return int(detected.numel()) > 0


class NoiseFilteringSpeakerDiarization:
    """Drop diarized spans that an independent voice detector rejects."""

    def __init__(
        self,
        delegate: Any,
        *,
        detector: Callable[[Any, int, float], bool] = _contains_voice,
    ) -> None:
        self.delegate = delegate
        self.detector = detector
        self.enabled = True
        self.trigger_level = DEFAULT_NOISE_FILTER_TRIGGER_LEVEL
        self.removed_spans: list[dict[str, Any]] = []
        self.candidate_spans: list[dict[str, Any]] = []
        self.kept_spans: list[dict[str, Any]] = []
        self.execution_state = "not_run"
        self.error_count = 0

    def __call__(
        self,
        audio: Any,
        sampling_rate: int,
        **kwargs: Any,
    ) -> Any:
        flags = getattr(audio, "flags", None)
        if flags is not None and not bool(getattr(flags, "writeable", True)):
            audio = audio.copy()
        annotation = self.delegate(
            audio,
            sampling_rate=sampling_rate,
            **kwargs,
        )
        self.removed_spans = []
        self.candidate_spans = []
        self.kept_spans = []
        self.error_count = 0
        entries = list(annotation.itertracks(yield_label=True))
        for index, (segment, _track, speaker) in enumerate(entries, start=1):
            start = round(float(segment.start), 3)
            end = round(float(segment.end), 3)
            self.candidate_spans.append(
                {
                    "span_id": f"dia-{index:06d}",
                    "parent_span_ids": [],
                    "stage": "diarization_raw",
                    "start": start,
                    "end": end,
                    "duration": round(max(0.0, end - start), 3),
                    "speaker": str(speaker),
                    "provider": "pyannote",
                    "decision": "keep",
                    "reason_codes": [],
                }
            )
        self.candidate_spans = annotate_span_diagnostics(
            self.candidate_spans
        )
        if not self.enabled:
            self.execution_state = "not_run"
            self.kept_spans = [
                {
                    **span,
                    "stage": "asr_input",
                    "parent_span_ids": [span["span_id"]],
                }
                for span in self.candidate_spans
            ]
            return annotation

        filtered = annotation.empty()
        total_samples = int(audio.shape[-1])
        for index, (segment, track, speaker) in enumerate(entries):
            candidate = self.candidate_spans[index]
            start_sample = max(
                0,
                min(total_samples, round(float(segment.start) * sampling_rate)),
            )
            end_sample = max(
                start_sample,
                min(total_samples, round(float(segment.end) * sampling_rate)),
            )
            try:
                contains_voice = self.detector(
                    audio[..., start_sample:end_sample],
                    sampling_rate,
                    self.trigger_level,
                )
            except (RuntimeError, TypeError, ValueError) as error:
                LOGGER.warning(
                    "noise filter could not inspect %.3f-%.3f; keeping span: %s",
                    float(segment.start),
                    float(segment.end),
                    error,
                )
                contains_voice = True
                self.error_count += 1
            if contains_voice:
                filtered[segment, track] = speaker
                kept = dict(candidate)
                kept.update(
                    {
                        "stage": "asr_input",
                        "parent_span_ids": [candidate["span_id"]],
                    }
                )
                self.kept_spans.append(kept)
                continue
            candidate["decision"] = "remove"
            candidate["reason_codes"] = ["SECONDARY_VAD_REJECTED"]
            self.removed_spans.append(
                {
                    "start": round(float(segment.start), 3),
                    "end": round(float(segment.end), 3),
                    "speaker": str(speaker),
                }
            )

        if self.error_count:
            self.execution_state = "error"
        elif self.removed_spans:
            self.execution_state = "run_removed"
        else:
            self.execution_state = "run_no_removal"

        if self.removed_spans:
            LOGGER.info(
                "noise filter removed %d of %d diarized speech spans",
                len(self.removed_spans),
                len(self.removed_spans) + sum(1 for _ in filtered.itertracks()),
            )
        return filtered

    def configure(
        self,
        *,
        enabled: bool,
        trigger_level: float,
    ) -> None:
        self.enabled = enabled
        self.trigger_level = trigger_level
        self.removed_spans = []
        self.candidate_spans = []
        self.kept_spans = []
        self.execution_state = "not_run"
        self.error_count = 0

    def span_id_for(self, speaker: str, start: float, end: float) -> str | None:
        """Resolve the stable diarization ID used by the latest invocation."""
        for span in self.candidate_spans:
            if (
                span["speaker"] == speaker
                and abs(float(span["start"]) - start) <= 0.001
                and abs(float(span["end"]) - end) <= 0.001
            ):
                return str(span["span_id"])
        return None

    def public_dict(self) -> dict[str, Any]:
        duration_sum, duration_union = interval_durations(self.removed_spans)
        return {
            "provider": "kotoba-noise-filter-v1",
            "execution_state": self.execution_state,
            "enabled": self.enabled,
            "trigger_level": self.trigger_level,
            "thresholds": {"trigger_level": self.trigger_level},
            "candidate_count": len(self.candidate_spans),
            "kept_count": len(self.kept_spans),
            "removed_count": len(self.removed_spans),
            "removed_duration_sum_sec": duration_sum,
            "removed_duration_union_sec": duration_union,
            "error_count": self.error_count,
            "removed_spans": list(self.removed_spans),
            "candidates": list(self.candidate_spans),
            "short_spans": short_span_diagnostics(self.candidate_spans),
        }


def install_corrected_kotoba_chunk_iter(speech_pipeline: Any) -> None:
    """Replace the remote iterator so 60-second inputs are not truncated."""
    module = importlib.import_module(type(speech_pipeline).__module__)
    if callable(getattr(module, "chunk_iter", None)):
        module.chunk_iter = corrected_kotoba_chunk_iter


def install_noise_filter(speech_pipeline: Any) -> None:
    """Wrap the remote diarizer without editing the model cache."""
    diarizer = getattr(speech_pipeline, "__dict__", {}).get(
        "model_speaker_diarization"
    )
    if not callable(diarizer) or isinstance(
        diarizer,
        NoiseFilteringSpeakerDiarization,
    ):
        return
    speech_pipeline.model_speaker_diarization = (
        NoiseFilteringSpeakerDiarization(diarizer)
    )


def _configure_noise_filter(
    speech_pipeline: Any,
    options: TranscriptionOptions,
) -> NoiseFilteringSpeakerDiarization | None:
    diarizer = getattr(speech_pipeline, "__dict__", {}).get(
        "model_speaker_diarization"
    )
    if not isinstance(diarizer, NoiseFilteringSpeakerDiarization):
        return None
    diarizer.configure(
        enabled=options.noise_filter,
        trigger_level=options.noise_filter_trigger_level,
    )
    return diarizer


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
        diarizer = getattr(speech_pipeline, "__dict__", {}).get(
            "model_speaker_diarization"
        )
        parent_span_id = (
            diarizer.span_id_for(speaker, span_start, span_end)
            if isinstance(diarizer, NoiseFilteringSpeakerDiarization)
            else None
        )
        for chunk in chunks:
            start, end = (float(value) for value in chunk["timestamp"])
            epsilon = 0.001
            if (
                start < span_start - epsilon
                or end > span_end + epsilon
                or end < start
            ):
                raise ValueError(
                    "Kotoba global timestamp escaped its parent speaker span"
                )
            chunk.update(
                {
                    "parent_span_ids": (
                        [parent_span_id] if parent_span_id is not None else []
                    ),
                    "local_start": round(start - span_start, 3),
                    "local_end": round(end - span_start, 3),
                    "global_start": round(start, 3),
                    "global_end": round(end, 3),
                    "stage": "asr_raw",
                    "provider": TIMESTAMP_POSTPROCESSOR,
                }
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
    for index, chunk in enumerate(output_chunks, start=1):
        chunk["span_id"] = f"asr-{index:06d}"
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
    validate_device(device)
    if diarization_device is not None:
        validate_device(diarization_device, setting="diarization_device")

    import torch
    from transformers import pipeline

    if threads is not None:
        torch.set_num_threads(threads)

    uses_accelerator = device == "mps" or device.startswith("cuda")
    torch_dtype = torch.float16 if uses_accelerator else torch.float32
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
    install_corrected_kotoba_chunk_iter(speech_pipeline)
    install_noise_filter(speech_pipeline)
    install_corrected_kotoba_postprocess(speech_pipeline)
    return speech_pipeline


def run_pipeline(
    speech_pipeline: SpeechPipeline,
    audio_path: Path,
    options: TranscriptionOptions,
    *,
    progress_callback: Callable[[ChunkProgress], None] | None = None,
    progress_every: int = 10,
    debug_artifact_dir: Path | None = None,
) -> Mapping[str, Any]:
    """Run one validated transcription against an already loaded pipeline."""
    options.validate()
    if progress_every not in {10, 100}:
        raise ValueError("progress_every must be either 10 or 100")
    noise_filter = _configure_noise_filter(speech_pipeline, options)
    if progress_callback is not None:
        result = _run_with_chunk_progress(
            speech_pipeline,
            audio_path,
            options,
            progress_callback,
            progress_every,
        )
    else:
        result = speech_pipeline(
            str(audio_path),
            chunk_length_s=options.chunk_length_seconds,
            add_punctuation=options.add_punctuation,
            num_speakers=options.num_speakers,
            min_speakers=options.min_speakers,
            max_speakers=options.max_speakers,
        )
    if noise_filter is None:
        noise_report: dict[str, Any] = {
            "provider": "kotoba-noise-filter-v1",
            "execution_state": "not_configured",
            "enabled": options.noise_filter,
            "candidate_count": None,
            "kept_count": None,
            "removed_count": None,
            "removed_duration_sum_sec": None,
            "removed_duration_union_sec": None,
            "removed_spans": [],
            "short_span_policy": options.short_span_policy,
        }
    else:
        noise_report = noise_filter.public_dict()
        noise_report["short_span_policy"] = options.short_span_policy
    enriched_result = dict(result)
    if noise_filter is not None:
        enriched_result["noise_filter"] = noise_report

    recorder = StageArtifactRecorder(debug_artifact_dir)
    unavailable = {
        "execution_state": "not_observable",
        "reason": (
            "the pinned remote Kotoba pipeline does not expose a callback for "
            "this intermediate stage"
        ),
    }
    candidates = noise_filter.candidate_spans if noise_filter is not None else []
    kept = noise_filter.kept_spans if noise_filter is not None else []
    recorder.record("01_diarization_raw.json", "diarization_raw", candidates)
    recorder.record(
        "02_speaker_spans_processed.json",
        "span_processed",
        unavailable,
    )
    recorder.record(
        "03_noise_filter_candidates.json",
        "noise_filter_candidates",
        candidates,
    )
    recorder.record(
        "04_noise_filter_result.json",
        "noise_filtered",
        noise_report,
    )
    recorder.record("05_asr_input_spans.json", "asr_input", kept)
    recorder.record(
        "06_asr_raw_segments.json",
        "asr_raw",
        enriched_result.get("chunks", []),
    )
    recorder.record(
        "07_final_segments.json",
        "subtitle_final",
        normalize_segments(enriched_result),
    )
    if noise_filter is None:
        return result
    enriched_result["encoding_warning"] = recorder.encoding_warning()
    return enriched_result


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
        item: dict[str, Any] = {
            "start": round(start, 3),
            "end": round(end, 3),
            "speaker": str(chunk.get("speaker_id", "UNKNOWN")),
            "text": text,
        }
        if "parent_span_ids" in chunk:
            item["parent_span_ids"] = list(chunk["parent_span_ids"])
        if "provider" in chunk:
            item["provider"] = str(chunk["provider"])
        normalized.append(item)

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
