"""SQLite job and event persistence for the web orchestrator."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable, Collection, Iterator, Mapping, Sequence
from uuid import uuid4

from .path_display import (
    PathDisplayRule,
    normalize_path_display_patterns,
)
from .job_state import JobPhase, JobReason, JobState, structured_state_from_legacy
from .storage_paths import rebase_stored_path
from .translation_prompt import (
    KOREAN_JAV_SYSTEM_PROMPT,
    KOREAN_JAV_REVIEW_PROMPT,
    KOREAN_VARIETY_REVIEW_PROMPT,
    KOREAN_VARIETY_SYSTEM_PROMPT,
)

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
RUNNING_JOB_STATUSES = {
    "extracting",
    "transcription_running",
    "translation_running",
    "rendering",
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


class WorkerLeaseLost(RuntimeError):
    """Raised when a superseded worker tries to publish stage output."""


def _canonical_json_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    phase: str
    state: str
    reason_code: str | None
    attempt: int
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
    lease_owner: str | None
    lease_expires_at: float | None
    lease_token: int
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
        return self.state in {"blocked", "stopped", "failed"}

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
            or self.can_retry
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
        "phase",
        "state",
        "reason_code",
        "attempt",
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
        "lease_owner",
        "lease_expires_at",
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
            translation_rows = connection.execute(
                "SELECT id, artifact_path FROM translation_generations"
            ).fetchall()
            for row in translation_rows:
                original = str(row["artifact_path"])
                rebased = rebase_stored_path(
                    original,
                    previous_root=previous_root,
                    current_root=current_root,
                )
                if rebased == original:
                    continue
                connection.execute(
                    """
                    UPDATE translation_generations
                    SET artifact_path = ?
                    WHERE id = ?
                    """,
                    (rebased, str(row["id"])),
                )
                changed += 1
            subtitle_rows = connection.execute(
                """
                SELECT id, srt_artifact_path, ass_artifact_path
                FROM subtitle_generations
                """
            ).fetchall()
            for row in subtitle_rows:
                original = (
                    str(row["srt_artifact_path"]),
                    str(row["ass_artifact_path"]),
                )
                rebased = tuple(
                    rebase_stored_path(
                        path,
                        previous_root=previous_root,
                        current_root=current_root,
                    )
                    for path in original
                )
                if rebased == original:
                    continue
                connection.execute(
                    """
                    UPDATE subtitle_generations
                    SET srt_artifact_path = ?, ass_artifact_path = ?
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
                    phase TEXT NOT NULL DEFAULT 'extraction',
                    state TEXT NOT NULL DEFAULT 'waiting',
                    reason_code TEXT,
                    attempt INTEGER NOT NULL DEFAULT 1,
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
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    lease_token INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS dependency_states (
                    dependency TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    reason_code TEXT,
                    last_error TEXT,
                    updated_at REAL NOT NULL,
                    CHECK (dependency IN ('stt', 'translation_lm')),
                    CHECK (state IN ('unknown', 'ready', 'lost', 'offline'))
                );

                CREATE TABLE IF NOT EXISTS subtitle_validator_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    base_url TEXT NOT NULL,
                    token TEXT NOT NULL,
                    model TEXT NOT NULL,
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

                CREATE TABLE IF NOT EXISTS subtitle_validations (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    source_rel TEXT NOT NULL,
                    external_path TEXT NOT NULL,
                    external_hash TEXT NOT NULL,
                    candidate_path TEXT NOT NULL,
                    candidate_hash TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    llm_json TEXT,
                    validator_model TEXT,
                    validator_input_hash TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(id),
                    UNIQUE (job_id, external_hash, candidate_hash)
                );

                CREATE TABLE IF NOT EXISTS translation_generations (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    generation_number INTEGER NOT NULL,
                    transcript_job_id TEXT NOT NULL,
                    transcript_hash TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    endpoint_key TEXT NOT NULL,
                    model TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0,
                    origin TEXT NOT NULL,
                    supersedes_generation_id TEXT,
                    artifact_path TEXT NOT NULL,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL,
                    FOREIGN KEY (job_id) REFERENCES jobs(id),
                    FOREIGN KEY (supersedes_generation_id)
                        REFERENCES translation_generations(id),
                    UNIQUE (job_id, generation_number)
                );

                CREATE TABLE IF NOT EXISTS translation_batches (
                    generation_id TEXT NOT NULL,
                    batch_index INTEGER NOT NULL,
                    generation_attempt INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    segment_ids_json TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    output_hash TEXT NOT NULL,
                    state TEXT NOT NULL,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (generation_id, batch_index),
                    FOREIGN KEY (generation_id)
                        REFERENCES translation_generations(id)
                );

                CREATE TABLE IF NOT EXISTS translation_items (
                    generation_id TEXT NOT NULL,
                    segment_id TEXT NOT NULL,
                    segment_index INTEGER NOT NULL,
                    source_hash TEXT NOT NULL,
                    translated_text TEXT NOT NULL,
                    batch_index INTEGER NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (generation_id, segment_id),
                    FOREIGN KEY (generation_id)
                        REFERENCES translation_generations(id)
                );

                CREATE TABLE IF NOT EXISTS subtitle_generations (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    generation_number INTEGER NOT NULL,
                    translation_generation_id TEXT,
                    transcript_hash TEXT NOT NULL,
                    translation_hash TEXT NOT NULL,
                    renderer_version TEXT NOT NULL,
                    render_hash TEXT NOT NULL,
                    srt_artifact_path TEXT NOT NULL,
                    ass_artifact_path TEXT NOT NULL,
                    srt_hash TEXT NOT NULL,
                    ass_hash TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    supersedes_generation_id TEXT,
                    created_at REAL NOT NULL,
                    published_at REAL,
                    FOREIGN KEY (job_id) REFERENCES jobs(id),
                    FOREIGN KEY (translation_generation_id)
                        REFERENCES translation_generations(id),
                    FOREIGN KEY (supersedes_generation_id)
                        REFERENCES subtitle_generations(id),
                    UNIQUE (job_id, generation_number)
                );

                CREATE TABLE IF NOT EXISTS subtitle_publications (
                    source_rel TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    subtitle_generation_id TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(id),
                    FOREIGN KEY (subtitle_generation_id)
                        REFERENCES subtitle_generations(id)
                );

                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS jobs_status_idx
                    ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS job_events_job_idx
                    ON job_events(job_id, id);
                CREATE INDEX IF NOT EXISTS subtitle_validations_job_idx
                    ON subtitle_validations(job_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS translation_generations_job_idx
                    ON translation_generations(job_id, generation_number DESC);
                CREATE INDEX IF NOT EXISTS translation_items_generation_idx
                    ON translation_items(generation_id, segment_index);
                CREATE INDEX IF NOT EXISTS subtitle_generations_job_idx
                    ON subtitle_generations(job_id, generation_number DESC);
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
                "phase": (
                    "ALTER TABLE jobs ADD COLUMN "
                    "phase TEXT NOT NULL DEFAULT 'extraction'"
                ),
                "state": (
                    "ALTER TABLE jobs ADD COLUMN "
                    "state TEXT NOT NULL DEFAULT 'waiting'"
                ),
                "reason_code": (
                    "ALTER TABLE jobs ADD COLUMN reason_code TEXT"
                ),
                "attempt": (
                    "ALTER TABLE jobs ADD COLUMN "
                    "attempt INTEGER NOT NULL DEFAULT 1"
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
                "lease_owner": (
                    "ALTER TABLE jobs ADD COLUMN lease_owner TEXT"
                ),
                "lease_expires_at": (
                    "ALTER TABLE jobs ADD COLUMN lease_expires_at REAL"
                ),
                "lease_token": (
                    "ALTER TABLE jobs ADD COLUMN "
                    "lease_token INTEGER NOT NULL DEFAULT 0"
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
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_state_phase_idx "
                "ON jobs(state, phase, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_lease_idx "
                "ON jobs(status, lease_expires_at)"
            )
            structured_state_migration = "structured_job_state_v1"
            structured_state_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE name = ?",
                (structured_state_migration,),
            ).fetchone()
            if structured_state_applied is None:
                rows = connection.execute(
                    "SELECT id, status, operation, blocked_stage, error FROM jobs"
                ).fetchall()
                for row in rows:
                    projected = structured_state_from_legacy(
                        status=str(row["status"]),
                        operation=str(row["operation"]),
                        blocked_stage=(
                            str(row["blocked_stage"])
                            if row["blocked_stage"]
                            else None
                        ),
                        error=str(row["error"]) if row["error"] else None,
                    )
                    connection.execute(
                        "UPDATE jobs SET phase = ?, state = ?, reason_code = ? "
                        "WHERE id = ?",
                        (
                            projected.phase.value,
                            projected.state.value,
                            (
                                projected.reason_code.value
                                if projected.reason_code is not None
                                else None
                            ),
                            str(row["id"]),
                        ),
                    )
                connection.execute(
                    "INSERT INTO schema_migrations (name, applied_at) "
                    "VALUES (?, ?)",
                    (structured_state_migration, time.time()),
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

    def get_dependency_state(self, dependency: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT dependency, state, reason_code, last_error, updated_at
                FROM dependency_states
                WHERE dependency = ?
                """,
                (dependency,),
            ).fetchone()
        if row is None:
            return None
        return {
            "dependency": str(row["dependency"]),
            "state": str(row["state"]),
            "reason_code": (
                str(row["reason_code"])
                if row["reason_code"] is not None
                else None
            ),
            "last_error": (
                str(row["last_error"])
                if row["last_error"] is not None
                else None
            ),
            "updated_at": float(row["updated_at"]),
        }

    def save_dependency_state(
        self,
        dependency: str,
        *,
        state: str,
        reason_code: str | None = None,
        error: str | None = None,
    ) -> None:
        if dependency not in {"stt", "translation_lm"}:
            raise ValueError("unsupported dependency state")
        if state not in {"unknown", "ready", "lost", "offline"}:
            raise ValueError("unsupported dependency status")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO dependency_states (
                    dependency, state, reason_code, last_error, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(dependency) DO UPDATE SET
                    state = excluded.state,
                    reason_code = excluded.reason_code,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (
                    dependency,
                    state,
                    reason_code,
                    error[:2000] if error else None,
                    time.time(),
                ),
            )

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

    def get_subtitle_validator_settings(self) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT base_url, token, model
                FROM subtitle_validator_settings
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "base_url": str(row["base_url"]),
            "token": str(row["token"]),
            "model": str(row["model"]),
        }

    def save_subtitle_validator_settings(
        self,
        *,
        base_url: str,
        token: str,
        model: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO subtitle_validator_settings (
                    id, base_url, token, model, updated_at
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    base_url = excluded.base_url,
                    token = excluded.token,
                    model = excluded.model,
                    updated_at = excluded.updated_at
                """,
                (base_url, token, model, time.time()),
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
            phase=str(row["phase"]),
            state=str(row["state"]),
            reason_code=(
                str(row["reason_code"]) if row["reason_code"] else None
            ),
            attempt=int(row["attempt"]),
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
            lease_owner=(
                str(row["lease_owner"]) if row["lease_owner"] else None
            ),
            lease_expires_at=(
                float(row["lease_expires_at"])
                if row["lease_expires_at"] is not None
                else None
            ),
            lease_token=int(row["lease_token"]),
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
        projected = structured_state_from_legacy(
            status=status,
            operation=operation,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, source_rel, status, force_overwrite, operation,
                    phase, state, reason_code, attempt,
                    options_json, audio_path, audio_sha256,
                    chunks_total_estimate,
                    created_at, status_updated_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    source_rel,
                    status,
                    int(force_overwrite),
                    operation,
                    projected.phase.value,
                    projected.state.value,
                    (
                        projected.reason_code.value
                        if projected.reason_code is not None
                        else None
                    ),
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

    @staticmethod
    def _job_filter_clause(
        *,
        statuses: Collection[str] | None,
        states: Collection[str] | None,
        phases: Collection[str] | None,
        legacy_phase_statuses: Collection[str] | None,
        include_comparison_transcriptions: bool,
    ) -> tuple[str, list[object]] | None:
        def normalized(
            values: Collection[str] | None,
        ) -> tuple[str, ...] | None:
            return tuple(sorted(set(values))) if values is not None else None

        status_values = normalized(statuses)
        state_values = normalized(states)
        phase_values = normalized(phases)
        legacy_phase_values = normalized(legacy_phase_statuses)
        if () in (status_values, state_values, phase_values):
            return None
        if legacy_phase_values == ():
            legacy_phase_values = None

        conditions: list[str] = []
        parameters: list[object] = []
        for column, values in (
            ("status", status_values),
            ("state", state_values),
        ):
            if values is None:
                continue
            placeholders = ", ".join("?" for _ in values)
            conditions.append(f"{column} IN ({placeholders})")
            parameters.extend(values)

        if phase_values is not None:
            phase_placeholders = ", ".join("?" for _ in phase_values)
            if legacy_phase_values is not None:
                status_placeholders = ", ".join(
                    "?" for _ in legacy_phase_values
                )
                conditions.append(
                    f"(phase IN ({phase_placeholders}) OR "
                    f"status IN ({status_placeholders}))"
                )
                parameters.extend((*phase_values, *legacy_phase_values))
            else:
                conditions.append(f"phase IN ({phase_placeholders})")
                parameters.extend(phase_values)
        elif legacy_phase_values is not None:
            placeholders = ", ".join("?" for _ in legacy_phase_values)
            conditions.append(f"status IN ({placeholders})")
            parameters.extend(legacy_phase_values)

        if not include_comparison_transcriptions:
            conditions.append(f"NOT ({_COMPARISON_TRANSCRIPTION_SQL})")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return where, parameters

    def list_jobs(
        self,
        limit: int | None = 100,
        *,
        offset: int = 0,
        statuses: Collection[str] | None = None,
        states: Collection[str] | None = None,
        phases: Collection[str] | None = None,
        legacy_phase_statuses: Collection[str] | None = None,
        include_comparison_transcriptions: bool = True,
    ) -> list[PipelineJob]:
        filtered = self._job_filter_clause(
            statuses=statuses,
            states=states,
            phases=phases,
            legacy_phase_statuses=legacy_phase_statuses,
            include_comparison_transcriptions=include_comparison_transcriptions,
        )
        if filtered is None:
            return []
        where, parameters = filtered
        query = "SELECT * FROM jobs" + where
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
        states: Collection[str] | None = None,
        phases: Collection[str] | None = None,
        legacy_phase_statuses: Collection[str] | None = None,
        include_comparison_transcriptions: bool = True,
    ) -> int:
        filtered = self._job_filter_clause(
            statuses=statuses,
            states=states,
            phases=phases,
            legacy_phase_statuses=legacy_phase_statuses,
            include_comparison_transcriptions=include_comparison_transcriptions,
        )
        if filtered is None:
            return 0
        where, parameters = filtered
        query = "SELECT COUNT(*) AS count FROM jobs" + where
        with self._connect() as connection:
            row = connection.execute(query, tuple(parameters)).fetchone()
        return int(row["count"]) if row is not None else 0

    def list_open_jobs(self) -> list[PipelineJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state != 'done'
                ORDER BY created_at DESC
                """
            ).fetchall()
        return [job for row in rows if (job := self._from_row(row)) is not None]

    def list_successful_jobs(self, *, limit: int, offset: int) -> list[PipelineJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'done'
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return [job for row in rows if (job := self._from_row(row)) is not None]

    def count_successful_jobs(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE state = 'done'",
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
            now = time.time()
            rows = connection.execute(
                "SELECT id FROM jobs "
                "WHERE status = ? AND job_stop_requested = 0 "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?) "
                "ORDER BY created_at",
                (status, now),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def claim_for_dispatch(
        self,
        job_id: str,
        waiting_status: str,
        running_status: str,
        *,
        lease_owner: str = "legacy-dispatcher",
        lease_seconds: float = 60.0,
    ) -> int | None:
        if not lease_owner.strip():
            raise ValueError("lease owner is required")
        if lease_seconds <= 0:
            raise ValueError("lease seconds must be positive")
        translation_condition = (
            " AND translation_pause_requested = 0"
            if waiting_status == "transcribed"
            else ""
        )
        current = self.get(job_id)
        if current is None:
            return False
        projected = structured_state_from_legacy(
            status=running_status,
            operation=current.operation,
        )
        with self._connect() as connection:
            now = time.time()
            result = connection.execute(
                "UPDATE jobs SET status = ?, phase = ?, state = ?, "
                "reason_code = NULL, blocked_stage = NULL, error = NULL, "
                "lease_owner = ?, lease_expires_at = ?, "
                "lease_token = lease_token + 1, "
                "status_updated_at = ?, updated_at = ? "
                "WHERE id = ? AND status = ? AND job_stop_requested = 0 "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?)"
                f"{translation_condition}",
                (
                    running_status,
                    projected.phase.value,
                    projected.state.value,
                    lease_owner,
                    now + lease_seconds,
                    now,
                    now,
                    job_id,
                    waiting_status,
                    now,
                ),
            )
            claimed = connection.execute(
                "SELECT lease_token FROM jobs "
                "WHERE id = ? AND lease_owner = ?",
                (job_id, lease_owner),
            ).fetchone()
        lease_token = (
            int(claimed["lease_token"])
            if result.rowcount == 1 and claimed is not None
            else None
        )
        if lease_token is not None:
            self._notify_change(job_id)
        return lease_token

    def recoverable_running_jobs(
        self,
        statuses: Collection[str],
    ) -> list[PipelineJob]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        now = time.time()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE status IN ({placeholders})
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY created_at
                """,
                (*sorted(statuses), now),
            ).fetchall()
        return [
            job for row in rows if (job := self._from_row(row)) is not None
        ]

    def claim_recovery_lease(
        self,
        job_id: str,
        status: str,
        *,
        lease_owner: str,
        lease_seconds: float,
    ) -> int | None:
        if not lease_owner.strip():
            raise ValueError("lease owner is required")
        if lease_seconds <= 0:
            raise ValueError("lease seconds must be positive")
        now = time.time()
        with self._connect() as connection:
            result = connection.execute(
                """
                UPDATE jobs
                SET lease_owner = ?, lease_expires_at = ?,
                    lease_token = lease_token + 1
                WHERE id = ? AND status = ?
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                """,
                (
                    lease_owner,
                    now + lease_seconds,
                    job_id,
                    status,
                    now,
                ),
            )
            claimed = connection.execute(
                "SELECT lease_token FROM jobs "
                "WHERE id = ? AND lease_owner = ?",
                (job_id, lease_owner),
            ).fetchone()
        if result.rowcount != 1 or claimed is None:
            return None
        return int(claimed["lease_token"])

    def refresh_job_lease(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_token: int | None = None,
        lease_seconds: float,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("lease seconds must be positive")
        now = time.time()
        token_condition = " AND lease_token = ?" if lease_token is not None else ""
        parameters: list[Any] = [
            now + lease_seconds,
            job_id,
            lease_owner,
            now,
        ]
        if lease_token is not None:
            parameters.append(lease_token)
        with self._connect() as connection:
            result = connection.execute(
                f"""
                UPDATE jobs
                SET lease_expires_at = ?
                WHERE id = ? AND lease_owner = ?
                  AND lease_expires_at > ?
                  AND status IN (
                      'extracting', 'transcription_running',
                      'translation_running', 'rendering'
                  )
                  {token_condition}
                """,
                parameters,
            )
        return result.rowcount == 1

    def release_job_lease(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_token: int | None = None,
    ) -> bool:
        token_condition = " AND lease_token = ?" if lease_token is not None else ""
        parameters: list[Any] = [job_id, lease_owner]
        if lease_token is not None:
            parameters.append(lease_token)
        with self._connect() as connection:
            result = connection.execute(
                f"""
                UPDATE jobs
                SET lease_owner = NULL, lease_expires_at = NULL
                WHERE id = ? AND lease_owner = ?
                {token_condition}
                """,
                parameters,
            )
        return result.rowcount == 1

    def update_if_lease(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_token: int,
        **fields: Any,
    ) -> bool:
        if not fields:
            return False
        fields = self._with_structured_state(job_id, fields)
        self._validate_structured_fields(fields)
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
        values.extend([now, job_id, lease_owner, lease_token, now])
        with self._connect() as connection:
            result = connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} "
                "WHERE id = ? AND lease_owner = ? AND lease_token = ? "
                "AND lease_expires_at > ?",
                values,
            )
        updated = result.rowcount == 1
        if updated:
            self._notify_change(job_id)
        return updated

    def lease_is_active(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_token: int,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE id = ? AND lease_owner = ? AND lease_token = ?
                  AND lease_expires_at > ?
                """,
                (job_id, lease_owner, lease_token, time.time()),
            ).fetchone()
        return row is not None

    def update(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields = self._with_structured_state(job_id, fields)
        self._validate_structured_fields(fields)
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
        fields = self._with_structured_state(job_id, fields)
        self._validate_structured_fields(fields)
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

    def _with_structured_state(
        self,
        job_id: str,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        expanded = dict(fields)
        if "status" not in expanded:
            return expanded
        if str(expanded["status"]) not in RUNNING_JOB_STATUSES:
            expanded.setdefault("lease_owner", None)
            expanded.setdefault("lease_expires_at", None)
        current = self.get(job_id)
        if current is None:
            return expanded
        projected = structured_state_from_legacy(
            status=str(expanded["status"]),
            operation=str(expanded.get("operation", current.operation)),
            blocked_stage=(
                str(expanded["blocked_stage"])
                if expanded.get("blocked_stage")
                else current.blocked_stage
            ),
            error=(
                str(expanded["error"])
                if expanded.get("error")
                else None
            ),
            detect_legacy_user_stop=False,
        )
        expanded.setdefault("phase", projected.phase.value)
        expanded.setdefault("state", projected.state.value)
        expanded.setdefault(
            "reason_code",
            (
                projected.reason_code.value
                if projected.reason_code is not None
                else None
            ),
        )
        return expanded

    @staticmethod
    def _validate_structured_fields(fields: Mapping[str, Any]) -> None:
        try:
            if "phase" in fields:
                JobPhase(str(fields["phase"]))
            if "state" in fields:
                JobState(str(fields["state"]))
            if fields.get("reason_code") is not None:
                JobReason(str(fields["reason_code"]))
        except ValueError as error:
            raise ValueError("invalid structured job state") from error
        if "attempt" in fields and int(fields["attempt"]) < 1:
            raise ValueError("job attempt must be at least 1")

    def delete(self, job_id: str) -> bool:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM subtitle_validations WHERE job_id = ?",
                (job_id,),
            )
            generation_ids = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM translation_generations WHERE job_id = ?",
                    (job_id,),
                ).fetchall()
            ]
            connection.execute(
                "DELETE FROM subtitle_publications WHERE job_id = ?",
                (job_id,),
            )
            connection.execute(
                "DELETE FROM subtitle_generations WHERE job_id = ?",
                (job_id,),
            )
            for generation_id in generation_ids:
                connection.execute(
                    "DELETE FROM translation_items WHERE generation_id = ?",
                    (generation_id,),
                )
                connection.execute(
                    "DELETE FROM translation_batches WHERE generation_id = ?",
                    (generation_id,),
                )
            connection.execute(
                "DELETE FROM translation_generations WHERE job_id = ?",
                (job_id,),
            )
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

    def create_translation_generation(
        self,
        *,
        generation_id: str,
        job_id: str,
        transcript_job_id: str,
        transcript_hash: str,
        prompt_hash: str,
        endpoint_key: str,
        model: str,
        config_hash: str,
        artifact_path: str,
        origin: str,
        force_new: bool = False,
    ) -> dict[str, Any]:
        if origin not in {"automatic", "legacy", "restart", "manual"}:
            raise ValueError("invalid translation generation origin")
        now = time.time()
        with self._connect() as connection:
            job = connection.execute(
                "SELECT id FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise ValueError("job not found")
            latest = connection.execute(
                """
                SELECT * FROM translation_generations
                WHERE job_id = ?
                ORDER BY generation_number DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if (
                not force_new
                and latest is not None
                and str(latest["config_hash"]) == config_hash
            ):
                return self._translation_generation_from_row(latest)
            generation_number = (
                int(latest["generation_number"]) + 1
                if latest is not None
                else 1
            )
            supersedes = str(latest["id"]) if latest is not None else None
            connection.execute(
                """
                INSERT INTO translation_generations (
                    id, job_id, generation_number,
                    transcript_job_id, transcript_hash, prompt_hash,
                    endpoint_key, model, config_hash,
                    state, attempt, origin, supersedes_generation_id,
                    artifact_path, last_error,
                    created_at, updated_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'partial', 0, ?, ?, ?,
                          NULL, ?, ?, NULL)
                """,
                (
                    generation_id,
                    job_id,
                    generation_number,
                    transcript_job_id,
                    transcript_hash,
                    prompt_hash,
                    endpoint_key,
                    model,
                    config_hash,
                    origin,
                    supersedes,
                    artifact_path,
                    now,
                    now,
                ),
            )
            created = connection.execute(
                "SELECT * FROM translation_generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
        return self._translation_generation_from_row(created)

    def latest_translation_generation(
        self,
        job_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM translation_generations
                WHERE job_id = ?
                ORDER BY generation_number DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return (
            self._translation_generation_from_row(row)
            if row is not None
            else None
        )

    def get_translation_generation(
        self,
        generation_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM translation_generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
        return (
            self._translation_generation_from_row(row)
            if row is not None
            else None
        )

    def list_translation_generations(
        self,
        job_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT generation.*,
                       (
                           SELECT COUNT(*) FROM translation_items AS item
                           WHERE item.generation_id = generation.id
                       ) AS item_count,
                       (
                           SELECT COUNT(*) FROM translation_batches AS batch
                           WHERE batch.generation_id = generation.id
                             AND batch.state = 'completed'
                       ) AS completed_batch_count
                FROM translation_generations AS generation
                WHERE generation.job_id = ?
                ORDER BY generation_number
                """,
                (job_id,),
            ).fetchall()
        return [self._translation_generation_from_row(row) for row in rows]

    def begin_translation_generation_attempt(self, generation_id: str) -> int:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE translation_generations
                SET state = 'running', attempt = attempt + 1,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (time.time(), generation_id),
            )
            row = connection.execute(
                "SELECT attempt FROM translation_generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
        if updated.rowcount != 1 or row is None:
            raise ValueError("translation generation not found")
        return int(row["attempt"])

    def mark_translation_generation(
        self,
        generation_id: str,
        *,
        state: str,
        error: str | None = None,
    ) -> None:
        if state not in {"partial", "paused", "blocked", "failed", "stopped"}:
            raise ValueError("invalid translation generation state")
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE translation_generations
                SET state = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (state, error, time.time(), generation_id),
            )
        if updated.rowcount != 1:
            raise ValueError("translation generation not found")

    def next_translation_batch_index(self, generation_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(batch_index), -1) + 1 AS next_index
                FROM translation_batches
                WHERE generation_id = ?
                """,
                (generation_id,),
            ).fetchone()
        return int(row["next_index"]) if row is not None else 0

    def completed_translation_batch_count(self, generation_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM translation_batches
                WHERE generation_id = ? AND state = 'completed'
                  AND kind = 'remote'
                """,
                (generation_id,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def start_translation_batch(
        self,
        generation_id: str,
        *,
        batch_index: int,
        generation_attempt: int,
        items: Sequence[Mapping[str, Any]],
    ) -> None:
        if batch_index < 0 or generation_attempt < 0:
            raise ValueError("invalid translation batch index or attempt")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            segment_id = str(item.get("id", "")).strip()
            source_hash = str(item.get("source_hash", "")).strip()
            if not segment_id or segment_id in seen or not source_hash:
                raise ValueError("invalid translation batch source item")
            seen.add(segment_id)
            normalized.append(
                {"id": segment_id, "source_hash": source_hash}
            )
        if not normalized:
            raise ValueError("translation batch must contain source items")
        segment_ids = [item["id"] for item in normalized]
        input_hash = _canonical_json_hash(normalized)
        now = time.time()
        with self._connect() as connection:
            generation = connection.execute(
                "SELECT id FROM translation_generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
            if generation is None:
                raise ValueError("translation generation not found")
            connection.execute(
                """
                INSERT INTO translation_batches (
                    generation_id, batch_index, generation_attempt, kind,
                    segment_ids_json, input_hash, output_hash,
                    state, error, created_at, updated_at
                ) VALUES (?, ?, ?, 'remote', ?, ?, '', 'running', NULL, ?, ?)
                ON CONFLICT(generation_id, batch_index) DO UPDATE SET
                    generation_attempt = excluded.generation_attempt,
                    kind = 'remote',
                    segment_ids_json = excluded.segment_ids_json,
                    input_hash = excluded.input_hash,
                    output_hash = '', state = 'running', error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    generation_id,
                    batch_index,
                    generation_attempt,
                    json.dumps(segment_ids, ensure_ascii=False),
                    input_hash,
                    now,
                    now,
                ),
            )

    def fail_translation_batch(
        self,
        generation_id: str,
        *,
        batch_index: int,
        error: str,
    ) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE translation_batches
                SET state = 'failed', error = ?, updated_at = ?
                WHERE generation_id = ? AND batch_index = ?
                """,
                (error[:2000], time.time(), generation_id, batch_index),
            )
        if updated.rowcount != 1:
            raise ValueError("translation batch not found")

    def translation_batches(
        self,
        generation_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT batch_index, generation_attempt, kind,
                       segment_ids_json, input_hash, output_hash,
                       state, error, created_at, updated_at
                FROM translation_batches
                WHERE generation_id = ?
                ORDER BY batch_index
                """,
                (generation_id,),
            ).fetchall()
        return [
            {
                "batch_index": int(row["batch_index"]),
                "generation_attempt": int(row["generation_attempt"]),
                "kind": str(row["kind"]),
                "segment_ids": json.loads(str(row["segment_ids_json"])),
                "input_hash": str(row["input_hash"]),
                "output_hash": str(row["output_hash"]),
                "state": str(row["state"]),
                "error": row["error"],
                "created_at": float(row["created_at"]),
                "updated_at": float(row["updated_at"]),
            }
            for row in rows
        ]

    def save_translation_batch(
        self,
        generation_id: str,
        *,
        batch_index: int,
        generation_attempt: int,
        kind: str,
        items: Sequence[Mapping[str, Any]],
    ) -> None:
        if kind not in {"remote", "legacy", "manual", "final"}:
            raise ValueError("invalid translation batch kind")
        if batch_index < 0 or generation_attempt < 0:
            raise ValueError("invalid translation batch index or attempt")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            segment_id = str(item.get("id", "")).strip()
            text = str(item.get("text", "")).strip()
            source_hash = str(item.get("source_hash", "")).strip()
            segment_index = item.get("segment_index")
            if (
                not segment_id
                or segment_id in seen
                or not text
                or not source_hash
                or not isinstance(segment_index, int)
                or segment_index < 0
            ):
                raise ValueError("invalid translation batch item")
            seen.add(segment_id)
            normalized.append(
                {
                    "id": segment_id,
                    "text": text,
                    "source_hash": source_hash,
                    "segment_index": segment_index,
                }
            )
        if not normalized:
            return
        segment_ids = [item["id"] for item in normalized]
        input_hash = _canonical_json_hash(
            [
                {"id": item["id"], "source_hash": item["source_hash"]}
                for item in normalized
            ]
        )
        output_hash = _canonical_json_hash(
            [{"id": item["id"], "text": item["text"]} for item in normalized]
        )
        now = time.time()
        with self._connect() as connection:
            generation = connection.execute(
                "SELECT id FROM translation_generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
            if generation is None:
                raise ValueError("translation generation not found")
            connection.execute(
                """
                INSERT INTO translation_batches (
                    generation_id, batch_index, generation_attempt, kind,
                    segment_ids_json, input_hash, output_hash,
                    state, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', NULL, ?, ?)
                ON CONFLICT(generation_id, batch_index) DO UPDATE SET
                    generation_attempt = excluded.generation_attempt,
                    kind = excluded.kind,
                    segment_ids_json = excluded.segment_ids_json,
                    input_hash = excluded.input_hash,
                    output_hash = excluded.output_hash,
                    state = 'completed', error = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    generation_id,
                    batch_index,
                    generation_attempt,
                    kind,
                    json.dumps(segment_ids, ensure_ascii=False),
                    input_hash,
                    output_hash,
                    now,
                    now,
                ),
            )
            for item in normalized:
                connection.execute(
                    """
                    INSERT INTO translation_items (
                        generation_id, segment_id, segment_index,
                        source_hash, translated_text, batch_index, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(generation_id, segment_id) DO UPDATE SET
                        segment_index = excluded.segment_index,
                        source_hash = excluded.source_hash,
                        translated_text = excluded.translated_text,
                        batch_index = excluded.batch_index,
                        updated_at = excluded.updated_at
                    """,
                    (
                        generation_id,
                        item["id"],
                        item["segment_index"],
                        item["source_hash"],
                        item["text"],
                        batch_index,
                        now,
                    ),
                )
            connection.execute(
                """
                UPDATE translation_generations
                SET state = 'partial', updated_at = ?
                WHERE id = ?
                """,
                (now, generation_id),
            )

    def translation_items(
        self,
        generation_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT segment_id, segment_index, source_hash,
                       translated_text, batch_index, updated_at
                FROM translation_items
                WHERE generation_id = ?
                ORDER BY segment_index, segment_id
                """,
                (generation_id,),
            ).fetchall()
        return [
            {
                "id": str(row["segment_id"]),
                "text": str(row["translated_text"]),
                "segment_index": int(row["segment_index"]),
                "source_hash": str(row["source_hash"]),
                "batch_index": int(row["batch_index"]),
                "updated_at": float(row["updated_at"]),
            }
            for row in rows
        ]

    def complete_translation_generation(
        self,
        generation_id: str,
        expected_segment_ids: Sequence[str],
    ) -> list[dict[str, str]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT segment_id, translated_text
                FROM translation_items
                WHERE generation_id = ?
                ORDER BY segment_index, segment_id
                """,
                (generation_id,),
            ).fetchall()
            received_ids = [str(row["segment_id"]) for row in rows]
            if received_ids != list(expected_segment_ids):
                raise ValueError(
                    "translation generation items do not match transcript"
                )
            now = time.time()
            updated = connection.execute(
                """
                UPDATE translation_generations
                SET state = 'completed', last_error = NULL,
                    completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, generation_id),
            )
        if updated.rowcount != 1:
            raise ValueError("translation generation not found")
        return [
            {"id": str(row["segment_id"]), "text": str(row["translated_text"])}
            for row in rows
        ]

    def create_subtitle_generation(
        self,
        *,
        generation_id: str,
        job_id: str,
        translation_generation_id: str | None,
        transcript_hash: str,
        translation_hash: str,
        renderer_version: str,
        render_hash: str,
        srt_artifact_path: str,
        ass_artifact_path: str,
        srt_hash: str,
        ass_hash: str,
        origin: str,
    ) -> dict[str, Any]:
        if origin not in {"rendered", "legacy"}:
            raise ValueError("invalid subtitle generation origin")
        now = time.time()
        with self._connect() as connection:
            job = connection.execute(
                "SELECT id FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise ValueError("job not found")
            if translation_generation_id is not None:
                translation_generation = connection.execute(
                    """
                    SELECT id FROM translation_generations
                    WHERE id = ? AND job_id = ?
                    """,
                    (translation_generation_id, job_id),
                ).fetchone()
                if translation_generation is None:
                    raise ValueError("translation generation not found")
            latest = connection.execute(
                """
                SELECT id, generation_number
                FROM subtitle_generations
                WHERE job_id = ?
                ORDER BY generation_number DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            generation_number = (
                int(latest["generation_number"]) + 1
                if latest is not None
                else 1
            )
            supersedes = str(latest["id"]) if latest is not None else None
            connection.execute(
                """
                INSERT INTO subtitle_generations (
                    id, job_id, generation_number,
                    translation_generation_id,
                    transcript_hash, translation_hash,
                    renderer_version, render_hash,
                    srt_artifact_path, ass_artifact_path,
                    srt_hash, ass_hash, origin,
                    supersedes_generation_id, created_at, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    generation_id,
                    job_id,
                    generation_number,
                    translation_generation_id,
                    transcript_hash,
                    translation_hash,
                    renderer_version,
                    render_hash,
                    srt_artifact_path,
                    ass_artifact_path,
                    srt_hash,
                    ass_hash,
                    origin,
                    supersedes,
                    now,
                ),
            )
            created = connection.execute(
                "SELECT * FROM subtitle_generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
        return self._subtitle_generation_from_row(created)

    def get_subtitle_generation(
        self,
        generation_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT generation.*,
                       publication.subtitle_generation_id IS NOT NULL
                           AS is_published
                FROM subtitle_generations AS generation
                LEFT JOIN subtitle_publications AS publication
                  ON publication.subtitle_generation_id = generation.id
                WHERE generation.id = ?
                """,
                (generation_id,),
            ).fetchone()
        return (
            self._subtitle_generation_from_row(row)
            if row is not None
            else None
        )

    def list_subtitle_generations(
        self,
        job_id: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT generation.*,
                       publication.subtitle_generation_id IS NOT NULL
                           AS is_published
                FROM subtitle_generations AS generation
                LEFT JOIN subtitle_publications AS publication
                  ON publication.subtitle_generation_id = generation.id
                WHERE generation.job_id = ?
                ORDER BY generation.generation_number
                """,
                (job_id,),
            ).fetchall()
        return [self._subtitle_generation_from_row(row) for row in rows]

    def published_subtitle_generation(
        self,
        job_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT generation.*, 1 AS is_published
                FROM jobs AS requested_job
                JOIN subtitle_publications AS publication
                  ON publication.source_rel = requested_job.source_rel
                JOIN subtitle_generations AS generation
                  ON generation.id = publication.subtitle_generation_id
                WHERE requested_job.id = ?
                """,
                (job_id,),
            ).fetchone()
        return (
            self._subtitle_generation_from_row(row)
            if row is not None
            else None
        )

    def list_subtitle_publications(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT generation.*, 1 AS is_published,
                       publication.source_rel AS publication_source_rel
                FROM subtitle_publications AS publication
                JOIN subtitle_generations AS generation
                  ON generation.id = publication.subtitle_generation_id
                ORDER BY publication.source_rel
                """
            ).fetchall()
        publications: list[dict[str, Any]] = []
        for row in rows:
            publication = self._subtitle_generation_from_row(row)
            publication["source_rel"] = str(row["publication_source_rel"])
            publications.append(publication)
        return publications

    def publish_subtitle_generation(
        self,
        generation_id: str,
        *,
        srt_path: str,
        ass_path: str,
        lease_owner: str | None = None,
        lease_token: int | None = None,
    ) -> dict[str, Any]:
        if (lease_owner is None) != (lease_token is None):
            raise ValueError("lease owner and token must be provided together")
        now = time.time()
        with self._connect() as connection:
            generation = connection.execute(
                """
                SELECT generation.*, job.source_rel
                FROM subtitle_generations AS generation
                JOIN jobs AS job ON job.id = generation.job_id
                WHERE generation.id = ?
                """,
                (generation_id,),
            ).fetchone()
            if generation is None:
                raise ValueError("subtitle generation not found")
            job_id = str(generation["job_id"])
            source_rel = str(generation["source_rel"])
            if lease_owner is not None and lease_token is not None:
                owned = connection.execute(
                    """
                    UPDATE jobs
                    SET lease_expires_at = lease_expires_at
                    WHERE id = ? AND status = 'rendering'
                      AND lease_owner = ? AND lease_token = ?
                      AND lease_expires_at > ?
                    """,
                    (job_id, lease_owner, lease_token, now),
                )
                if owned.rowcount != 1:
                    raise WorkerLeaseLost("worker lease was superseded")
            connection.execute(
                """
                INSERT INTO subtitle_publications (
                    source_rel, job_id, subtitle_generation_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(source_rel) DO UPDATE SET
                    job_id = excluded.job_id,
                    subtitle_generation_id = excluded.subtitle_generation_id,
                    updated_at = excluded.updated_at
                """,
                (source_rel, job_id, generation_id, now),
            )
            connection.execute(
                """
                UPDATE subtitle_generations
                SET published_at = ?
                WHERE id = ?
                """,
                (now, generation_id),
            )
            connection.execute(
                """
                UPDATE jobs
                SET status = 'completed', srt_path = ?, ass_path = ?,
                    blocked_stage = NULL, error = NULL,
                    lease_owner = NULL, lease_expires_at = NULL,
                    status_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (srt_path, ass_path, now, now, job_id),
            )
            published = connection.execute(
                """
                SELECT generation.*, 1 AS is_published
                FROM subtitle_generations AS generation
                WHERE generation.id = ?
                """,
                (generation_id,),
            ).fetchone()
        self._notify_change(job_id)
        return self._subtitle_generation_from_row(published)

    @staticmethod
    def _translation_generation_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "id": str(row["id"]),
            "job_id": str(row["job_id"]),
            "generation_number": int(row["generation_number"]),
            "transcript_job_id": str(row["transcript_job_id"]),
            "transcript_hash": str(row["transcript_hash"]),
            "prompt_hash": str(row["prompt_hash"]),
            "endpoint_key": str(row["endpoint_key"]),
            "model": str(row["model"]),
            "config_hash": str(row["config_hash"]),
            "state": str(row["state"]),
            "attempt": int(row["attempt"]),
            "origin": str(row["origin"]),
            "supersedes_generation_id": (
                str(row["supersedes_generation_id"])
                if row["supersedes_generation_id"] is not None
                else None
            ),
            "artifact_path": str(row["artifact_path"]),
            "last_error": (
                str(row["last_error"]) if row["last_error"] is not None else None
            ),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "completed_at": (
                float(row["completed_at"])
                if row["completed_at"] is not None
                else None
            ),
        }
        if "item_count" in row.keys():
            result["item_count"] = int(row["item_count"])
        if "completed_batch_count" in row.keys():
            result["completed_batch_count"] = int(row["completed_batch_count"])
        return result

    @staticmethod
    def _subtitle_generation_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "id": str(row["id"]),
            "job_id": str(row["job_id"]),
            "generation_number": int(row["generation_number"]),
            "translation_generation_id": (
                str(row["translation_generation_id"])
                if row["translation_generation_id"] is not None
                else None
            ),
            "transcript_hash": str(row["transcript_hash"]),
            "translation_hash": str(row["translation_hash"]),
            "renderer_version": str(row["renderer_version"]),
            "render_hash": str(row["render_hash"]),
            "srt_artifact_path": str(row["srt_artifact_path"]),
            "ass_artifact_path": str(row["ass_artifact_path"]),
            "srt_hash": str(row["srt_hash"]),
            "ass_hash": str(row["ass_hash"]),
            "origin": str(row["origin"]),
            "supersedes_generation_id": (
                str(row["supersedes_generation_id"])
                if row["supersedes_generation_id"] is not None
                else None
            ),
            "created_at": float(row["created_at"]),
            "published_at": (
                float(row["published_at"])
                if row["published_at"] is not None
                else None
            ),
        }
        if "is_published" in row.keys():
            result["is_published"] = bool(row["is_published"])
        return result

    def save_subtitle_validation(
        self,
        *,
        job_id: str,
        source_rel: str,
        external_path: str,
        external_hash: str,
        candidate_path: str,
        candidate_hash: str,
        metrics: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = time.time()
        validation_id = uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO subtitle_validations (
                    id, job_id, source_rel,
                    external_path, external_hash,
                    candidate_path, candidate_hash,
                    metrics_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, external_hash, candidate_hash)
                DO UPDATE SET
                    external_path = excluded.external_path,
                    candidate_path = excluded.candidate_path,
                    metrics_json = excluded.metrics_json,
                    updated_at = excluded.updated_at
                """,
                (
                    validation_id,
                    job_id,
                    source_rel,
                    external_path,
                    external_hash,
                    candidate_path,
                    candidate_hash,
                    json.dumps(dict(metrics), ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM subtitle_validations
                WHERE job_id = ? AND external_hash = ? AND candidate_hash = ?
                """,
                (job_id, external_hash, candidate_hash),
            ).fetchone()
        validation = self._subtitle_validation_from_row(row)
        if validation is None:
            raise RuntimeError("subtitle validation could not be read")
        return validation

    def latest_subtitle_validation(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM subtitle_validations
                WHERE job_id = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
        return self._subtitle_validation_from_row(row)

    def get_subtitle_validation(
        self,
        *,
        job_id: str,
        external_hash: str,
        candidate_hash: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM subtitle_validations
                WHERE job_id = ? AND external_hash = ? AND candidate_hash = ?
                """,
                (job_id, external_hash, candidate_hash),
            ).fetchone()
        return self._subtitle_validation_from_row(row)

    def get_subtitle_validation_by_id(
        self,
        validation_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM subtitle_validations WHERE id = ?",
                (validation_id,),
            ).fetchone()
        return self._subtitle_validation_from_row(row)

    def save_subtitle_llm_validation(
        self,
        validation_id: str,
        *,
        result: Mapping[str, Any],
        model: str,
        input_hash: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE subtitle_validations
                SET llm_json = ?, validator_model = ?,
                    validator_input_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(dict(result), ensure_ascii=False, sort_keys=True),
                    model,
                    input_hash,
                    time.time(),
                    validation_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM subtitle_validations WHERE id = ?",
                (validation_id,),
            ).fetchone()
        if updated.rowcount != 1:
            raise ValueError("subtitle validation not found")
        validation = self._subtitle_validation_from_row(row)
        if validation is None:
            raise RuntimeError("subtitle validation could not be read")
        return validation

    @staticmethod
    def _subtitle_validation_from_row(
        row: sqlite3.Row | None,
    ) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "job_id": str(row["job_id"]),
            "source_rel": str(row["source_rel"]),
            "external_path": str(row["external_path"]),
            "external_hash": str(row["external_hash"]),
            "candidate_path": str(row["candidate_path"]),
            "candidate_hash": str(row["candidate_hash"]),
            "metrics": json.loads(str(row["metrics_json"])),
            "llm": (
                json.loads(str(row["llm_json"]))
                if row["llm_json"] is not None
                else None
            ),
            "validator_model": (
                str(row["validator_model"])
                if row["validator_model"] is not None
                else None
            ),
            "validator_input_hash": (
                str(row["validator_input_hash"])
                if row["validator_input_hash"] is not None
                else None
            ),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
        }

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
