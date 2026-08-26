"""Run JobStore migrations against an isolated SQLite backup."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from typing import Any

from .job_store import JobStore


def _source_connection_uri(source_path: Path) -> str:
    """Open a source snapshot without creating SQLite WAL sidecars."""

    wal_path = source_path.with_name(f"{source_path.name}-wal")
    shm_path = source_path.with_name(f"{source_path.name}-shm")
    if wal_path.is_file() and wal_path.stat().st_size > 0:
        if not shm_path.is_file():
            raise RuntimeError(
                "database has an active WAL without its shared-memory file; "
                "run the check as the database service user or checkpoint "
                "the database first"
            )
        return f"{source_path.as_uri()}?mode=ro"
    return f"{source_path.as_uri()}?mode=ro&immutable=1"


def _migration_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if table is None:
        return []
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(schema_migrations)"
        ).fetchall()
    }
    sequence_expression = "sequence" if "sequence" in columns else "NULL"
    rows = connection.execute(
        f"SELECT name, {sequence_expression} AS sequence, applied_at "
        "FROM schema_migrations ORDER BY applied_at, name"
    ).fetchall()
    return [
        {
            "name": str(row[0]),
            "sequence": int(row[1]) if row[1] is not None else None,
            "applied_at": float(row[2]),
        }
        for row in rows
    ]


def dry_run_job_store_migrations(database_path: Path) -> dict[str, Any]:
    """Migrate a SQLite backup and return a path-free verification report."""

    source_path = database_path.resolve()
    if not source_path.is_file():
        raise ValueError("job database file does not exist")
    with TemporaryDirectory(prefix="stt-job-store-migration-") as directory:
        copied_path = Path(directory) / "jobs.sqlite3"
        source = sqlite3.connect(
            _source_connection_uri(source_path),
            uri=True,
        )
        target = sqlite3.connect(copied_path)
        try:
            source_check = str(
                source.execute("PRAGMA quick_check(1)").fetchone()[0]
            )
            before = _migration_rows(source)
            source.backup(target)
        finally:
            target.close()
            source.close()

        migrated = JobStore(copied_path)
        with sqlite3.connect(copied_path) as connection:
            after = _migration_rows(connection)
        before_by_name = {row["name"]: row for row in before}
        applied = [
            {"name": row["name"], "sequence": row["sequence"]}
            for row in after
            if row["name"] not in before_by_name
        ]
        backfilled = [
            {"name": row["name"], "sequence": row["sequence"]}
            for row in after
            if row["name"] in before_by_name
            and before_by_name[row["name"]]["sequence"] is None
            and row["sequence"] is not None
        ]
        return {
            "source": {
                "size_bytes": source_path.stat().st_size,
                "quick_check": source_check,
                "migration_count": len(before),
            },
            "dry_run": {
                "applied": applied,
                "backfilled_sequences": backfilled,
                "integrity": migrated.database_integrity(),
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="JobStore SQLite file")
    args = parser.parse_args()
    try:
        report = dry_run_job_store_migrations(args.database)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
