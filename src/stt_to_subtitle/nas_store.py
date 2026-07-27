"""SQLite job and event persistence for the NAS orchestrator."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Mapping

RUNNING_STATUSES = {
    "extracting",
    "transcription_running",
    "translation_running",
    "rendering",
}


@dataclass(frozen=True)
class NASJob:
    id: str
    source_rel: str
    status: str
    force_overwrite: bool
    options: dict[str, Any]
    audio_path: str | None
    audio_sha256: str | None
    stt_job_id: str | None
    transcript_path: str | None
    translation_path: str | None
    srt_path: str | None
    ass_path: str | None
    blocked_stage: str | None
    error: str | None
    chunks_created: int
    chunks_completed: int
    chunk_progress_every: int
    created_at: float
    updated_at: float

    @property
    def chunks_in_progress(self) -> int:
        return max(0, self.chunks_created - self.chunks_completed)


class NASStore:
    _UPDATABLE_FIELDS = {
        "status",
        "audio_path",
        "audio_sha256",
        "stt_job_id",
        "transcript_path",
        "translation_path",
        "srt_path",
        "ass_path",
        "blocked_stage",
        "error",
        "chunks_created",
        "chunks_completed",
        "chunk_progress_every",
    }

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    source_rel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    force_overwrite INTEGER NOT NULL,
                    options_json TEXT NOT NULL,
                    audio_path TEXT,
                    audio_sha256 TEXT,
                    stt_job_id TEXT,
                    transcript_path TEXT,
                    translation_path TEXT,
                    srt_path TEXT,
                    ass_path TEXT,
                    blocked_stage TEXT,
                    error TEXT,
                    chunks_created INTEGER NOT NULL DEFAULT 0,
                    chunks_completed INTEGER NOT NULL DEFAULT 0,
                    chunk_progress_every INTEGER NOT NULL DEFAULT 10,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(id)
                );

                CREATE TABLE IF NOT EXISTS remote_server_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    stt_base_url TEXT NOT NULL,
                    stt_token TEXT NOT NULL,
                    lm_base_url TEXT NOT NULL,
                    lm_token TEXT NOT NULL,
                    lm_model TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS jobs_status_idx
                    ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS job_events_job_idx
                    ON job_events(job_id, id);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            migrations = {
                "chunks_created": (
                    "ALTER TABLE jobs ADD COLUMN "
                    "chunks_created INTEGER NOT NULL DEFAULT 0"
                ),
                "chunks_completed": (
                    "ALTER TABLE jobs ADD COLUMN "
                    "chunks_completed INTEGER NOT NULL DEFAULT 0"
                ),
                "chunk_progress_every": (
                    "ALTER TABLE jobs ADD COLUMN "
                    "chunk_progress_every INTEGER NOT NULL DEFAULT 10"
                ),
                "ass_path": "ALTER TABLE jobs ADD COLUMN ass_path TEXT",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)

    def get_remote_server_settings(self) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT stt_base_url, stt_token, lm_base_url, lm_token, lm_model
                FROM remote_server_settings
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "stt_base_url": str(row["stt_base_url"]),
            "stt_token": str(row["stt_token"]),
            "lm_base_url": str(row["lm_base_url"]),
            "lm_token": str(row["lm_token"]),
            "lm_model": str(row["lm_model"]),
        }

    def save_remote_server_settings(
        self,
        *,
        stt_base_url: str,
        stt_token: str,
        lm_base_url: str,
        lm_token: str,
        lm_model: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO remote_server_settings (
                    id, stt_base_url, stt_token,
                    lm_base_url, lm_token, lm_model, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    stt_base_url = excluded.stt_base_url,
                    stt_token = excluded.stt_token,
                    lm_base_url = excluded.lm_base_url,
                    lm_token = excluded.lm_token,
                    lm_model = excluded.lm_model,
                    updated_at = excluded.updated_at
                """,
                (
                    stt_base_url,
                    stt_token,
                    lm_base_url,
                    lm_token,
                    lm_model,
                    time.time(),
                ),
            )

    @staticmethod
    def _from_row(row: sqlite3.Row | None) -> NASJob | None:
        if row is None:
            return None
        return NASJob(
            id=str(row["id"]),
            source_rel=str(row["source_rel"]),
            status=str(row["status"]),
            force_overwrite=bool(row["force_overwrite"]),
            options=json.loads(str(row["options_json"])),
            audio_path=str(row["audio_path"]) if row["audio_path"] else None,
            audio_sha256=(
                str(row["audio_sha256"]) if row["audio_sha256"] else None
            ),
            stt_job_id=str(row["stt_job_id"]) if row["stt_job_id"] else None,
            transcript_path=(
                str(row["transcript_path"]) if row["transcript_path"] else None
            ),
            translation_path=(
                str(row["translation_path"]) if row["translation_path"] else None
            ),
            srt_path=str(row["srt_path"]) if row["srt_path"] else None,
            ass_path=str(row["ass_path"]) if row["ass_path"] else None,
            blocked_stage=(
                str(row["blocked_stage"]) if row["blocked_stage"] else None
            ),
            error=str(row["error"]) if row["error"] else None,
            chunks_created=int(row["chunks_created"]),
            chunks_completed=int(row["chunks_completed"]),
            chunk_progress_every=int(row["chunk_progress_every"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def create(
        self,
        *,
        job_id: str,
        source_rel: str,
        force_overwrite: bool,
        options: Mapping[str, Any],
    ) -> NASJob:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, source_rel, status, force_overwrite, options_json,
                    created_at, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    job_id,
                    source_rel,
                    int(force_overwrite),
                    json.dumps(dict(options), sort_keys=True),
                    now,
                    now,
                ),
            )
        self.add_event(job_id, "info", "job queued")
        job = self.get(job_id)
        if job is None:
            raise RuntimeError("created NAS job could not be read")
        return job

    def get(self, job_id: str) -> NASJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._from_row(row)

    def list_jobs(self, limit: int = 100) -> list[NASJob]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [job for row in rows if (job := self._from_row(row)) is not None]

    def ids_with_status(self, status: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status = ? ORDER BY created_at",
                (status,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - self._UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"unsupported NAS job fields: {sorted(unknown)}")
        assignments = [f"{field} = ?" for field in fields]
        values = [fields[field] for field in fields]
        assignments.append("updated_at = ?")
        values.extend([time.time(), job_id])
        with self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def add_event(self, job_id: str, level: str, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO job_events (job_id, level, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, level, message[:4000], time.time()),
            )

    def events(self, job_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT level, message, created_at
                FROM job_events
                WHERE job_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (job_id, limit),
            ).fetchall()
        return [
            {
                "level": str(row["level"]),
                "message": str(row["message"]),
                "created_at": float(row["created_at"]),
            }
            for row in reversed(rows)
        ]

    def recover_interrupted(self) -> int:
        placeholders = ", ".join("?" for _ in RUNNING_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT id, status FROM jobs WHERE status IN ({placeholders})",
                tuple(RUNNING_STATUSES),
            ).fetchall()
            now = time.time()
            for row in rows:
                stage = str(row["status"]).replace("_running", "")
                connection.execute(
                    """
                    UPDATE jobs
                    SET status = 'blocked', blocked_stage = ?,
                        error = 'NAS service restarted during this stage',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (stage, now, str(row["id"])),
                )
        for row in rows:
            self.add_event(
                str(row["id"]),
                "warning",
                "service restart detected; manual retry is required",
            )
        return len(rows)
