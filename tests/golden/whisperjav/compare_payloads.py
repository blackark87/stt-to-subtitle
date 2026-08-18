#!/usr/bin/env python3
"""Compare two WhisperJAV worker payloads captured from the same audio.

Used to check the vendored in-process ensemble against the baseline recorded
before internalization. Timestamps are compared with a tolerance because a
different scene split can move a boundary by a frame; the text must match
exactly.

    python3 compare_payloads.py baseline.json candidate.json [--tolerance 0.05]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

SCHEMA_TOP_LEVEL = {
    "model",
    "timing",
    "runtime",
    "quality",
    "options",
    "words",
    "segments",
}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def compare(baseline: dict, candidate: dict, tolerance: float) -> list[str]:
    problems: list[str] = []

    missing = SCHEMA_TOP_LEVEL - set(candidate)
    if missing:
        problems.append(f"payload lost top-level keys: {sorted(missing)}")
    for key in ("id", "revision", "pass1", "pass2", "pass1_vad", "pass2_vad", "aligner"):
        if baseline["model"].get(key) != candidate["model"].get(key):
            problems.append(
                f"model.{key} changed: "
                f"{baseline['model'].get(key)} -> {candidate['model'].get(key)}"
            )

    old, new = baseline["segments"], candidate["segments"]
    if len(old) != len(new):
        problems.append(f"cue count changed: {len(old)} -> {len(new)}")

    for index, (a, b) in enumerate(zip(old, new)):
        if a["text"] != b["text"]:
            problems.append(
                f"cue {index} text changed:\n    - {a['text']!r}\n    + {b['text']!r}"
            )
        for edge in ("start", "end"):
            drift = abs(a[edge] - b[edge])
            if drift > tolerance:
                problems.append(
                    f"cue {index} {edge} moved {drift:.3f}s "
                    f"({a[edge]} -> {b[edge]})"
                )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--tolerance", type=float, default=0.05)
    args = parser.parse_args()

    baseline, candidate = load(args.baseline), load(args.candidate)
    problems = compare(baseline, candidate, args.tolerance)

    print(
        f"baseline: {len(baseline['segments'])} cues, "
        f"{len(baseline['words'])} words, "
        f"{baseline['runtime']['elapsed_seconds']}s"
    )
    print(
        f"candidate: {len(candidate['segments'])} cues, "
        f"{len(candidate['words'])} words, "
        f"{candidate['runtime']['elapsed_seconds']}s"
    )
    if not problems:
        print("IDENTICAL within tolerance")
        return 0
    print(f"\n{len(problems)} difference(s):")
    for problem in problems:
        print(f"  - {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
