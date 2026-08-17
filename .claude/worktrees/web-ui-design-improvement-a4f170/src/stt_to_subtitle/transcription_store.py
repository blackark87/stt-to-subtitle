"""SQLite persistence for the native transcription service."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

from .time_display import format_kst_iso


@dataclass(frozen=True)
class TranscriptionJob:
    id: str
    idempotency_key: str
    status: str
    audio_path: str
    audio_sha256: str
    options: dict[str, Any]
    result_path: str | None
    error: str | None
    chunks_created: int
    chunks_completed: int
    created_at: float
    updated_at: float

    def public_dict(self, *, report_every: int = 10) -> dict[str, Any]:
        in_progress = max(0, self.chunks_created - self.chunks_completed)
        return {
            "id": self.id,
            "status": self.status,
            "audio_sha256": self.audio_sha256,
            "error": self.error,
            "chunk_progress": {
                "created": self.chunks_created,
                "completed": self.chunks_completed,
                "in_progress": in_progress,
                "report_every": report_every,
            },
            "created_at": format_kst_iso(self.created_at),
            "updated_at": format_kst_iso(self.updated_at),
        }


class TranscriptionStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS transcription_jobs (
                    id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    audio_path TEXT NOT NULL,
                    audio_sha256 TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    result_path TEXT,
                    error TEXT,
                    chunks_created INTEGER NOT NULL DEFAULT 0,
                    chunks_completed INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(transcription_jobs)"
                ).fetchall()
            }
            if "chunks_created" not in columns:
                connection.execute(
                    """
                    ALTER TABLE transcription_jobs
                    ADD COLUMN chunks_created INTEGER NOT NULL DEFAULT 0
                    """
                )
            if "chunks_completed" not in columns:
                connection.execute(
                    """
                    ALTER TABLE transcription_jobs
                    ADD COLUMN chunks_completed INTEGER NOT NULL DEFAULT 0
                    """
                )

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> TranscriptionJob | None:
        if row is None:
            return None
        return TranscriptionJob(
            id=str(row["id"]),
            idempotency_key=str(row["idempotency_key"]),
            status=str(row["status"]),
            audio_path=str(row["audio_path"]),
            audio_sha256=str(row["audio_sha256"]),
            options=json.loads(str(row["options_json"])),
            result_path=(
                str(row["result_path"]) if row["result_path"] is not None else None
            ),
            error=str(row["error"]) if row["error"] is not None else None,
            chunks_created=int(row["chunks_created"]),
            chunks_completed=int(row["chunks_completed"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def get(self, job_id: str) -> TranscriptionJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transcription_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._from_row(row)

    def get_by_idempotency_key(self, key: str) -> TranscriptionJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM transcription_jobs
                WHERE idempotency_key = ?
                """,
                (key,),
            ).fetchone()
        return self._from_row(row)

    def queued_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM transcription_jobs
                WHERE status = 'queued'
                ORDER BY created_at
                """
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def create(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        audio_path: Path,
        audio_sha256: str,
        options: Mapping[str, Any],
    ) -> TranscriptionJob:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO transcription_jobs (
                    id, idempotency_key, status, audio_path, audio_sha256,
                    options_json, result_path, error, created_at, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (
                    job_id,
                    idempotency_key,
                    str(audio_path),
                    audio_sha256,
                    json.dumps(dict(options), sort_keys=True),
                    now,
                    now,
                ),
            )
        job = self.get(job_id)
        if job is None:
            raise RuntimeError("created transcription job could not be read")
        return job

    def update(
        self,
        job_id: str,
        *,
        status: str,
        result_path: Path | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE transcription_jobs
                SET status = ?, result_path = COALESCE(?, result_path),
                    error = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    str(result_path) if result_path is not None else None,
                    error,
                    time.time(),
                    job_id,
                ),
            )

    def requeue(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE transcription_jobs
                SET status = 'queued', error = NULL,
                    chunks_created = 0, chunks_completed = 0,
                    updated_at = ?
                WHERE id = ?
                """,
                (time.time(), job_id),
            )

    def update_chunk_progress(
        self,
        job_id: str,
        *,
        created: int,
        completed: int,
    ) -> None:
        if created < 0 or completed < 0 or completed > created:
            raise ValueError("invalid transcription chunk progress")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE transcription_jobs
                SET chunks_created = ?, chunks_completed = ?, updated_at = ?
                WHERE id = ?
                """,
                (created, completed, time.time(), job_id),
            )

    def fail_interrupted_jobs(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE transcription_jobs
                SET status = 'failed',
                    error = 'service restarted while transcription was running',
                    updated_at = ?
                WHERE status = 'running'
                """,
                (time.time(),),
            )
        return cursor.rowcount
