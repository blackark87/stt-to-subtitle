"""Small filesystem helpers used by both services."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Sequence
from uuid import uuid4


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write UTF-8 JSON and replace the target only after a complete flush."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def copy_files_atomic(
    pairs: Sequence[tuple[Path, Path]],
    *,
    overwrite: bool,
) -> None:
    """Stage complete file copies before replacing a related target set."""
    if not overwrite:
        for _source, target in pairs:
            if target.exists():
                raise FileExistsError(f"file already exists: {target}")
    staged: list[tuple[Path, Path]] = []
    try:
        for source, target in pairs:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(
                f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
            )
            staged.append((target, temporary))
            with source.open("rb") as source_stream, temporary.open(
                "wb"
            ) as target_stream:
                shutil.copyfileobj(source_stream, target_stream)
                target_stream.flush()
                os.fsync(target_stream.fileno())
        if not overwrite:
            for target, _temporary in staged:
                if target.exists():
                    raise FileExistsError(f"file already exists: {target}")
        for target, temporary in staged:
            temporary.replace(target)
    finally:
        for _target, temporary in staged:
            temporary.unlink(missing_ok=True)
