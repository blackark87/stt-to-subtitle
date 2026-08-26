"""Stable, segment-aware comparison of translation generations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


TRANSLATION_CHANGE_LABELS = {
    "added": "추가",
    "removed": "삭제",
    "changed": "번역 변경",
    "unchanged": "동일",
}


def compare_translation_items(
    base_items: Sequence[Mapping[str, Any]],
    candidate_items: Sequence[Mapping[str, Any]],
    *,
    base_source_texts: Mapping[str, str] | None = None,
    candidate_source_texts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compare two generation item sets by their stable segment IDs."""
    base_by_id = _items_by_id(base_items, label="base")
    candidate_by_id = _items_by_id(candidate_items, label="candidate")
    base_sources = base_source_texts or {}
    candidate_sources = candidate_source_texts or {}
    segment_ids = sorted(
        base_by_id.keys() | candidate_by_id.keys(),
        key=lambda segment_id: _sort_key(
            segment_id,
            base_by_id,
            candidate_by_id,
        ),
    )

    rows = []
    counts = {key: 0 for key in TRANSLATION_CHANGE_LABELS}
    source_changed_count = 0
    for segment_id in segment_ids:
        base = base_by_id.get(segment_id)
        candidate = candidate_by_id.get(segment_id)
        if base is None:
            state = "added"
        elif candidate is None:
            state = "removed"
        elif str(base["text"]) != str(candidate["text"]):
            state = "changed"
        else:
            state = "unchanged"
        source_changed = bool(
            base is not None
            and candidate is not None
            and str(base.get("source_hash", ""))
            != str(candidate.get("source_hash", ""))
        )
        counts[state] += 1
        if source_changed:
            source_changed_count += 1
        base_source = str(base_sources.get(segment_id, ""))
        candidate_source = str(candidate_sources.get(segment_id, ""))
        rows.append(
            {
                "id": segment_id,
                "segment_index": int(
                    (candidate or base or {}).get("segment_index", 0)
                ),
                "state": state,
                "state_label": TRANSLATION_CHANGE_LABELS[state],
                "base_text": str(base["text"]) if base is not None else "",
                "candidate_text": (
                    str(candidate["text"]) if candidate is not None else ""
                ),
                "base_source_text": base_source,
                "candidate_source_text": candidate_source,
                "source_changed": source_changed,
                "has_change": state != "unchanged" or source_changed,
            }
        )

    return {
        "rows": rows,
        "total_count": len(rows),
        "change_count": sum(1 for row in rows if row["has_change"]),
        "source_changed_count": source_changed_count,
        **{f"{key}_count": value for key, value in counts.items()},
    }


def _items_by_id(
    items: Sequence[Mapping[str, Any]],
    *,
    label: str,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for fallback_index, item in enumerate(items):
        segment_id = str(item.get("id", "")).strip()
        text = str(item.get("text", "")).strip()
        if not segment_id or not text or segment_id in indexed:
            raise ValueError(f"invalid {label} translation items")
        try:
            segment_index = int(item.get("segment_index", fallback_index))
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid {label} translation items") from error
        if segment_index < 0:
            raise ValueError(f"invalid {label} translation items")
        indexed[segment_id] = {
            **dict(item),
            "id": segment_id,
            "text": text,
            "segment_index": segment_index,
        }
    return indexed


def _sort_key(
    segment_id: str,
    base_by_id: Mapping[str, Mapping[str, Any]],
    candidate_by_id: Mapping[str, Mapping[str, Any]],
) -> tuple[int, str]:
    candidate = candidate_by_id.get(segment_id)
    base = base_by_id.get(segment_id)
    return (
        int((candidate or base or {}).get("segment_index", 0)),
        segment_id,
    )
