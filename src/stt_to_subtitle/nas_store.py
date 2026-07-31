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

SUCCESS_STATUSES = {"audio_completed", "completed"}


@dataclass(frozen=True)
class NASJob:
    id: str
    source_rel: str
    status: str
    force_overwrite: bool
    operation: str
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
    translation_chunks_total: int
    translation_chunks_completed: int
    translation_pause_requested: bool
    created_at: float
    updated_at: float

    @property
    def chunks_in_progress(self) -> int:
        return max(0, self.chunks_created - self.chunks_completed)

    @property
    def translation_chunks_in_progress(self) -> int:
        return max(
            0,
            self.translation_chunks_total - self.translation_chunks_completed,
        )


class NASStore:
    _UPDATABLE_FIELDS = {
        "status",
        "force_overwrite",
        "operation",
        "options_json",
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
        "translation_chunks_total",
        "translation_chunks_completed",
        "translation_pause_requested",
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
                    operation TEXT NOT NULL DEFAULT 'full',
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
                    translation_chunks_total INTEGER NOT NULL DEFAULT 0,
                    translation_chunks_completed INTEGER NOT NULL DEFAULT 0,
                    translation_pause_requested INTEGER NOT NULL DEFAULT 0,
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
                    translation_workers INTEGER NOT NULL DEFAULT 1,
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
                "operation": (
                    "ALTER TABLE jobs ADD COLUMN "
                    "operation TEXT NOT NULL DEFAULT 'full'"
                ),
                "translation_chunks_total": (
                    "ALTER TABLE jobs ADD COLUMN translation_chunks_total "
                    "INTEGER NOT NULL DEFAULT 0"
                ),
                "translation_chunks_completed": (
                    "ALTER TABLE jobs ADD COLUMN translation_chunks_completed "
                    "INTEGER NOT NULL DEFAULT 0"
                ),
                "translation_pause_requested": (
                    "ALTER TABLE jobs ADD COLUMN translation_pause_requested "
                    "INTEGER NOT NULL DEFAULT 0"
                ),
            }
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)
            server_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(remote_server_settings)"
                ).fetchall()
            }
            if "translation_workers" not in server_columns:
                connection.execute(
                    "ALTER TABLE remote_server_settings ADD COLUMN "
                    "translation_workers INTEGER NOT NULL DEFAULT 1"
                )

    def get_remote_server_settings(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT stt_base_url, stt_token, lm_base_url, lm_token, lm_model,
                       translation_workers
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
            "translation_workers": int(row["translation_workers"]),
        }

    def save_remote_server_settings(
        self,
        *,
        stt_base_url: str,
        stt_token: str,
        lm_base_url: str,
        lm_token: str,
        lm_model: str,
        translation_workers: int = 1,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO remote_server_settings (
                    id, stt_base_url, stt_token,
                    lm_base_url, lm_token, lm_model,
                    translation_workers, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    stt_base_url = excluded.stt_base_url,
                    stt_token = excluded.stt_token,
                    lm_base_url = excluded.lm_base_url,
                    lm_token = excluded.lm_token,
                    lm_model = excluded.lm_model,
                    translation_workers = excluded.translation_workers,
                    updated_at = excluded.updated_at
                """,
                (
                    stt_base_url,
                    stt_token,
                    lm_base_url,
                    lm_token,
                    lm_model,
                    translation_workers,
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
            operation=str(row["operation"]),
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
            translation_chunks_total=int(row["translation_chunks_total"]),
            translation_chunks_completed=int(
                row["translation_chunks_completed"]
            ),
            translation_pause_requested=bool(
                row["translation_pause_requested"]
            ),
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
        operation: str = "full",
    ) -> NASJob:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, source_rel, status, force_overwrite, operation,
                    options_json,
                    created_at, updated_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    source_rel,
                    int(force_overwrite),
                    operation,
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

    def list_jobs(self, limit: int | None = 100) -> list[NASJob]:
        with self._connect() as connection:
            if limit is None:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [job for row in rows if (job := self._from_row(row)) is not None]

    def list_open_jobs(self) -> list[NASJob]:
        placeholders = ", ".join("?" for _ in SUCCESS_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE status NOT IN ({placeholders})
                ORDER BY created_at DESC
                """,
                tuple(sorted(SUCCESS_STATUSES)),
            ).fetchall()
        return [job for row in rows if (job := self._from_row(row)) is not None]

    def list_successful_jobs(self, *, limit: int, offset: int) -> list[NASJob]:
        placeholders = ", ".join("?" for _ in SUCCESS_STATUSES)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE status IN ({placeholders})
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (*sorted(SUCCESS_STATUSES), limit, offset),
            ).fetchall()
        return [job for row in rows if (job := self._from_row(row)) is not None]

    def count_successful_jobs(self) -> int:
        placeholders = ", ".join("?" for _ in SUCCESS_STATUSES)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM jobs "
                f"WHERE status IN ({placeholders})",
                tuple(sorted(SUCCESS_STATUSES)),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def latest_jobs_by_source(self) -> dict[str, NASJob]:
        latest: dict[str, NASJob] = {}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY updated_at DESC, created_at DESC"
            ).fetchall()
        for row in rows:
            job = self._from_row(row)
            if job is not None:
                latest.setdefault(job.source_rel, job)
        return latest

    def latest_completed_subtitle_jobs(self) -> dict[str, NASJob]:
        latest: dict[str, NASJob] = {}
        for job in self.list_jobs(limit=None):
            if job.status == "completed" and (job.srt_path or job.ass_path):
                latest.setdefault(job.source_rel, job)
        return latest

    def latest_audio_job(self, source_rel: str) -> NASJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE source_rel = ? AND status = 'audio_completed'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (source_rel,),
            ).fetchone()
        return self._from_row(row)

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
