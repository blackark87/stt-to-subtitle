"""Kotoba-Whisper loading and result normalization."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

MODEL_ID = "kotoba-tech/kotoba-whisper-v2.2"
MODEL_REVISION = "9d33482a0eb9b57f1ad80708e8ac5538246d8355"


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


def transcribe(
    audio_path: Path,
    token: str,
    options: TranscriptionOptions,
) -> Mapping[str, Any]:
    """Load the pinned Kotoba pipeline and transcribe one WAV file on CPU."""
    options.validate()
    if not token.strip():
        raise ValueError("HF_TOKEN is required for gated Pyannote models")

    import torch
    from transformers import pipeline

    if options.threads is not None:
        torch.set_num_threads(options.threads)

    speech_pipeline = pipeline(
        model=MODEL_ID,
        revision=MODEL_REVISION,
        token=token,
        torch_dtype=torch.float32,
        device="cpu",
        batch_size=options.batch_size,
        trust_remote_code=True,
    )
    return speech_pipeline(
        str(audio_path),
        chunk_length_s=options.chunk_length_seconds,
        add_punctuation=options.add_punctuation,
        num_speakers=options.num_speakers,
        min_speakers=options.min_speakers,
        max_speakers=options.max_speakers,
    )


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
