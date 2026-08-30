"""Display-only media listing transformations for the web API."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .path_display import shorten_display_path


FILE_SORTS = {
    "filename",
    "created_desc",
    "modified_desc",
    "nfo_title",
    "nfo_release_desc",
}


def _timestamp(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0


def _nfo_release_value(value: object) -> int:
    """Return a comparable YYYYMMDD value from common NFO date formats."""
    parts = [int(part) for part in re.findall(r"\d+", str(value))]
    if not parts:
        return 0
    year = parts[0]
    month = parts[1] if len(parts) > 1 else 1
    day = parts[2] if len(parts) > 2 else 1
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return 0
    return year * 10_000 + month * 100 + day


def _sort_media_files(files: list[dict[str, object]], file_sort: str) -> None:
    if file_sort == "created_desc":
        files.sort(
            key=lambda media: (
                -_timestamp(media.get("created_at")),
                str(media.get("name", "")).casefold(),
            )
        )
    elif file_sort == "modified_desc":
        files.sort(
            key=lambda media: (
                -_timestamp(media.get("modified_at")),
                str(media.get("name", "")).casefold(),
            )
        )
    elif file_sort == "nfo_title":
        files.sort(
            key=lambda media: (
                not bool(media.get("nfo_title")),
                str(media.get("nfo_title") or "").casefold(),
                str(media.get("name", "")).casefold(),
            )
        )
    elif file_sort == "nfo_release_desc":
        files.sort(
            key=lambda media: (
                not bool(media.get("nfo_release_date")),
                -_nfo_release_value(media.get("nfo_release_date")),
                str(media.get("name", "")).casefold(),
            )
        )
    else:
        files.sort(
            key=lambda media: (
                str(media.get("name", "")).casefold(),
                str(media.get("path", "")).casefold(),
            )
        )


def flatten_media_display_folders(
    library: Any,
    browser: Mapping[str, object],
    rules: Sequence[object],
) -> dict[str, object]:
    """Lift files from a directory removed by a display-path rule."""
    if not rules or not browser.get("folders"):
        return dict(browser)
    current_folder = str(browser.get("current_folder", ""))
    files = list(browser.get("files", ()))
    folders: list[dict[str, object]] = []
    for folder in browser.get("folders", ()):
        if not isinstance(folder, Mapping):
            continue
        child = library.browse(str(folder["path"]))
        child_files = list(child["files"])
        lifted_files = []
        for media in child_files:
            source_path = str(media["path"])
            display_path = shorten_display_path(source_path, rules)
            display_parent = Path(display_path).parent
            display_parent_text = (
                "" if display_parent == Path(".") else display_parent.as_posix()
            )
            if (
                display_path != source_path
                and display_parent_text.casefold() == current_folder.casefold()
            ):
                lifted_files.append(media)
        can_lift_folder = (
            bool(child_files)
            and len(lifted_files) == len(child_files)
            and not child["folders"]
            and len(files) + len(lifted_files) <= library.maximum_files
        )
        if can_lift_folder:
            files.extend(lifted_files)
        else:
            folders.append(dict(folder))
    files.sort(key=lambda media: str(media["path"]).casefold())
    return {**browser, "folders": folders, "files": files}


def decorate_media_listing(
    library: Any,
    browser: Mapping[str, object],
    rules: Sequence[object],
    *,
    folder_sort: str = "name",
    folder_offset: int = 0,
    folder_limit: int | None = None,
    file_sort: str = "filename",
) -> dict[str, object]:
    """Add safe display paths and optional actor images without changing IDs."""
    raw_folders = [
        dict(folder)
        for folder in browser.get("folders", ())
        if isinstance(folder, Mapping)
    ]
    if folder_sort not in {"name", "modified_desc", "modified_asc"}:
        raise ValueError("unsupported media folder sort")
    if folder_offset < 0:
        raise ValueError("media folder offset must not be negative")
    if file_sort not in FILE_SORTS:
        raise ValueError("unsupported media file sort")
    if folder_sort == "name":
        raw_folders.sort(
            key=lambda folder: str(folder.get("name", "")).casefold()
        )
    else:
        direction = -1 if folder_sort == "modified_desc" else 1
        raw_folders.sort(
            key=lambda folder: (
                direction * float(folder.get("modified_at") or 0),
                str(folder.get("name", "")).casefold(),
            )
        )
    folder_total = len(raw_folders)
    if folder_limit is not None:
        raw_folders = raw_folders[
            folder_offset : folder_offset + folder_limit
        ]
    elif folder_offset:
        raw_folders = raw_folders[folder_offset:]

    folders = []
    for folder in raw_folders:
        source_path = str(folder.get("path", ""))
        display_path = shorten_display_path(source_path, rules)
        folder["display_path"] = display_path
        folder["display_name"] = Path(display_path).name or folder.get("name", "")
        try:
            folder["actor_image_path"] = library.actor_profile_for_directory(
                source_path
            )
        except ValueError:
            folder["actor_image_path"] = None
        folders.append(folder)

    files = []
    for raw_media in browser.get("files", ()):
        if not isinstance(raw_media, Mapping):
            continue
        media = dict(raw_media)
        source_path = str(media.get("path", ""))
        display_path = shorten_display_path(source_path, rules)
        media["display_path"] = display_path
        media["display_name"] = Path(display_path).name or media.get("name", "")
        paths = media.get("paths")
        if isinstance(paths, list):
            media["display_paths"] = [
                shorten_display_path(str(path), rules) for path in paths
            ]
        files.append(media)
    _sort_media_files(files, file_sort)
    return {
        **browser,
        "folders": folders,
        "folder_total": folder_total,
        "folder_offset": folder_offset,
        "folder_limit": folder_limit,
        "files": files,
    }
