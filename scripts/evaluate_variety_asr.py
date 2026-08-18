#!/usr/bin/env python3
"""Prepare and run a reproducible Japanese variety-ASR proxy evaluation.

The engine subcommands intentionally import heavyweight dependencies lazily so
the same script can run from the repository's Kotoba and WhisperX environments
as well as a small ReazonSpeech K2 evaluation environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import unicodedata
import wave
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


EMOTIONS = ("anger", "disgust", "fear", "happy", "sad", "surprise")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            rows.append(value)
    return rows


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def _load_jvnv_transcripts(path: Path) -> dict[str, str]:
    transcripts: dict[str, str] = {}
    with path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            parts = line.rstrip("\r\n").split("|")
            if len(parts) != 3:
                raise ValueError(
                    f"{path}:{line_number}: expected tag|reading|transcription"
                )
            tag, _, transcription = parts
            transcripts[tag] = transcription
    return transcripts


def prepare_jvnv(args: argparse.Namespace) -> None:
    source_dir = args.source_dir.resolve()
    transcript_path = source_dir / "transcription.csv"
    transcripts = _load_jvnv_transcripts(transcript_path)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for audio_path in source_dir.rglob("*.wav"):
        relative = audio_path.relative_to(source_dir)
        relative_lower = relative.as_posix().lower()
        emotion = next(
            (name for name in EMOTIONS if f"/{name}/" in f"/{relative_lower}/"),
            None,
        )
        if emotion is None or "clean" in relative_lower:
            continue
        stem_parts = audio_path.stem.split("_", maxsplit=1)
        if len(stem_parts) != 2 or stem_parts[1] not in transcripts:
            continue
        grouped[emotion].append(
            {
                "id": audio_path.stem,
                "audio": relative.as_posix(),
                "reference": transcripts[stem_parts[1]],
                "emotion": emotion,
                "duration_seconds": round(_wav_duration(audio_path), 6),
            }
        )

    selected: list[dict[str, Any]] = []
    for emotion in EMOTIONS:
        candidates = grouped.get(emotion, [])
        if len(candidates) < args.per_emotion:
            raise ValueError(
                f"{emotion} has {len(candidates)} clips, fewer than "
                f"--per-emotion={args.per_emotion}"
            )
        candidates.sort(
            key=lambda row: hashlib.sha256(
                f"{args.seed}:{row['audio']}".encode()
            ).hexdigest()
        )
        selected.extend(candidates[: args.per_emotion])

    selected.sort(key=lambda row: (str(row["emotion"]), str(row["id"])))
    _write_jsonl(args.manifest, selected)
    _write_json(
        args.manifest.with_suffix(args.manifest.suffix + ".meta.json"),
        {
            "dataset": "JVNV v1",
            "source_dir": str(source_dir),
            "seed": args.seed,
            "per_emotion": args.per_emotion,
            "clip_count": len(selected),
            "duration_seconds": round(
                sum(float(row["duration_seconds"]) for row in selected), 3
            ),
            "emotions": list(EMOTIONS),
        },
    )


def _run_engine(
    args: argparse.Namespace,
    *,
    engine: str,
    model_id: str,
    load: Callable[[], object],
    transcribe: Callable[[object, Path], str],
) -> None:
    manifest = _read_jsonl(args.manifest)
    load_started = time.monotonic()
    model = load()
    model_load_seconds = time.monotonic() - load_started
    rows: list[dict[str, Any]] = []
    inference_started = time.monotonic()
    for index, item in enumerate(manifest, start=1):
        audio_path = args.audio_root / str(item["audio"])
        item_started = time.monotonic()
        text = transcribe(model, audio_path).strip()
        rows.append(
            {
                "id": item["id"],
                "text": text,
                "elapsed_seconds": round(time.monotonic() - item_started, 6),
            }
        )
        print(
            f"[{engine}] {index}/{len(manifest)} {item['id']}",
            flush=True,
        )
    inference_seconds = time.monotonic() - inference_started
    _write_jsonl(args.output, rows)
    _write_json(
        args.output.with_suffix(args.output.suffix + ".meta.json"),
        {
            "engine": engine,
            "model": model_id,
            "clip_count": len(rows),
            "audio_seconds": round(
                sum(float(item["duration_seconds"]) for item in manifest), 3
            ),
            "model_load_seconds": round(model_load_seconds, 3),
            "inference_seconds": round(inference_seconds, 3),
            "real_time_factor": round(
                inference_seconds
                / sum(float(item["duration_seconds"]) for item in manifest),
                6,
            ),
        },
    )


def run_whisperx(args: argparse.Namespace) -> None:
    import whisperx

    def load() -> object:
        return whisperx.load_model(
            args.model,
            "cuda",
            device_index=args.device_index,
            compute_type=args.compute_type,
            language="ja",
            download_root=str(args.model_cache),
            threads=args.threads,
        )

    def transcribe(model: object, audio_path: Path) -> str:
        audio = whisperx.load_audio(str(audio_path))
        result = model.transcribe(
            audio,
            batch_size=args.batch_size,
            language="ja",
            chunk_size=args.chunk_seconds,
        )
        return "".join(str(segment.get("text", "")) for segment in result["segments"])

    _run_engine(
        args,
        engine="whisperx",
        model_id=args.model,
        load=load,
        transcribe=transcribe,
    )


def run_kotoba(args: argparse.Namespace) -> None:
    from stt_to_subtitle.kotoba import (
        MODEL_ID,
        MODEL_REVISION,
        TranscriptionOptions,
        load_pipeline,
        normalize_segments,
        run_pipeline,
    )

    token = os.environ.get("HF_TOKEN", "")

    def load() -> object:
        return load_pipeline(
            token,
            batch_size=args.batch_size,
            device=f"cuda:{args.device_index}",
            diarization_device=f"cuda:{args.device_index}",
            threads=args.threads,
        )

    options = TranscriptionOptions(
        chunk_length_seconds=args.chunk_seconds,
        num_speakers=1,
        noise_filter=True,
    )

    def transcribe(model: object, audio_path: Path) -> str:
        result = run_pipeline(model, audio_path, options)
        return "".join(
            str(segment.get("text", ""))
            for segment in normalize_segments(result)
        )

    _run_engine(
        args,
        engine="kotoba",
        model_id=f"{MODEL_ID}@{MODEL_REVISION}",
        load=load,
        transcribe=transcribe,
    )


def run_reazon_k2(args: argparse.Namespace) -> None:
    import sherpa_onnx

    from reazonspeech.k2.asr import audio_from_path, load_model, transcribe

    sherpa_version = str(sherpa_onnx.__version__)
    if "+cuda" not in sherpa_version:
        raise RuntimeError(
            "ReazonSpeech evaluation requires a sherpa-onnx CUDA wheel; "
            f"found {sherpa_version!r}"
        )

    def load() -> object:
        return load_model(
            device="cuda",
            precision=args.precision,
            language="ja",
        )

    def recognize(model: object, audio_path: Path) -> str:
        result = transcribe(model, audio_from_path(str(audio_path)))
        return str(result.text)

    _run_engine(
        args,
        engine="reazonspeech-k2",
        model_id=(
            "reazon-research/reazonspeech-k2-v2:"
            f"{args.precision}:cuda:sherpa-{sherpa_version}"
        ),
        load=load,
        transcribe=recognize,
    )


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).lower()
    return "".join(
        character
        for character in normalized
        if unicodedata.category(character)[0] not in {"C", "P", "S", "Z"}
    )


def _edit_distance(reference: str, hypothesis: str) -> int:
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for row, reference_character in enumerate(reference, start=1):
        current = [row]
        for column, hypothesis_character in enumerate(hypothesis, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1]
                    + (reference_character != hypothesis_character),
                )
            )
        previous = current
    return previous[-1]


def _has_repetition_burst(text: str) -> bool:
    normalized = _normalize_text(text)
    return any(
        re.search(rf"(.{{{width}}})\1{{3,}}", normalized)
        for width in range(1, 9)
    )


def _surface_pair(reference: str, hypothesis: str) -> tuple[str, str]:
    return _normalize_text(reference), _normalize_text(hypothesis)


def _load_fugashi_pair_normalizer() -> Callable[[str, str], tuple[str, str]]:
    try:
        from pysctkja.charnorm import CharNormalizerV1
        from pysctkja.wordnorm import FugashiLemmaTaggerV1, adjust_to_reference
    except ModuleNotFoundError as error:
        raise RuntimeError(
            "--fugashi-normalization requires asr-ja_evalkit and its UniDic "
            "dictionary"
        ) from error

    character_normalizer = CharNormalizerV1()
    lemma_tagger = FugashiLemmaTaggerV1()

    def normalize_pair(reference: str, hypothesis: str) -> tuple[str, str]:
        adjusted_reference, adjusted_hypothesis, _, _ = adjust_to_reference(
            lemma_tagger,
            character_normalizer(reference),
            character_normalizer(hypothesis),
        )
        return _normalize_text(adjusted_reference), _normalize_text(
            adjusted_hypothesis
        )

    return normalize_pair


def _score_pairs(
    references: Mapping[str, Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    normalize_pair: Callable[[str, str], tuple[str, str]],
) -> dict[str, Any]:
    reference_characters = 0
    hypothesis_characters = 0
    edits = 0
    by_emotion: dict[str, dict[str, int]] = defaultdict(
        lambda: {"reference_characters": 0, "edits": 0, "clips": 0}
    )
    for item_id, reference_row in references.items():
        reference, hypothesis = normalize_pair(
            str(reference_row["reference"]),
            str(results[item_id].get("text", "")),
        )
        item_edits = _edit_distance(reference, hypothesis)
        reference_characters += len(reference)
        hypothesis_characters += len(hypothesis)
        edits += item_edits
        emotion = str(reference_row["emotion"])
        by_emotion[emotion]["reference_characters"] += len(reference)
        by_emotion[emotion]["edits"] += item_edits
        by_emotion[emotion]["clips"] += 1
    return {
        "normalized_cer": round(edits / reference_characters, 6),
        "reference_characters": reference_characters,
        "hypothesis_characters": hypothesis_characters,
        "length_ratio": round(hypothesis_characters / reference_characters, 6),
        "by_emotion": {
            emotion: {
                **values,
                "normalized_cer": round(
                    values["edits"] / values["reference_characters"], 6
                ),
            }
            for emotion, values in sorted(by_emotion.items())
        },
    }


def score(args: argparse.Namespace) -> None:
    manifest = _read_jsonl(args.manifest)
    references = {str(row["id"]): row for row in manifest}
    fugashi_normalizer = (
        _load_fugashi_pair_normalizer() if args.fugashi_normalization else None
    )
    summaries: dict[str, Any] = {}
    for specification in args.result:
        name, separator, raw_path = specification.partition("=")
        if not separator or not name or not raw_path:
            raise ValueError("--result must have the form NAME=PATH")
        results = {str(row["id"]): row for row in _read_jsonl(Path(raw_path))}
        missing = sorted(set(references) - set(results))
        extra = sorted(set(results) - set(references))
        if missing or extra:
            raise ValueError(f"{name}: missing={missing}, extra={extra}")
        summaries[name] = {
            "clip_count": len(references),
            "surface": _score_pairs(references, results, _surface_pair),
            "empty_count": sum(
                not _normalize_text(str(row.get("text", "")))
                for row in results.values()
            ),
            "repetition_burst_count": sum(
                _has_repetition_burst(str(row.get("text", "")))
                for row in results.values()
            ),
        }
        if fugashi_normalizer is not None:
            summaries[name]["fugashi"] = _score_pairs(
                references, results, fugashi_normalizer
            )
        metadata_path = Path(raw_path).with_suffix(
            Path(raw_path).suffix + ".meta.json"
        )
        if metadata_path.is_file():
            summaries[name]["runtime"] = json.loads(
                metadata_path.read_text(encoding="utf-8")
            )
    _write_json(
        args.output,
        {
            "normalization": {
                "surface": "NFKC-strip-CPSZ",
                "fugashi": "asr-ja_evalkit CharNormalizerV1 + UniDic lemma",
            },
            "engines": summaries,
        },
    )


def _engine_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare-jvnv")
    prepare.add_argument("--source-dir", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--per-emotion", type=int, default=20)
    prepare.add_argument("--seed", default="stt-to-subtitle-variety-v1")
    prepare.set_defaults(function=prepare_jvnv)

    whisperx = subparsers.add_parser("run-whisperx")
    _engine_arguments(whisperx)
    whisperx.add_argument("--model", default="large-v3")
    whisperx.add_argument("--model-cache", type=Path, required=True)
    whisperx.add_argument("--compute-type", default="float16")
    whisperx.add_argument("--batch-size", type=int, default=8)
    whisperx.add_argument("--chunk-seconds", type=int, default=30)
    whisperx.add_argument("--threads", type=int, default=8)
    whisperx.add_argument("--device-index", type=int, default=0)
    whisperx.set_defaults(function=run_whisperx)

    kotoba = subparsers.add_parser("run-kotoba")
    _engine_arguments(kotoba)
    kotoba.add_argument("--batch-size", type=int, default=8)
    kotoba.add_argument("--chunk-seconds", type=int, default=15)
    kotoba.add_argument("--threads", type=int, default=8)
    kotoba.add_argument("--device-index", type=int, default=0)
    kotoba.set_defaults(function=run_kotoba)

    reazon = subparsers.add_parser("run-reazon-k2")
    _engine_arguments(reazon)
    reazon.add_argument(
        "--precision", choices=("fp32", "int8", "int8-fp32"), default="fp32"
    )
    reazon.set_defaults(function=run_reazon_k2)

    scorer = subparsers.add_parser("score")
    scorer.add_argument("--manifest", type=Path, required=True)
    scorer.add_argument("--result", action="append", required=True)
    scorer.add_argument("--output", type=Path, required=True)
    scorer.add_argument("--fugashi-normalization", action="store_true")
    scorer.set_defaults(function=score)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
