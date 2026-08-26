"""Compatibility helpers for persisted paths when storage roots move."""

from __future__ import annotations

from pathlib import Path


def rebase_stored_path(
    value: str | None,
    *,
    previous_root: Path,
    current_root: Path,
) -> str | None:
    """Move a stored path between roots without touching unrelated paths."""
    if value is None or previous_root == current_root:
        return value
    try:
        relative = Path(value).relative_to(previous_root)
    except ValueError:
        return value
    return str(current_root / relative)
