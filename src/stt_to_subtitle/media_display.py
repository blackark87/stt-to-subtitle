"""Display-only media listing transformations for the web API."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .path_display import shorten_display_path


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
    folder_limit: int | None = None,
) -> dict[str, object]:
    """Add safe display paths and optional actor images without changing IDs."""
    raw_folders = [
        dict(folder)
        for folder in browser.get("folders", ())
        if isinstance(folder, Mapping)
    ]
    if folder_sort not in {"name", "modified_desc", "modified_asc"}:
        raise ValueError("unsupported media folder sort")
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
        raw_folders = raw_folders[:folder_limit]

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
    return {
        **browser,
        "folders": folders,
        "folder_total": folder_total,
        "files": files,
    }
