"""SQLite job and event persistence for the web orchestrator."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Collection, Iterator, Mapping
from uuid import uuid4

from .path_display import (
    PathDisplayRule,
    normalize_path_display_patterns,
)
from .storage_paths import rebase_stored_path
from .translation_prompt import (
    KOREAN_JAV_SYSTEM_PROMPT,
    KOREAN_JAV_REVIEW_PROMPT,
    KOREAN_VARIETY_REVIEW_PROMPT,
    KOREAN_VARIETY_SYSTEM_PROMPT,
)

RUNNING_STATUSES = {
    "extracting",
    "transcription_running",
    "translation_running",
    "rendering",
}

SUCCESS_STATUSES = {
    "audio_completed",
    "transcription_completed",
    "completed",
}
RETRYABLE_STATUSES = {"blocked", "failed"}
STOPPABLE_STATUSES = {
    "queued",
    "extracting",
    "audio_ready",
    "transcription_running",
    "transcribed",
    "translation_running",
    "translated",
    "rendering",
}
TRANSLATION_PAUSABLE_STATUSES = {
    "queued",
    "extracting",
    "audio_ready",
    "transcription_running",
    "transcribed",
    "translation_running",
}
PROMPT_NAME_MAX_LENGTH = 80
PROMPT_TEXT_MAX_LENGTH = 50_000
DEFAULT_PATH_DISPLAY_RULE_ID = "default-actress-content"
DEFAULT_PATH_DISPLAY_SOURCE = "av/japan/{actress}/{content_id}/{filename}"
DEFAULT_PATH_DISPLAY_TARGET = "av/japan/{actress}/{filename}"
LEGACY_PATH_DISPLAY_SOURCE = (
    "{root}/{collection}/{actress}/{content_id}/{filename}"
)
LEGACY_PATH_DISPLAY_TARGET = "{actress}/{filename}"
_COMPARISON_TRANSCRIPTION_SQL = (
    "operation = 'transcribe' AND "
    "json_extract(options_json, '$.comparison_id') IS NOT NULL"
)


@dataclass(frozen=True)
class PromptCategory:
    id: str
    name: str
    translation_prompt: str
    review_prompt: str
    archived: bool
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class PipelineJob:
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
    chunks_total_estimate: int
    chunk_progress_every: int
    translation_chunks_total: int
    translation_chunks_completed: int
    translation_pause_requested: bool
    job_stop_requested: bool
    created_at: float
    status_updated_at: float
    updated_at: float

    @property
    def chunks_in_progress(self) -> int:
        return max(0, self.chunks_created - self.chunks_completed)

    @property
    def transcription_chunks_total(self) -> int:
        return max(self.chunks_created, self.chunks_total_estimate)

    @property
    def transcription_total_is_estimated(self) -> bool:
        return self.chunks_total_estimate > self.chunks_created

    @property
    def transcription_chunks_remaining(self) -> int:
        return max(0, self.transcription_chunks_total - self.chunks_completed)

    @property
    def translation_chunks_in_progress(self) -> int:
        return max(
            0,
            self.translation_chunks_total - self.translation_chunks_completed,
        )

    @property
    def remote_transcription_missing(self) -> bool:
        error = (self.error or "").lower()
        return (
            self.status in {"blocked", "failed"}
            and self.blocked_stage == "transcription"
            and error.startswith(
                "transcription status request failed: http 404:"
            )
            and "job not found" in error
        )

    @property
    def can_stop(self) -> bool:
        return self.status in STOPPABLE_STATUSES and not self.job_stop_requested

    @property
    def can_retry(self) -> bool:
        return self.status in RETRYABLE_STATUSES

    @property
    def can_pause_translation(self) -> bool:
        return (
            self.operation in {"translate", "full"}
            and self.status in TRANSLATION_PAUSABLE_STATUSES
            and not self.translation_pause_requested
            and not self.job_stop_requested
        )

    @property
    def can_start_translation(self) -> bool:
        return self.status == "transcription_completed" and bool(
            self.transcript_path
        )

    @property
    def can_delete_record(self) -> bool:
        return (
            self.status == "audio_completed"
            or self.status in RETRYABLE_STATUSES
            or self.remote_transcription_missing
        )

    @property
    def prompt_category_name(self) -> str:
        snapshot = self.options.get("translation_prompt")
        if isinstance(snapshot, Mapping):
            name = str(snapshot.get("category_name", "")).strip()
            if name:
                return name
        return "JAV (기존 작업)"


class JobStore:
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
        "chunks_total_estimate",
        "chunk_progress_every",
        "translation_chunks_total",
        "translation_chunks_completed",
        "translation_pause_requested",
        "job_stop_requested",
    }

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._change_hook: Callable[[str], None] | None = None
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def set_change_hook(
        self,
        hook: Callable[[str], None] | None,
    ) -> None:
        self._change_hook = hook

    def rebase_artifact_paths(
        self,
        *,
        previous_root: Path,
        current_root: Path,
    ) -> int:
        """Repoint persisted work artifacts after their storage root moves."""
        if previous_root == current_root:
            return 0
        changed = 0
        fields = ("audio_path", "transcript_path", "translation_path")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, audio_path, transcript_path, translation_path
                FROM jobs
                """
            ).fetchall()
            for row in rows:
                original = tuple(row[field] for field in fields)
                rebased = tuple(
                    rebase_stored_path(
                        str(value) if value is not None else None,
                        previous_root=previous_root,
                        current_root=current_root,
                    )
                    for value in original
                )
                if rebased == original:
                    continue
                connection.execute(
                    """
                    UPDATE jobs
                    SET audio_path = ?, transcript_path = ?,
                        translation_path = ?
                    WHERE id = ?
                    """,
                    (*rebased, str(row["id"])),
                )
                changed += 1
        return changed

    def _notify_change(self, job_id: str) -> None:
        if self._change_hook is not None:
            self._change_hook(job_id)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

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
                    chunks_total_estimate INTEGER NOT NULL DEFAULT 0,
                    chunk_progress_every INTEGER NOT NULL DEFAULT 10,
                    translation_chunks_total INTEGER NOT NULL DEFAULT 0,
                    translation_chunks_completed INTEGER NOT NULL DEFAULT 0,
                    translation_pause_requested INTEGER NOT NULL DEFAULT 0,
                    job_stop_requested INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    status_updated_at REAL NOT NULL,
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

                CREATE TABLE IF NOT EXISTS prompt_categories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    translation_prompt TEXT NOT NULL,
                    review_prompt TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS path_display_rules (
                    id TEXT PRIMARY KEY,
                    source_pattern TEXT NOT NULL UNIQUE,
                    display_pattern TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at REAL NOT NULL
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
                "chunks_total_estimate": (
                    "ALTER TABLE jobs ADD COLUMN "
                    "chunks_total_estimate INTEGER NOT NULL DEFAULT 0"
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
                "job_stop_requested": (
                    "ALTER TABLE jobs ADD COLUMN job_stop_requested "
                    "INTEGER NOT NULL DEFAULT 0"
                ),
            }
            for column, statement in migrations.items():
                if column not in columns:
                    connection.execute(statement)
            if "status_updated_at" not in columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN status_updated_at REAL"
                )
                connection.execute(
                    """
                    UPDATE jobs
                    SET status_updated_at = COALESCE(
                        (
                            SELECT MAX(job_events.created_at)
                            FROM job_events
                            WHERE job_events.job_id = jobs.id
                              AND job_events.message NOT LIKE
                                  'transcription chunks:%'
                              AND job_events.message NOT LIKE
                                  'translation checkpoint saved%'
                        ),
                        updated_at,
                        created_at
                    )
                    """
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_status_updated_idx "
                "ON jobs(status_updated_at DESC, created_at DESC)"
            )
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
            now = time.time()
            connection.executemany(
                """
                INSERT OR IGNORE INTO prompt_categories (
                    id, name, translation_prompt, review_prompt,
                    archived, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    (
                        "jav",
                        "JAV",
                        KOREAN_JAV_SYSTEM_PROMPT,
                        KOREAN_JAV_REVIEW_PROMPT,
                        now,
                        now,
                    ),
                    (
                        "variety",
                        "버라이어티",
                        KOREAN_VARIETY_SYSTEM_PROMPT,
                        KOREAN_VARIETY_REVIEW_PROMPT,
                        now,
                        now,
                    ),
                ),
            )
            default_rule_migration = "default_path_display_rule_v1"
            default_rule_seeded = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                (default_rule_migration,),
            ).fetchone()
            if default_rule_seeded is None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO path_display_rules (
                        id, source_pattern, display_pattern,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        DEFAULT_PATH_DISPLAY_RULE_ID,
                        DEFAULT_PATH_DISPLAY_SOURCE,
                        DEFAULT_PATH_DISPLAY_TARGET,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO schema_migrations (name, applied_at) "
                    "VALUES (?, ?)",
                    (default_rule_migration, now),
                )
            corrected_rule_migration = "correct_default_path_display_rule_v2"
            corrected_rule_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                (corrected_rule_migration,),
            ).fetchone()
            if corrected_rule_applied is None:
                conflicting_rule = connection.execute(
                    "SELECT id FROM path_display_rules "
                    "WHERE source_pattern = ? AND id != ?",
                    (
                        DEFAULT_PATH_DISPLAY_SOURCE,
                        DEFAULT_PATH_DISPLAY_RULE_ID,
                    ),
                ).fetchone()
                if conflicting_rule is None:
                    connection.execute(
                        """
                        UPDATE path_display_rules
                        SET source_pattern = ?, display_pattern = ?,
                            updated_at = ?
                        WHERE id = ? AND source_pattern = ?
                          AND display_pattern = ?
                        """,
                        (
                            DEFAULT_PATH_DISPLAY_SOURCE,
                            DEFAULT_PATH_DISPLAY_TARGET,
                            now,
                            DEFAULT_PATH_DISPLAY_RULE_ID,
                            LEGACY_PATH_DISPLAY_SOURCE,
                            LEGACY_PATH_DISPLAY_TARGET,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        DELETE FROM path_display_rules
                        WHERE id = ? AND source_pattern = ?
                          AND display_pattern = ?
                        """,
                        (
                            DEFAULT_PATH_DISPLAY_RULE_ID,
                            LEGACY_PATH_DISPLAY_SOURCE,
                            LEGACY_PATH_DISPLAY_TARGET,
                        ),
                    )
                connection.execute(
                    "INSERT INTO schema_migrations (name, applied_at) "
                    "VALUES (?, ?)",
                    (corrected_rule_migration, now),
                )

    @staticmethod
    def _prompt_category_from_row(
        row: sqlite3.Row | None,
    ) -> PromptCategory | None:
        if row is None:
            return None
        return PromptCategory(
            id=str(row["id"]),
            name=str(row["name"]),
            translation_prompt=str(row["translation_prompt"]),
            review_prompt=str(row["review_prompt"]),
            archived=bool(row["archived"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _normalize_prompt_category_values(
        name: str,
        translation_prompt: str,
        review_prompt: str,
    ) -> tuple[str, str, str]:
        normalized_name = name.strip()
        normalized_translation = translation_prompt.strip()
        normalized_review = review_prompt.strip()
        if not normalized_name:
            raise ValueError("프롬프트 카테고리 이름을 입력하세요.")
        if len(normalized_name) > PROMPT_NAME_MAX_LENGTH:
            raise ValueError("프롬프트 카테고리 이름이 너무 깁니다.")
        if not normalized_translation:
            raise ValueError("번역 프롬프트를 입력하세요.")
        if not normalized_review:
            raise ValueError("검토 프롬프트를 입력하세요.")
        if (
            len(normalized_translation) > PROMPT_TEXT_MAX_LENGTH
            or len(normalized_review) > PROMPT_TEXT_MAX_LENGTH
        ):
            raise ValueError("프롬프트는 각각 50,000자 이하여야 합니다.")
        return normalized_name, normalized_translation, normalized_review

    def list_prompt_categories(
        self,
        *,
        include_archived: bool = False,
    ) -> list[PromptCategory]:
        where = "" if include_archived else "WHERE archived = 0"
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM prompt_categories {where} "
                "ORDER BY archived, name COLLATE NOCASE, created_at"
            ).fetchall()
        return [
            category
            for row in rows
            if (category := self._prompt_category_from_row(row)) is not None
        ]

    def get_prompt_category(self, category_id: str) -> PromptCategory | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM prompt_categories WHERE id = ?",
                (category_id,),
            ).fetchone()
        return self._prompt_category_from_row(row)

    def create_prompt_category(
        self,
        *,
        name: str,
        translation_prompt: str,
        review_prompt: str,
    ) -> PromptCategory:
        values = self._normalize_prompt_category_values(
            name,
            translation_prompt,
            review_prompt,
        )
        category_id = uuid4().hex
        now = time.time()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO prompt_categories (
                        id, name, translation_prompt, review_prompt,
                        archived, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 0, ?, ?)
                    """,
                    (category_id, *values, now, now),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("같은 이름의 프롬프트 카테고리가 있습니다.") from error
        created = self.get_prompt_category(category_id)
        if created is None:
            raise RuntimeError("created prompt category could not be read")
        return created

    def update_prompt_category(
        self,
        category_id: str,
        *,
        name: str,
        translation_prompt: str,
        review_prompt: str,
    ) -> PromptCategory:
        values = self._normalize_prompt_category_values(
            name,
            translation_prompt,
            review_prompt,
        )
        try:
            with self._connect() as connection:
                result = connection.execute(
                    """
                    UPDATE prompt_categories
                    SET name = ?, translation_prompt = ?, review_prompt = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (*values, time.time(), category_id),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("같은 이름의 프롬프트 카테고리가 있습니다.") from error
        if result.rowcount != 1:
            raise ValueError("프롬프트 카테고리를 찾을 수 없습니다.")
        updated = self.get_prompt_category(category_id)
        if updated is None:
            raise RuntimeError("updated prompt category could not be read")
        return updated

    def set_prompt_category_archived(
        self,
        category_id: str,
        *,
        archived: bool,
    ) -> PromptCategory:
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE prompt_categories
                SET archived = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(archived), time.time(), category_id),
            )
        if result.rowcount != 1:
            raise ValueError("프롬프트 카테고리를 찾을 수 없습니다.")
        updated = self.get_prompt_category(category_id)
        if updated is None:
            raise RuntimeError("updated prompt category could not be read")
        return updated

    @staticmethod
    def _path_display_rule_from_row(
        row: sqlite3.Row | None,
    ) -> PathDisplayRule | None:
        if row is None:
            return None
        return PathDisplayRule(
            id=str(row["id"]),
            source_pattern=str(row["source_pattern"]),
            display_pattern=str(row["display_pattern"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def list_path_display_rules(self) -> list[PathDisplayRule]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, source_pattern, display_pattern,
                       created_at, updated_at
                FROM path_display_rules
                ORDER BY created_at, id
                """
            ).fetchall()
        return [
            rule
            for row in rows
            if (rule := self._path_display_rule_from_row(row)) is not None
        ]

    def create_path_display_rule(
        self,
        *,
        source_pattern: str,
        display_pattern: str,
    ) -> PathDisplayRule:
        source, display = normalize_path_display_patterns(
            source_pattern,
            display_pattern,
        )
        rule_id = str(uuid4())
        now = time.time()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO path_display_rules (
                        id, source_pattern, display_pattern,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (rule_id, source, display, now, now),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("같은 원본 패턴의 규칙이 이미 있습니다.") from error
        created = next(
            (
                rule
                for rule in self.list_path_display_rules()
                if rule.id == rule_id
            ),
            None,
        )
        if created is None:
            raise RuntimeError("created path display rule could not be read")
        return created

    def update_path_display_rule(
        self,
        rule_id: str,
        *,
        source_pattern: str,
        display_pattern: str,
    ) -> PathDisplayRule:
        source, display = normalize_path_display_patterns(
            source_pattern,
            display_pattern,
        )
        try:
            with self._connect() as connection:
                result = connection.execute(
                    """
                    UPDATE path_display_rules
                    SET source_pattern = ?, display_pattern = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (source, display, time.time(), rule_id),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("같은 원본 패턴의 규칙이 이미 있습니다.") from error
        if result.rowcount != 1:
            raise ValueError("경로 표시 규칙을 찾을 수 없습니다.")
        updated = next(
            (
                rule
                for rule in self.list_path_display_rules()
                if rule.id == rule_id
            ),
            None,
        )
        if updated is None:
            raise RuntimeError("updated path display rule could not be read")
        return updated

    def delete_path_display_rule(self, rule_id: str) -> None:
        with self._connect() as connection:
            result = connection.execute(
                "DELETE FROM path_display_rules WHERE id = ?",
                (rule_id,),
            )
        if result.rowcount != 1:
            raise ValueError("경로 표시 규칙을 찾을 수 없습니다.")

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
    def _from_row(row: sqlite3.Row | None) -> PipelineJob | None:
        if row is None:
            return None
        return PipelineJob(
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
            chunks_total_estimate=int(row["chunks_total_estimate"]),
            chunk_progress_every=int(row["chunk_progress_every"]),
            translation_chunks_total=int(row["translation_chunks_total"]),
            translation_chunks_completed=int(
                row["translation_chunks_completed"]
            ),
            translation_pause_requested=bool(
                row["translation_pause_requested"]
            ),
            job_stop_requested=bool(row["job_stop_requested"]),
            created_at=float(row["created_at"]),
            status_updated_at=float(row["status_updated_at"]),
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
        status: str = "queued",
        audio_path: str | None = None,
        audio_sha256: str | None = None,
        chunks_total_estimate: int = 0,
    ) -> PipelineJob:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, source_rel, status, force_overwrite, operation,
                    options_json, audio_path, audio_sha256,
                    chunks_total_estimate,
                    created_at, status_updated_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    source_rel,
                    status,
                    int(force_overwrite),
                    operation,
                    json.dumps(dict(options), sort_keys=True),
                    audio_path,
                    audio_sha256,
                    max(0, int(chunks_total_estimate)),
                    now,
                    now,
                    now,
                ),
            )
        self.add_event(
            job_id,
            "info",
            "job queued" if status == "queued" else f"job created in {status}",
        )
        job = self.get(job_id)
        if job is None:
            raise RuntimeError("created job could not be read")
        return job

    def get(self, job_id: str) -> PipelineJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return self._from_row(row)

    def list_jobs(
        self,
        limit: int | None = 100,
        *,
        offset: int = 0,
        statuses: Collection[str] | None = None,
        include_comparison_transcriptions: bool = True,
    ) -> list[PipelineJob]:
        status_values = (
            tuple(sorted(set(statuses))) if statuses is not None else None
        )
        if status_values == ():
            return []
        conditions: list[str] = []
        parameters: list[object] = []
        if status_values is not None:
            placeholders = ", ".join("?" for _ in status_values)
            conditions.append(f"status IN ({placeholders})")
            parameters.extend(status_values)
        if not include_comparison_transcriptions:
            conditions.append(f"NOT ({_COMPARISON_TRANSCRIPTION_SQL})")
        query = "SELECT * FROM jobs"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY status_updated_at DESC, created_at DESC"
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            parameters.extend((limit, offset))
        with self._connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [job for row in rows if (job := self._from_row(row)) is not None]

    def count_jobs(
        self,
        *,
        statuses: Collection[str] | None = None,
        include_comparison_transcriptions: bool = True,
    ) -> int:
        status_values = (
            tuple(sorted(set(statuses))) if statuses is not None else None
        )
        if status_values == ():
            return 0
        conditions: list[str] = []
        parameters: list[object] = []
        if status_values is not None:
            placeholders = ", ".join("?" for _ in status_values)
            conditions.append(f"status IN ({placeholders})")
            parameters.extend(status_values)
        if not include_comparison_transcriptions:
            conditions.append(f"NOT ({_COMPARISON_TRANSCRIPTION_SQL})")
        query = "SELECT COUNT(*) AS count FROM jobs"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        with self._connect() as connection:
            row = connection.execute(query, tuple(parameters)).fetchone()
        return int(row["count"]) if row is not None else 0

    def list_open_jobs(self) -> list[PipelineJob]:
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

    def list_successful_jobs(self, *, limit: int, offset: int) -> list[PipelineJob]:
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

    def latest_jobs_by_source(self) -> dict[str, PipelineJob]:
        latest: dict[str, PipelineJob] = {}
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs "
                "ORDER BY status_updated_at DESC, created_at DESC"
            ).fetchall()
        for row in rows:
            job = self._from_row(row)
            if job is not None:
                latest.setdefault(job.source_rel, job)
        return latest

    def latest_completed_subtitle_jobs(self) -> dict[str, PipelineJob]:
        latest: dict[str, PipelineJob] = {}
        for job in self.list_jobs(limit=None):
            if job.status == "completed" and (job.srt_path or job.ass_path):
                latest.setdefault(job.source_rel, job)
        return latest

    def latest_audio_job(self, source_rel: str) -> PipelineJob | None:
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

    def latest_transcript_job(self, source_rel: str) -> PipelineJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE source_rel = ? AND transcript_path IS NOT NULL
                ORDER BY updated_at DESC, created_at DESC
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

    def dispatchable_ids_with_status(self, status: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs "
                "WHERE status = ? AND job_stop_requested = 0 "
                "ORDER BY created_at",
                (status,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def claim_for_dispatch(
        self,
        job_id: str,
        waiting_status: str,
        running_status: str,
    ) -> bool:
        translation_condition = (
            " AND translation_pause_requested = 0"
            if waiting_status == "transcribed"
            else ""
        )
        with self._connect() as connection:
            now = time.time()
            result = connection.execute(
                "UPDATE jobs SET status = ?, blocked_stage = NULL, "
                "error = NULL, status_updated_at = ?, updated_at = ? "
                "WHERE id = ? AND status = ? AND job_stop_requested = 0"
                f"{translation_condition}",
                (running_status, now, now, job_id, waiting_status),
            )
        claimed = result.rowcount == 1
        if claimed:
            self._notify_change(job_id)
        return claimed

    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        unknown = set(fields) - self._UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"unsupported job fields: {sorted(unknown)}")
        assignments = [f"{field} = ?" for field in fields]
        values = [fields[field] for field in fields]
        now = time.time()
        if "status" in fields:
            assignments.append("status_updated_at = ?")
            values.append(now)
        assignments.append("updated_at = ?")
        values.extend([now, job_id])
        with self._connect() as connection:
            result = connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
        if result.rowcount == 1:
            self._notify_change(job_id)

    def update_if_status(
        self,
        job_id: str,
        statuses: set[str],
        **fields: Any,
    ) -> bool:
        if not statuses or not fields:
            return False
        unknown = set(fields) - self._UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"unsupported job fields: {sorted(unknown)}")
        assignments = [f"{field} = ?" for field in fields]
        values = [fields[field] for field in fields]
        now = time.time()
        if "status" in fields:
            assignments.append("status_updated_at = ?")
            values.append(now)
        assignments.append("updated_at = ?")
        placeholders = ", ".join("?" for _ in statuses)
        values.extend([now, job_id, *sorted(statuses)])
        with self._connect() as connection:
            result = connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} "
                f"WHERE id = ? AND status IN ({placeholders})",
                values,
            )
        updated = result.rowcount == 1
        if updated:
            self._notify_change(job_id)
        return updated

    def delete(self, job_id: str) -> bool:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM job_events WHERE job_id = ?",
                (job_id,),
            )
            result = connection.execute(
                "DELETE FROM jobs WHERE id = ?",
                (job_id,),
            )
        deleted = result.rowcount == 1
        if deleted:
            self._notify_change(job_id)
        return deleted

    def add_event(self, job_id: str, level: str, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO job_events (job_id, level, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (job_id, level, message[:4000], time.time()),
            )
        self._notify_change(job_id)

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
                        error = 'service restarted during this stage',
                        job_stop_requested = 0,
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
