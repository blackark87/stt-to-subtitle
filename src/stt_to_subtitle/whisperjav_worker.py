"""Run the pinned WhisperJAV domain ensemble in an isolated environment."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import time
from typing import Any, Mapping, Sequence
import wave

from .files import write_json_atomic

WHISPERJAV_RECIPE = "whisperjav-domain-ensemble-v1"
WHISPERJAV_COMMIT = "a69a43244e14612ebd3a1eb417bdd0de6d494d0f"
ANIME_MODEL_ID = "litagin/anime-whisper"
ANIME_MODEL_REVISION = "22e2008a8182b357da3922a6308d095008f72973"
QWEN_MODEL_ID = "jaykwok/Qwen3-ASR-1.7B-JA-Anime-Galgame"
QWEN_MODEL_REVISION = "6db0efecd56d4a7e0003190a6bd4cac056d0f390"
ALIGNER_MODEL_ID = "Qwen/Qwen3-ForcedAligner-0.6B"
ALIGNER_MODEL_REVISION = "c7cbfc2048c462b0d63a45797104fc9db3ad62b7"
WHISPERSEG_MODEL_REVISION = "6ac29e2cbf2f4f8e9b639861766a8639dd666e9c"
TEN_VAD_VERSION = "1.0.6.8"
DEFAULT_ANIME_MAX_GROUP_SECONDS = 2.0
DEFAULT_QWEN_MAX_GROUP_SECONDS = 3.0
MIN_MAX_GROUP_SECONDS = 0.5
MAX_MAX_GROUP_SECONDS = 30.0


@dataclass(frozen=True)
class WhisperJAVOptions:
    """Validated controls exposed by the stable WhisperJAV recipe."""

    recipe: str = WHISPERJAV_RECIPE
    anime_max_group_duration_seconds: float = (
        DEFAULT_ANIME_MAX_GROUP_SECONDS
    )
    qwen_max_group_duration_seconds: float = DEFAULT_QWEN_MAX_GROUP_SECONDS

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> WhisperJAVOptions:
        raw = options.get("whisperjav", {})
        if not isinstance(raw, Mapping):
            raise ValueError("whisperjav must be a JSON object")
        allowed = {
            "recipe",
            "anime_max_group_duration_seconds",
            "qwen_max_group_duration_seconds",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(
                f"unsupported whisperjav options: {sorted(unknown)}"
            )
        recipe = str(raw.get("recipe", WHISPERJAV_RECIPE)).strip()
        if recipe != WHISPERJAV_RECIPE:
            raise ValueError(
                f"whisperjav recipe must be '{WHISPERJAV_RECIPE}'"
            )

        def group_seconds(name: str, default: float) -> float:
            try:
                value = float(raw.get(name, default))
            except (TypeError, ValueError) as error:
                raise ValueError(f"{name} must be a number") from error
            if not MIN_MAX_GROUP_SECONDS <= value <= MAX_MAX_GROUP_SECONDS:
                raise ValueError(
                    f"{name} must be between {MIN_MAX_GROUP_SECONDS} and "
                    f"{MAX_MAX_GROUP_SECONDS}"
                )
            return value

        return cls(
            recipe=recipe,
            anime_max_group_duration_seconds=group_seconds(
                "anime_max_group_duration_seconds",
                DEFAULT_ANIME_MAX_GROUP_SECONDS,
            ),
            qwen_max_group_duration_seconds=group_seconds(
                "qwen_max_group_duration_seconds",
                DEFAULT_QWEN_MAX_GROUP_SECONDS,
            ),
        )


@dataclass(frozen=True)
class SubtitleCue:
    start: float
    end: float
    text: str


_TIMESTAMP_RE = re.compile(
    r"(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2})[,.](?P<sms>\d{3})"
    r"\s*-->\s*"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2})[,.](?P<ems>\d{3})"
)


def _seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 1000
    )


def parse_srt(content: str) -> list[SubtitleCue]:
    """Parse UTF-8 SRT text without depending on WhisperJAV internals."""
    cues: list[SubtitleCue] = []
    for block in re.split(r"\r?\n\s*\r?\n", content.strip()):
        lines = [line.rstrip() for line in block.splitlines()]
        timestamp_index = next(
            (index for index, line in enumerate(lines) if "-->" in line),
            None,
        )
        if timestamp_index is None:
            continue
        match = _TIMESTAMP_RE.fullmatch(lines[timestamp_index].strip())
        if match is None:
            continue
        values = match.groupdict()
        start = _seconds(
            values["sh"], values["sm"], values["ss"], values["sms"]
        )
        end = _seconds(
            values["eh"], values["em"], values["es"], values["ems"]
        )
        text = "\n".join(lines[timestamp_index + 1 :]).strip()
        if text and end >= start:
            cues.append(SubtitleCue(start=start, end=end, text=text))
    return sorted(cues, key=lambda cue: (cue.start, cue.end, cue.text))


def build_whisperjav_command(
    *,
    python: Path,
    audio_path: Path,
    output_dir: Path,
    temp_dir: Path,
    anime_model_path: Path,
    qwen_model_path: Path,
    options: WhisperJAVOptions,
) -> list[str]:
    """Build the fixed, auditable two-pass domain recipe."""
    pass1_params = {
        "generator_backend": "anime-whisper",
        "model_id": str(anime_model_path),
        "max_group_duration": options.anime_max_group_duration_seconds,
        "timestamp_mode": "vad_only",
        "use_aligner": False,
        "context": "",
    }
    pass2_params = {
        "generator_backend": "qwen3",
        "model_id": str(qwen_model_path),
        "max_group_duration": options.qwen_max_group_duration_seconds,
        "timestamp_mode": "vad_only",
        "use_aligner": False,
        "context": "",
    }
    return [
        str(python),
        "-m",
        "whisperjav.main",
        str(audio_path),
        "--ensemble",
        "--ensemble-serial",
        "--pass1-pipeline",
        "qwen",
        "--pass1-sensitivity",
        "aggressive",
        "--pass1-scene-detector",
        "semantic",
        "--pass1-speech-segmenter",
        "whisperseg",
        "--pass1-qwen-params",
        json.dumps(pass1_params, sort_keys=True),
        "--pass2-pipeline",
        "qwen",
        "--pass2-sensitivity",
        "balanced",
        "--pass2-scene-detector",
        "semantic",
        "--pass2-speech-segmenter",
        "ten",
        "--pass2-qwen-params",
        json.dumps(pass2_params, sort_keys=True),
        "--merge-strategy",
        "pass1_primary",
        "--language",
        "japanese",
        "--output-dir",
        str(output_dir),
        "--temp-dir",
        str(temp_dir),
        "--no-progress",
        "--log-level",
        "INFO",
    ]


def _snapshot(repo_id: str, revision: str) -> Path:
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(repo_id=repo_id, revision=revision))


def _ensemble_result(temp_dir: Path) -> tuple[Path, dict[str, Any]]:
    summaries = sorted(temp_dir.glob("ensemble_summary_*.json"))
    if not summaries:
        raise RuntimeError("WhisperJAV did not write an ensemble summary")
    try:
        summary = json.loads(summaries[-1].read_text(encoding="utf-8"))
        file_result = summary["files"][0]
        final_output = Path(file_result["final_output"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("WhisperJAV ensemble summary is invalid") from error
    if not final_output.is_file():
        raise RuntimeError("WhisperJAV final SRT is unavailable")
    if str(file_result.get("status", "")) == "failed":
        raise RuntimeError("WhisperJAV primary pass failed")
    return final_output, dict(file_result)


def _read_pcm16(audio_path: Path) -> tuple[Any, int]:
    import numpy as np

    with wave.open(str(audio_path), "rb") as source:
        channels = source.getnchannels()
        sample_rate = source.getframerate()
        sample_width = source.getsampwidth()
        frames = source.readframes(source.getnframes())
    if channels != 1 or sample_rate != 16000 or sample_width != 2:
        raise ValueError("WhisperJAV input must be mono 16 kHz PCM16 WAV")
    return np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0, sample_rate


def align_cues(
    audio_path: Path,
    cues: Sequence[SubtitleCue],
    *,
    aligner_path: Path,
) -> tuple[list[dict[str, Any]], int]:
    """Align final merged cues once and retain a bounded cue fallback."""
    from whisperjav.modules.subtitle_pipeline.aligners.qwen3 import (
        Qwen3ForcedAlignerAdapter,
    )

    audio, sample_rate = _read_pcm16(audio_path)
    slices = [
        audio[
            max(0, round(cue.start * sample_rate)) :
            max(0, round(cue.end * sample_rate))
        ].copy()
        for cue in cues
    ]
    aligner = Qwen3ForcedAlignerAdapter(
        aligner_id=str(aligner_path),
        device=os.environ.get("STT_DEVICE", "cuda"),
        dtype="auto",
        language="Japanese",
    )
    aligner.load()
    try:
        results = aligner.align_batch(
            audio_paths=slices,
            texts=[cue.text for cue in cues],
            language="ja",
            audio_durations=[cue.end - cue.start for cue in cues],
        )
    finally:
        aligner.unload()

    words: list[dict[str, Any]] = []
    fallback_count = 0
    for cue_index, (cue, result) in enumerate(zip(cues, results), start=1):
        parent_id = f"whisperjav-cue-{cue_index:06d}"
        aligned = list(result.words)
        if not aligned:
            fallback_count += 1
            words.append(
                {
                    "word_id": f"whisperjav-word-{len(words) + 1:06d}",
                    "word": cue.text,
                    "start": round(cue.start, 3),
                    "end": round(cue.end, 3),
                    "speaker": "UNKNOWN",
                    "timestamp_source": "cue_fallback",
                    "timestamp_fallback": True,
                    "parent_span_ids": [parent_id],
                    "provider": "qwen3-forced-aligner",
                }
            )
            continue
        for word in aligned:
            start = min(cue.end, max(cue.start, cue.start + word.start))
            end = min(cue.end, max(start, cue.start + word.end))
            words.append(
                {
                    "word_id": f"whisperjav-word-{len(words) + 1:06d}",
                    "word": word.word,
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "speaker": "UNKNOWN",
                    "timestamp_source": "qwen3_forced_alignment",
                    "timestamp_fallback": False,
                    "parent_span_ids": [parent_id],
                    "provider": "qwen3-forced-aligner",
                }
            )
    return words, fallback_count


def run_whisperjav(
    audio_path: Path,
    options: Mapping[str, Any],
    *,
    debug_artifact_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute both ASR passes, merge, and final forced alignment."""
    started = time.monotonic()
    validated = WhisperJAVOptions.from_options(options)
    anime_path = _snapshot(ANIME_MODEL_ID, ANIME_MODEL_REVISION)
    qwen_path = _snapshot(QWEN_MODEL_ID, QWEN_MODEL_REVISION)
    aligner_path = _snapshot(ALIGNER_MODEL_ID, ALIGNER_MODEL_REVISION)

    with TemporaryDirectory(prefix="stt-whisperjav-") as directory:
        work = Path(directory)
        output_dir = work / "output"
        temp_dir = work / "temp"
        output_dir.mkdir()
        temp_dir.mkdir()
        command = build_whisperjav_command(
            python=Path(os.sys.executable),
            audio_path=audio_path,
            output_dir=output_dir,
            temp_dir=temp_dir,
            anime_model_path=anime_path,
            qwen_model_path=qwen_path,
            options=validated,
        )
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=os.environ.copy(),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(
                "WhisperJAV ensemble failed"
                + (f": {detail[-4000:]}" if detail else "")
            )
        final_srt, ensemble = _ensemble_result(temp_dir)
        cues = parse_srt(final_srt.read_text(encoding="utf-8-sig"))
        words, fallback_count = align_cues(
            audio_path,
            cues,
            aligner_path=aligner_path,
        )
        if debug_artifact_dir is not None:
            debug_artifact_dir.mkdir(parents=True, exist_ok=True)
            (debug_artifact_dir / "merged.srt").write_text(
                final_srt.read_text(encoding="utf-8-sig"),
                encoding="utf-8",
            )
            write_json_atomic(
                debug_artifact_dir / "ensemble.json",
                ensemble,
            )
            write_json_atomic(
                debug_artifact_dir / "aligned_words.json",
                {"words": words, "fallback_count": fallback_count},
            )

    return {
        "model": {
            "id": "whisperjav-domain-ensemble",
            "revision": WHISPERJAV_COMMIT,
            "pass1": {"id": ANIME_MODEL_ID, "revision": ANIME_MODEL_REVISION},
            "pass2": {"id": QWEN_MODEL_ID, "revision": QWEN_MODEL_REVISION},
            "pass1_vad": {
                "id": "TransWithAI/Whisper-Vad-EncDec-ASMR-onnx",
                "revision": WHISPERSEG_MODEL_REVISION,
            },
            "pass2_vad": {"id": "ten-vad", "version": TEN_VAD_VERSION},
            "aligner": {
                "id": ALIGNER_MODEL_ID,
                "revision": ALIGNER_MODEL_REVISION,
            },
        },
        "timing": {"postprocessor": "qwen3-forced-alignment"},
        "runtime": {
            "backend": "whisperjav",
            "device": os.environ.get("STT_DEVICE", "cuda"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "asr_pass_count": 2,
            "alignment_pass_count": 1,
            "simultaneous_model_residency": False,
        },
        "quality": {
            "ensemble_status": ensemble.get("status", "completed"),
            "pass1": ensemble.get("pass1", {}),
            "pass2": ensemble.get("pass2", {}),
            "merge": ensemble.get("merge", {}),
            "alignment_fallback_count": fallback_count,
        },
        "options": {"whisperjav": asdict(validated)},
        "words": words,
        "segments": [
            {
                "start": round(cue.start, 3),
                "end": round(cue.end, 3),
                "speaker": "UNKNOWN",
                "text": cue.text,
            }
            for cue in cues
        ],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run one isolated WhisperJAV domain ensemble"
    )
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--options", required=True)
    parser.add_argument("--debug-dir", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    options = json.loads(args.options)
    if not isinstance(options, Mapping):
        raise ValueError("WhisperJAV options must be a JSON object")
    write_json_atomic(
        args.output,
        run_whisperjav(
            args.audio,
            options,
            debug_artifact_dir=args.debug_dir,
        ),
    )


if __name__ == "__main__":
    main()
