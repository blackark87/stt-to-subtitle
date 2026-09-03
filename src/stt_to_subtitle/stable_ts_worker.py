"""Regroup aligned ASR words with the pinned Japanese stable-ts policy."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .files import write_json_atomic
from .vendor.whisperjav.runner import Cue


REGROUP_PROVIDER = "stable-ts-regroup-jav-v1"


def regroup_aligned_words(
    words: Sequence[Mapping[str, Any]],
    audio_path: Path,
    *,
    parent_prefix: str = "stable-ts-cue",
) -> tuple[list[dict[str, Any]], list[Cue]]:
    """Replace only parent cue boundaries, retaining every source word."""
    from .vendor.whisperjav.modules.subtitle_pipeline.reconstruction import (
        REGROUP_JAV,
        reconstruct_from_words,
    )

    prepared = [
        {
            "word": str(word.get("word", "")),
            "start": float(word.get("start", 0.0)),
            "end": float(word.get("end", word.get("start", 0.0))),
        }
        for word in words
    ]
    # stable-ts 2.19 crashes in ``sd`` when an aligner-free fallback emits one
    # indivisible word longer than the duration cap: the segment needs a split
    # but has no word boundary on which to split.  Keep all other JAV regroup
    # rules and let reconstruction's wall-clock pass split only segments that
    # actually have two or more words.
    regroup = REGROUP_JAV
    if any(item["end"] - item["start"] > 8.0 for item in prepared):
        regroup = regroup.replace("_sd=8", "")
    result = reconstruct_from_words(
        prepared,
        audio_path,
        suppress_silence=False,
        regroup=regroup,
    )
    grouped_words: list[dict[str, Any]] = []
    cues: list[Cue] = []
    source_index = 0
    for segment_index, segment in enumerate(result.segments, start=1):
        stable_words = list(segment.words or [])
        if not stable_words:
            continue
        next_index = source_index + len(stable_words)
        source_words = words[source_index:next_index]
        if len(source_words) != len(stable_words):
            raise RuntimeError(
                "stable-ts regroup returned an unexpected word count"
            )
        parent_id = f"{parent_prefix}-{segment_index:06d}"
        for source_word in source_words:
            grouped_words.append(
                {
                    **dict(source_word),
                    "parent_span_ids": [parent_id],
                    "regroup_provider": REGROUP_PROVIDER,
                }
            )
        cues.append(
            Cue(
                start=float(segment.start),
                end=float(segment.end),
                text=str(segment.text).strip(),
            )
        )
        source_index = next_index
    if source_index != len(words):
        raise RuntimeError("stable-ts regroup did not retain every aligned word")
    return grouped_words, cues


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Regroup aligned words with stable-ts"
    )
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("stable-ts input must be a JSON object")
    words = payload.get("words")
    if not isinstance(words, list):
        raise ValueError("stable-ts input must contain a words list")
    grouped_words, cues = regroup_aligned_words(words, args.audio)
    write_json_atomic(
        args.output,
        {
            "provider": REGROUP_PROVIDER,
            "words": grouped_words,
            "segments": [
                {
                    "start": round(cue.start, 3),
                    "end": round(cue.end, 3),
                    "text": cue.text,
                }
                for cue in cues
            ],
        },
    )


if __name__ == "__main__":
    main()
