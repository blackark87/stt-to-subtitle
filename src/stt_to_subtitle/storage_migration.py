"""Safely migrate legacy bind-mounted backend data into one named volume."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
from typing import Any


SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _same_file(source: Path, target: Path) -> bool:
    try:
        return source.samefile(target)
    except (FileNotFoundError, OSError):
        return False


def _copy_file(source: Path, target: Path) -> tuple[str, int]:
    if _same_file(source, target):
        return "same", source.stat().st_size
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if (
            source.stat().st_size == target.stat().st_size
            and _sha256(source) == _sha256(target)
        ):
            return "existing", source.stat().st_size
        raise ValueError(f"migration target already differs: {target}")
    with tempfile.NamedTemporaryFile(
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        shutil.copy2(source, temporary_path)
        if _sha256(source) != _sha256(temporary_path):
            raise ValueError(f"copied file hash mismatch: {source}")
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)
    return "copied", source.stat().st_size


def _backup_sqlite(source: Path, target: Path) -> tuple[str, int]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        with sqlite3.connect(target) as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise ValueError(f"existing SQLite target is not healthy: {target}")
        return "existing", target.stat().st_size
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        # The legacy tree is mounted read-only.  SQLite can still require a
        # writable directory while opening a database, and a stopped service
        # may leave committed data in WAL sidecars.  Stage the complete SQLite
        # file set before using the online backup API.
        with tempfile.TemporaryDirectory(
            dir=target.parent,
            prefix=f".{target.name}.source.",
        ) as staging_directory:
            staged_source = Path(staging_directory) / source.name
            shutil.copy2(source, staged_source)
            for suffix in ("-wal", "-shm"):
                companion = Path(f"{source}{suffix}")
                if companion.is_file():
                    shutil.copy2(
                        companion,
                        Path(f"{staged_source}{suffix}"),
                    )
            with sqlite3.connect(staged_source) as source_db, sqlite3.connect(
                temporary_path
            ) as target_db:
                source_db.backup(target_db)
                result = target_db.execute("PRAGMA quick_check").fetchone()
        if result is None or result[0] != "ok":
            raise ValueError(f"SQLite backup failed integrity check: {source}")
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)
    return "copied", target.stat().st_size


def _copy_tree(
    source_root: Path,
    target_root: Path,
    *,
    include_wav: bool,
) -> dict[str, int]:
    counts = {"copied": 0, "existing": 0, "same": 0, "bytes": 0}
    if not source_root.is_dir():
        return counts
    for source in sorted(source_root.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(source_root)
        if source.name.endswith(("-wal", "-shm")):
            continue
        if (source.suffix.casefold() == ".wav") != include_wav:
            continue
        target = target_root / relative
        if source.suffix.casefold() in SQLITE_SUFFIXES:
            state, size = _backup_sqlite(source, target)
        else:
            state, size = _copy_file(source, target)
        counts[state] += 1
        counts["bytes"] += size
    return counts


def migrate(
    *,
    source_state: Path,
    source_work: Path,
    source_translation: Path,
    target_root: Path,
    audio_root: Path,
) -> dict[str, Any]:
    target_root.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "schema_version": 1,
        "created_at": time.time(),
        "state": _copy_tree(
            source_state,
            target_root / "state",
            include_wav=False,
        ),
        "work": _copy_tree(
            source_work,
            target_root / "work",
            include_wav=False,
        ),
        "translation": _copy_tree(
            source_translation,
            target_root / "translation",
            include_wav=False,
        ),
        "audio": _copy_tree(
            source_work,
            audio_root,
            include_wav=True,
        ),
    }
    databases = sorted(target_root.rglob("*.sqlite3"))
    for database in databases:
        with sqlite3.connect(database) as connection:
            check = connection.execute("PRAGMA quick_check").fetchone()
        if check is None or check[0] != "ok":
            raise ValueError(f"migrated SQLite database is not healthy: {database}")
    result["sqlite_integrity"] = {
        "checked": len(databases),
        "status": "ok",
    }
    manifest = target_root / "storage-migration.json"
    manifest.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-state", type=Path, required=True)
    parser.add_argument("--source-work", type=Path, required=True)
    parser.add_argument("--source-translation", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    arguments = parser.parse_args()
    result = migrate(
        source_state=arguments.source_state,
        source_work=arguments.source_work,
        source_translation=arguments.source_translation,
        target_root=arguments.target_root,
        audio_root=arguments.audio_root,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
