"""Summarize media completion by actor for the dashboard."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


ATTENTION_STATES = frozenset({"paused", "blocked", "stopped", "failed"})


def summarize_library_progress(
    entries: Sequence[Mapping[str, Any]],
    latest_jobs: Mapping[str, Any],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    summaries = []
    for entry in entries:
        media_items = list(entry.get("media", ()))
        if not media_items:
            continue
        counts = {
            "done": 0,
            "running": 0,
            "waiting": 0,
            "attention": 0,
            "unprocessed": 0,
        }
        for media in media_items:
            source = str(media.get("path", ""))
            job = latest_jobs.get(source)
            state = str(getattr(job, "state", "")) if job is not None else ""
            status = str(getattr(job, "status", "")) if job is not None else ""
            if bool(media.get("has_subtitle")) or status == "completed":
                counts["done"] += 1
            elif state in ATTENTION_STATES:
                counts["attention"] += 1
            elif state == "running":
                counts["running"] += 1
            elif state == "waiting":
                counts["waiting"] += 1
            else:
                counts["unprocessed"] += 1
        total = len(media_items)
        active = counts["running"] + counts["waiting"] + counts["attention"]
        summaries.append(
            {
                "name": str(entry.get("name", "")),
                "path": str(entry.get("path", "")),
                "image_path": entry.get("image_path"),
                "done": counts["done"],
                "total": total,
                "remaining": total - counts["done"],
                "active": active,
                "attention": counts["attention"],
                "segments": [
                    {
                        "state": state,
                        "percent": round(counts[state] * 100 / total, 1),
                    }
                    for state in (
                        "done",
                        "running",
                        "waiting",
                        "attention",
                        "unprocessed",
                    )
                    if counts[state]
                ],
            }
        )
    summaries.sort(
        key=lambda item: (
            -int(bool(item["active"])),
            -int(item["attention"]),
            -int(item["active"]),
            -int(item["remaining"]),
            str(item["name"]).casefold(),
        )
    )
    return summaries[:limit]
