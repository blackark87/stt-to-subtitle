"""Small ordered migration runner for SQLite stores."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import re
import sqlite3
import time


MIGRATION_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


@dataclass(frozen=True)
class Migration:
    sequence: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


def ordered_migrations(
    migrations: Sequence[Migration],
) -> tuple[Migration, ...]:
    ordered = tuple(
        sorted(migrations, key=lambda migration: migration.sequence)
    )
    sequences = [migration.sequence for migration in ordered]
    names = [migration.name for migration in ordered]
    if any(sequence < 1 for sequence in sequences):
        raise ValueError("migration sequences must be positive")
    if len(set(sequences)) != len(sequences):
        raise ValueError("migration sequences must be unique")
    if len(set(names)) != len(names):
        raise ValueError("migration names must be unique")
    if any(MIGRATION_NAME_PATTERN.fullmatch(name) is None for name in names):
        raise ValueError("invalid migration name")
    return ordered


def run_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration],
) -> tuple[str, ...]:
    """Apply missing migrations in sequence order and record completion."""

    ordered = ordered_migrations(migrations)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY,
            sequence INTEGER,
            applied_at REAL NOT NULL
        )
        """
    )
    columns = {
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(schema_migrations)"
        ).fetchall()
    }
    if "sequence" not in columns:
        connection.execute(
            "ALTER TABLE schema_migrations ADD COLUMN sequence INTEGER"
        )
    applied = {
        str(row[0]): (int(row[1]) if row[1] is not None else None)
        for row in connection.execute(
            "SELECT name, sequence FROM schema_migrations"
        ).fetchall()
    }
    for migration in ordered:
        recorded_sequence = applied.get(migration.name)
        if (
            recorded_sequence is not None
            and recorded_sequence != migration.sequence
        ):
            raise RuntimeError(
                "migration sequence changed for "
                f"{migration.name}: {recorded_sequence} != {migration.sequence}"
            )
        if migration.name in applied and recorded_sequence is None:
            connection.execute(
                "UPDATE schema_migrations SET sequence = ? WHERE name = ?",
                (migration.sequence, migration.name),
            )
            applied[migration.name] = migration.sequence
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "schema_migrations_sequence_idx ON schema_migrations(sequence) "
        "WHERE sequence IS NOT NULL"
    )
    completed: list[str] = []
    for migration in ordered:
        if migration.name in applied:
            continue
        savepoint = f"schema_migration_{migration.sequence}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            migration.apply(connection)
            connection.execute(
                "INSERT INTO schema_migrations (name, sequence, applied_at) "
                "VALUES (?, ?, ?)",
                (migration.name, migration.sequence, time.time()),
            )
        except BaseException:
            connection.execute(f"ROLLBACK TO {savepoint}")
            connection.execute(f"RELEASE {savepoint}")
            raise
        connection.execute(f"RELEASE {savepoint}")
        applied[migration.name] = migration.sequence
        completed.append(migration.name)
    return tuple(completed)


def execute_sql_statements(
    connection: sqlite3.Connection,
    script: str,
) -> None:
    """Execute a SQL script without sqlite3.executescript auto-commits."""

    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if not sqlite3.complete_statement(pending):
            continue
        statement = pending.strip()
        pending = ""
        if statement:
            connection.execute(statement)
    if pending.strip():
        raise ValueError("incomplete SQL migration statement")
