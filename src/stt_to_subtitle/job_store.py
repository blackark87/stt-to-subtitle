"""SQLite job and event persistence for the web orchestrator."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Collection, Iterator, Mapping, Sequence
from uuid import uuid4

from .artifact_retention import ArtifactReference
from .db_migrations import Migration, execute_sql_statements, run_migrations
from .path_display import (
    PathDisplayRule,
    normalize_path_display_patterns,
)
from .job_state import JobPhase, JobReason, JobState, structured_state_from_legacy
from .storage_paths import rebase_stored_path
from .transcription_progress import TRANSCRIPTION_STAGE_LABELS
from .translation_prompt import (
    BUILTIN_PROMPT_PAIRS,
    KOREAN_JAV_DRAFT_PROMPT,
    KOREAN_JAV_REVIEW_PROMPT,
    KOREAN_VARIETY_DRAFT_PROMPT,
    KOREAN_VARIETY_REVIEW_PROMPT,
    LEGACY_BUILTIN_PROMPT_PAIR_HASHES,
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
JOB_OPERATIONS = {"extract", "transcribe", "translate", "full"}
JOB_STATUSES = {
    "queued",
    "extracting",
    "audio_ready",
    "transcription_running",
    "transcribed",
    "translation_running",
    "translated",
    "rendering",
    "audio_completed",
    "transcription_completed",
    "translation_paused",
    "blocked",
    "failed",
    "completed",
}
NONNEGATIVE_JOB_FIELDS = {
    "chunks_created",
    "chunks_completed",
    "chunks_total_estimate",
    "transcription_stage_index",
    "transcription_stage_total",
    "translation_chunks_total",
    "translation_chunks_completed",
    "lease_token",
}
BOOLEAN_JOB_FIELDS = {
    "force_overwrite",
    "translation_pause_requested",
    "job_stop_requested",
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
TRANSLATION_RESTART_INTERRUPTED = (
    "service restart interrupted translation attempt"
)
TRANSLATION_BATCH_RESTART_INTERRUPTED = (
    "service restart interrupted translation batch"
)
EVENT_CODE_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
EVENT_PAYLOAD_MAX_BYTES = 16 * 1024
SENSITIVE_EVENT_KEY_PARTS = frozenset(
    {
        "authorization",
        "credential",
        "password",
        "secret",
        "token",
        "api_key",
        "apikey",
    }
)
LOGGER = logging.getLogger(__name__)
STAGE_DURATION_OUTCOMES = {
    "stage.completed": "completed",
    "stage.blocked": "blocked",
    "stage.failed": "failed",
    "stage.paused": "paused",
    "job.stopped": "stopped",
    "transcription.runtime_failover": "runtime_failover",
}


def media_duration_bucket_minutes(duration_seconds: float | None) -> int | None:
    """Return the nearest 15-minute nominal media-length bucket."""

    if duration_seconds is None:
        return None
    duration = float(duration_seconds)
    if not math.isfinite(duration) or duration <= 0:
        return None
    return max(15, int(math.floor(duration / (15 * 60) + 0.5)) * 15)


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
    prompt_revision_id: str
    prompt_revision_number: int
    archived: bool
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class RuntimeEndpoint:
    id: str
    name: str
    base_url: str
    token: str
    enabled: bool
    capacity: int
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
    audio_revision_id: str | None
    stt_job_id: str | None
    stt_runtime_id: str | None
    transcript_path: str | None
    transcript_revision_id: str | None
    translation_path: str | None
    srt_path: str | None
    ass_path: str | None
    blocked_stage: str | None
    error: str | None
    chunks_created: int
    chunks_completed: int
    chunks_total_estimate: int
    chunk_progress_every: int
    transcription_stage: str | None
    transcription_stage_index: int
    transcription_stage_total: int
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
        "audio_revision_id",
        "stt_job_id",
        "stt_runtime_id",
        "transcript_path",
        "transcript_revision_id",
        "translation_path",
        "srt_path",
        "ass_path",
        "blocked_stage",
        "error",
        "chunks_created",
        "chunks_completed",
        "chunks_total_estimate",
        "chunk_progress_every",
        "transcription_stage",
        "transcription_stage_index",
        "transcription_stage_total",
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
            for table in ("audio_revisions", "transcript_revisions"):
                revision_rows = connection.execute(
                    f"SELECT id, artifact_path FROM {table}"
                ).fetchall()
                for row in revision_rows:
                    original = str(row["artifact_path"])
                    rebased = rebase_stored_path(
                        original,
                        previous_root=previous_root,
                        current_root=current_root,
                    )
                    if rebased == original:
                        continue
                    connection.execute(
                        f"UPDATE {table} SET artifact_path = ? WHERE id = ?",
                        (rebased, str(row["id"])),
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

    def artifact_references(self) -> list[ArtifactReference]:
        """Return every persisted path that must survive orphan cleanup."""
        references: list[ArtifactReference] = []

        def append_reference(
            path: object,
            *,
            kind: str,
            record_id: object,
            expected: bool = True,
        ) -> None:
            if path is None or not str(path).strip():
                return
            references.append(
                ArtifactReference(
                    path=str(path),
                    kind=kind,
                    record_id=str(record_id),
                    expected=expected,
                )
            )

        with self._connect() as connection:
            for row in connection.execute(
                """
                SELECT id, audio_path, transcript_path, translation_path,
                       srt_path, ass_path
                FROM jobs
                """
            ).fetchall():
                for field in (
                    "audio_path",
                    "transcript_path",
                    "translation_path",
                    "srt_path",
                    "ass_path",
                ):
                    append_reference(
                        row[field],
                        kind=f"job.{field}",
                        record_id=row["id"],
                    )
            for table, kind in (
                ("audio_revisions", "audio_revision"),
                ("transcript_revisions", "transcript_revision"),
            ):
                for row in connection.execute(
                    f"SELECT id, artifact_path FROM {table}"
                ).fetchall():
                    append_reference(
                        row["artifact_path"],
                        kind=kind,
                        record_id=row["id"],
                    )
            for row in connection.execute(
                """
                SELECT id, state, artifact_path
                FROM translation_generations
                """
            ).fetchall():
                append_reference(
                    row["artifact_path"],
                    kind="translation_generation",
                    record_id=row["id"],
                    expected=str(row["state"]) == "completed",
                )
            for row in connection.execute(
                """
                SELECT id, srt_artifact_path, ass_artifact_path
                FROM subtitle_generations
                """
            ).fetchall():
                append_reference(
                    row["srt_artifact_path"],
                    kind="subtitle_generation.srt",
                    record_id=row["id"],
                )
                append_reference(
                    row["ass_artifact_path"],
                    kind="subtitle_generation.ass",
                    record_id=row["id"],
                )
            for row in connection.execute(
                """
                SELECT id, external_path, candidate_path
                FROM subtitle_validations
                """
            ).fetchall():
                append_reference(
                    row["external_path"],
                    kind="subtitle_validation.external",
                    record_id=row["id"],
                )
                append_reference(
                    row["candidate_path"],
                    kind="subtitle_validation.candidate",
                    record_id=row["id"],
                )
        return references

    def _notify_change(self, job_id: str) -> None:
        if self._change_hook is not None:
            self._change_hook(job_id)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            connection.close()
            raise RuntimeError("SQLite foreign key enforcement is unavailable")
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
                    audio_revision_id TEXT,
                    stt_job_id TEXT,
                    stt_runtime_id TEXT,
                    transcript_path TEXT,
                    transcript_revision_id TEXT,
                    translation_path TEXT,
                    srt_path TEXT,
                    ass_path TEXT,
                    blocked_stage TEXT,
                    error TEXT,
                    chunks_created INTEGER NOT NULL DEFAULT 0,
                    chunks_completed INTEGER NOT NULL DEFAULT 0,
                    chunks_total_estimate INTEGER NOT NULL DEFAULT 0,
                    chunk_progress_every INTEGER NOT NULL DEFAULT 10,
                    transcription_stage TEXT,
                    transcription_stage_index INTEGER NOT NULL DEFAULT 0,
                    transcription_stage_total INTEGER NOT NULL DEFAULT 0,
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
                    event_code TEXT NOT NULL DEFAULT 'job.message',
                    from_state TEXT,
                    to_state TEXT,
                    phase TEXT,
                    attempt INTEGER,
                    correlation_id TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(id),
                    CHECK (from_state IS NULL OR from_state IN (
                        'waiting', 'running', 'paused', 'blocked',
                        'stopped', 'failed', 'done'
                    )),
                    CHECK (to_state IS NULL OR to_state IN (
                        'waiting', 'running', 'paused', 'blocked',
                        'stopped', 'failed', 'done'
                    )),
                    CHECK (phase IS NULL OR phase IN (
                        'extraction', 'transcription', 'translation',
                        'render', 'complete'
                    )),
                    CHECK (attempt IS NULL OR attempt >= 1)
                );

                CREATE TABLE IF NOT EXISTS builtin_runtime_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    stt_base_url TEXT NOT NULL,
                    stt_token TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runtime_endpoints (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL UNIQUE,
                    token TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    capacity INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    CHECK (enabled IN (0, 1)),
                    CHECK (capacity BETWEEN 1 AND 8)
                );

                CREATE TABLE IF NOT EXISTS runtime_batch_settings (
                    runtime_id TEXT PRIMARY KEY,
                    kotoba_batch_size INTEGER,
                    whisperx_batch_size INTEGER,
                    updated_at REAL NOT NULL,
                    CHECK (
                        kotoba_batch_size IS NULL
                        OR kotoba_batch_size BETWEEN 1 AND 64
                    ),
                    CHECK (
                        whisperx_batch_size IS NULL
                        OR whisperx_batch_size BETWEEN 1 AND 64
                    ),
                    CHECK (
                        kotoba_batch_size IS NOT NULL
                        OR whisperx_batch_size IS NOT NULL
                    )
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
                    provider TEXT NOT NULL DEFAULT 'openai_compatible',
                    base_url TEXT NOT NULL,
                    token TEXT NOT NULL,
                    model TEXT NOT NULL,
                    region TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prompt_categories (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    translation_prompt TEXT NOT NULL,
                    review_prompt TEXT NOT NULL,
                    active_revision_id TEXT,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prompt_revisions (
                    id TEXT PRIMARY KEY,
                    category_id TEXT NOT NULL,
                    revision_number INTEGER NOT NULL,
                    translation_prompt TEXT NOT NULL,
                    review_prompt TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (category_id) REFERENCES prompt_categories(id),
                    UNIQUE (category_id, revision_number)
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
                    validator_provider TEXT,
                    validator_model TEXT,
                    validator_input_hash TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(id),
                    UNIQUE (job_id, external_hash, candidate_hash)
                );

                CREATE TABLE IF NOT EXISTS audio_revisions (
                    id TEXT PRIMARY KEY,
                    created_by_job_id TEXT NOT NULL,
                    source_rel TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    extraction_hash TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    duration_seconds REAL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transcript_revisions (
                    id TEXT PRIMARY KEY,
                    created_by_job_id TEXT NOT NULL,
                    audio_revision_id TEXT,
                    remote_job_id TEXT,
                    backend TEXT NOT NULL,
                    model_revision TEXT NOT NULL,
                    options_hash TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    chunks_total INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (audio_revision_id)
                        REFERENCES audio_revisions(id)
                );

                CREATE TABLE IF NOT EXISTS translation_generations (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    generation_number INTEGER NOT NULL,
                    transcript_job_id TEXT NOT NULL,
                    transcript_revision_id TEXT,
                    transcript_hash TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    prompt_revision_id TEXT,
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
                    FOREIGN KEY (transcript_revision_id)
                        REFERENCES transcript_revisions(id),
                    FOREIGN KEY (prompt_revision_id)
                        REFERENCES prompt_revisions(id),
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
                    sequence INTEGER,
                    applied_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS operational_measurements (
                    metric TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    total REAL NOT NULL,
                    maximum REAL NOT NULL,
                    last_value REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (metric, labels_json),
                    CHECK (sample_count >= 1)
                );

                CREATE INDEX IF NOT EXISTS jobs_status_idx
                    ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS job_events_job_idx
                    ON job_events(job_id, id);
                CREATE INDEX IF NOT EXISTS subtitle_validations_job_idx
                    ON subtitle_validations(job_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS audio_revisions_lookup_idx
                    ON audio_revisions(
                        source_rel, source_hash, extraction_hash, created_at DESC
                    );
                CREATE INDEX IF NOT EXISTS transcript_revisions_job_idx
                    ON transcript_revisions(created_by_job_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS prompt_revisions_category_idx
                    ON prompt_revisions(category_id, revision_number DESC);
                CREATE INDEX IF NOT EXISTS translation_generations_job_idx
                    ON translation_generations(job_id, generation_number DESC);
                CREATE INDEX IF NOT EXISTS translation_items_generation_idx
                    ON translation_items(generation_id, segment_index);
                CREATE INDEX IF NOT EXISTS subtitle_generations_job_idx
                    ON subtitle_generations(job_id, generation_number DESC);
                """
            )
            self._run_schema_migrations(connection)
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
                        KOREAN_JAV_DRAFT_PROMPT,
                        KOREAN_JAV_REVIEW_PROMPT,
                        now,
                        now,
                    ),
                    (
                        "variety",
                        "버라이어티",
                        KOREAN_VARIETY_DRAFT_PROMPT,
                        KOREAN_VARIETY_REVIEW_PROMPT,
                        now,
                        now,
                    ),
                ),
            )
            prompt_rows = connection.execute(
                """
                SELECT id, translation_prompt, review_prompt
                FROM prompt_categories
                WHERE active_revision_id IS NULL
                """
            ).fetchall()
            for row in prompt_rows:
                revision_id = uuid4().hex
                translation_prompt = str(row["translation_prompt"])
                review_prompt = str(row["review_prompt"])
                connection.execute(
                    """
                    INSERT INTO prompt_revisions (
                        id, category_id, revision_number,
                        translation_prompt, review_prompt,
                        content_hash, created_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        str(row["id"]),
                        translation_prompt,
                        review_prompt,
                        _canonical_json_hash(
                            {
                                "translation_prompt": translation_prompt,
                                "review_prompt": review_prompt,
                            }
                        ),
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE prompt_categories
                    SET active_revision_id = ?
                    WHERE id = ?
                    """,
                    (revision_id, str(row["id"])),
                )
            self._install_integrity_triggers(connection)
            self._assert_foreign_key_integrity(connection)

    @staticmethod
    def _migrate_legacy_schema_columns(
        connection: sqlite3.Connection,
    ) -> None:
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
            "transcription_stage": (
                "ALTER TABLE jobs ADD COLUMN transcription_stage TEXT"
            ),
            "transcription_stage_index": (
                "ALTER TABLE jobs ADD COLUMN transcription_stage_index "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "transcription_stage_total": (
                "ALTER TABLE jobs ADD COLUMN transcription_stage_total "
                "INTEGER NOT NULL DEFAULT 0"
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
            "reason_code": "ALTER TABLE jobs ADD COLUMN reason_code TEXT",
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
            "lease_owner": "ALTER TABLE jobs ADD COLUMN lease_owner TEXT",
            "lease_expires_at": (
                "ALTER TABLE jobs ADD COLUMN lease_expires_at REAL"
            ),
            "lease_token": (
                "ALTER TABLE jobs ADD COLUMN "
                "lease_token INTEGER NOT NULL DEFAULT 0"
            ),
            "audio_revision_id": (
                "ALTER TABLE jobs ADD COLUMN audio_revision_id TEXT"
            ),
            "transcript_revision_id": (
                "ALTER TABLE jobs ADD COLUMN transcript_revision_id TEXT"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

        translation_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(translation_generations)"
            ).fetchall()
        }
        if "transcript_revision_id" not in translation_columns:
            connection.execute(
                "ALTER TABLE translation_generations "
                "ADD COLUMN transcript_revision_id TEXT"
            )
        if "prompt_revision_id" not in translation_columns:
            connection.execute(
                "ALTER TABLE translation_generations "
                "ADD COLUMN prompt_revision_id TEXT"
            )
        transcript_revision_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(transcript_revisions)"
            ).fetchall()
        }
        if "chunks_total" not in transcript_revision_columns:
            connection.execute(
                "ALTER TABLE transcript_revisions ADD COLUMN "
                "chunks_total INTEGER NOT NULL DEFAULT 0"
            )

        event_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(job_events)"
            ).fetchall()
        }
        event_migrations = {
            "event_code": (
                "ALTER TABLE job_events ADD COLUMN event_code "
                "TEXT NOT NULL DEFAULT 'job.message'"
            ),
            "from_state": (
                "ALTER TABLE job_events ADD COLUMN from_state TEXT"
            ),
            "to_state": "ALTER TABLE job_events ADD COLUMN to_state TEXT",
            "phase": "ALTER TABLE job_events ADD COLUMN phase TEXT",
            "attempt": "ALTER TABLE job_events ADD COLUMN attempt INTEGER",
            "correlation_id": (
                "ALTER TABLE job_events ADD COLUMN correlation_id TEXT"
            ),
            "payload_json": (
                "ALTER TABLE job_events ADD COLUMN payload_json "
                "TEXT NOT NULL DEFAULT '{}'"
            ),
        }
        for column, statement in event_migrations.items():
            if column not in event_columns:
                connection.execute(statement)

        prompt_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(prompt_categories)"
            ).fetchall()
        }
        if "active_revision_id" not in prompt_columns:
            connection.execute(
                "ALTER TABLE prompt_categories "
                "ADD COLUMN active_revision_id TEXT"
            )
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
        connection.execute(
            "CREATE INDEX IF NOT EXISTS job_events_code_idx "
            "ON job_events(event_code, created_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS job_events_created_idx "
            "ON job_events(created_at, id)"
        )
        JobStore._migrate_runtime_settings_split(connection)

    @staticmethod
    def _migrate_structured_job_state(
        connection: sqlite3.Connection,
    ) -> None:
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

    def _run_schema_migrations(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[str, ...]:
        return run_migrations(
            connection,
            (
                Migration(
                    5,
                    "legacy_schema_columns_v1",
                    self._migrate_legacy_schema_columns,
                ),
                Migration(
                    10,
                    "structured_job_state_v1",
                    self._migrate_structured_job_state,
                ),
                Migration(
                    20,
                    "referential_integrity_v1",
                    self._repair_referential_integrity,
                ),
                Migration(
                    30,
                    "default_path_display_rule_v1",
                    self._seed_default_path_display_rule,
                ),
                Migration(
                    40,
                    "correct_default_path_display_rule_v2",
                    self._correct_default_path_display_rule,
                ),
                Migration(
                    50,
                    "subtitle_validator_providers_v1",
                    self._migrate_subtitle_validator_providers,
                ),
                Migration(
                    51,
                    "transcription_stage_progress_v1",
                    self._migrate_transcription_stage_progress,
                ),
                Migration(
                    52,
                    "transcript_segment_counts_v1",
                    self._migrate_transcript_segment_counts,
                ),
                Migration(
                    53,
                    "runtime_pool_v1",
                    self._migrate_runtime_pool,
                ),
                Migration(
                    54,
                    "runtime_batch_settings_v1",
                    self._migrate_runtime_batch_settings,
                ),
                Migration(
                    55,
                    "runtime_settings_split_v1",
                    self._migrate_runtime_settings_split,
                ),
                Migration(
                    56,
                    "builtin_translation_prompts_v2",
                    self._upgrade_builtin_translation_prompts,
                ),
                Migration(
                    57,
                    "builtin_translation_prompts_v3",
                    self._upgrade_builtin_translation_prompts,
                ),
            ),
        )

    @staticmethod
    def _upgrade_builtin_translation_prompts(
        connection: sqlite3.Connection,
    ) -> None:
        """Append the latest built-in pair only when it is still untouched."""

        now = time.time()
        for category_id, (draft_prompt, review_prompt) in (
            BUILTIN_PROMPT_PAIRS.items()
        ):
            row = connection.execute(
                """
                SELECT
                    category.translation_prompt,
                    category.review_prompt,
                    category.active_revision_id,
                    revision.translation_prompt AS revision_translation_prompt,
                    revision.review_prompt AS revision_review_prompt,
                    revision.content_hash AS revision_content_hash
                FROM prompt_categories AS category
                LEFT JOIN prompt_revisions AS revision
                  ON revision.id = category.active_revision_id
                WHERE category.id = ?
                """,
                (category_id,),
            ).fetchone()
            if row is None:
                continue
            current_pair = {
                "translation_prompt": str(row["translation_prompt"]),
                "review_prompt": str(row["review_prompt"]),
            }
            current_hash = _canonical_json_hash(current_pair)
            active_revision_id = row["active_revision_id"]
            if active_revision_id is not None:
                revision_pair = {
                    "translation_prompt": str(
                        row["revision_translation_prompt"]
                    ),
                    "review_prompt": str(row["revision_review_prompt"]),
                }
                if revision_pair != current_pair or str(
                    row["revision_content_hash"]
                ) not in LEGACY_BUILTIN_PROMPT_PAIR_HASHES[category_id]:
                    continue
            elif current_hash not in LEGACY_BUILTIN_PROMPT_PAIR_HASHES[
                category_id
            ]:
                continue
            latest = connection.execute(
                """
                SELECT COALESCE(MAX(revision_number), 0) AS latest
                FROM prompt_revisions
                WHERE category_id = ?
                """,
                (category_id,),
            ).fetchone()
            revision_number = int(latest["latest"])
            if active_revision_id is None:
                legacy_revision_id = uuid4().hex
                revision_number += 1
                connection.execute(
                    """
                    INSERT INTO prompt_revisions (
                        id, category_id, revision_number,
                        translation_prompt, review_prompt,
                        content_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        legacy_revision_id,
                        category_id,
                        revision_number,
                        current_pair["translation_prompt"],
                        current_pair["review_prompt"],
                        current_hash,
                        now,
                    ),
                )
            new_pair = {
                "translation_prompt": draft_prompt,
                "review_prompt": review_prompt,
            }
            revision_id = uuid4().hex
            revision_number += 1
            connection.execute(
                """
                INSERT INTO prompt_revisions (
                    id, category_id, revision_number,
                    translation_prompt, review_prompt,
                    content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    category_id,
                    revision_number,
                    draft_prompt,
                    review_prompt,
                    _canonical_json_hash(new_pair),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE prompt_categories
                SET translation_prompt = ?, review_prompt = ?,
                    active_revision_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    draft_prompt,
                    review_prompt,
                    revision_id,
                    now,
                    category_id,
                ),
            )

    @staticmethod
    def _migrate_runtime_settings_split(
        connection: sqlite3.Connection,
    ) -> None:
        legacy_server_table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'remote_server_settings'"
        ).fetchone()
        if legacy_server_table is None:
            return
        connection.execute(
            """
            INSERT OR IGNORE INTO builtin_runtime_settings (
                id, stt_base_url, stt_token, updated_at
            )
            SELECT id, stt_base_url, stt_token, updated_at
            FROM remote_server_settings
            """
        )
        connection.execute("DROP TABLE remote_server_settings")

    @staticmethod
    def _migrate_runtime_batch_settings(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_batch_settings (
                runtime_id TEXT PRIMARY KEY,
                kotoba_batch_size INTEGER,
                whisperx_batch_size INTEGER,
                updated_at REAL NOT NULL,
                CHECK (
                    kotoba_batch_size IS NULL
                    OR kotoba_batch_size BETWEEN 1 AND 64
                ),
                CHECK (
                    whisperx_batch_size IS NULL
                    OR whisperx_batch_size BETWEEN 1 AND 64
                ),
                CHECK (
                    kotoba_batch_size IS NOT NULL
                    OR whisperx_batch_size IS NOT NULL
                )
            )
            """
        )

    @staticmethod
    def _migrate_runtime_pool(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "stt_runtime_id" not in columns:
            connection.execute(
                "ALTER TABLE jobs ADD COLUMN stt_runtime_id TEXT"
            )
        connection.execute(
            "UPDATE jobs SET stt_runtime_id = 'builtin' "
            "WHERE stt_job_id IS NOT NULL AND stt_runtime_id IS NULL"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS jobs_stt_runtime_idx "
            "ON jobs(stt_runtime_id, status)"
        )

    @staticmethod
    def _migrate_transcript_segment_counts(
        connection: sqlite3.Connection,
    ) -> None:
        rows = connection.execute(
            """
            SELECT id, created_by_job_id, artifact_path
            FROM transcript_revisions
            """
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(
                    Path(str(row["artifact_path"])).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            segments = payload.get("segments")
            if not isinstance(segments, list):
                continue
            segment_count = len(segments)
            revision_id = str(row["id"])
            job_id = str(row["created_by_job_id"])
            connection.execute(
                "UPDATE transcript_revisions "
                "SET chunks_total = ? WHERE id = ?",
                (segment_count, revision_id),
            )
            connection.execute(
                """
                UPDATE jobs
                SET chunks_created = ?, chunks_completed = ?,
                    chunks_total_estimate = ?
                WHERE id = ? AND transcript_revision_id = ?
                """,
                (
                    segment_count,
                    segment_count,
                    segment_count,
                    job_id,
                    revision_id,
                ),
            )

    @staticmethod
    def _migrate_transcription_stage_progress(
        connection: sqlite3.Connection,
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
        }
        migrations = {
            "transcription_stage": (
                "ALTER TABLE jobs ADD COLUMN transcription_stage TEXT"
            ),
            "transcription_stage_index": (
                "ALTER TABLE jobs ADD COLUMN transcription_stage_index "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "transcription_stage_total": (
                "ALTER TABLE jobs ADD COLUMN transcription_stage_total "
                "INTEGER NOT NULL DEFAULT 0"
            ),
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)

    @staticmethod
    def _migrate_subtitle_validator_providers(
        connection: sqlite3.Connection,
    ) -> None:
        settings_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(subtitle_validator_settings)"
            ).fetchall()
        }
        if "provider" not in settings_columns:
            connection.execute(
                "ALTER TABLE subtitle_validator_settings ADD COLUMN "
                "provider TEXT NOT NULL DEFAULT 'openai_compatible'"
            )
        if "region" not in settings_columns:
            connection.execute(
                "ALTER TABLE subtitle_validator_settings ADD COLUMN "
                "region TEXT NOT NULL DEFAULT ''"
            )
        validation_columns = {
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(subtitle_validations)"
            ).fetchall()
        }
        if "validator_provider" not in validation_columns:
            connection.execute(
                "ALTER TABLE subtitle_validations ADD COLUMN "
                "validator_provider TEXT"
            )

    @staticmethod
    def _seed_default_path_display_rule(
        connection: sqlite3.Connection,
    ) -> None:
        now = time.time()
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

    @staticmethod
    def _correct_default_path_display_rule(
        connection: sqlite3.Connection,
    ) -> None:
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
                SET source_pattern = ?, display_pattern = ?, updated_at = ?
                WHERE id = ? AND source_pattern = ?
                  AND display_pattern = ?
                """,
                (
                    DEFAULT_PATH_DISPLAY_SOURCE,
                    DEFAULT_PATH_DISPLAY_TARGET,
                    time.time(),
                    DEFAULT_PATH_DISPLAY_RULE_ID,
                    LEGACY_PATH_DISPLAY_SOURCE,
                    LEGACY_PATH_DISPLAY_TARGET,
                ),
            )
            return
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

    @staticmethod
    def _repair_referential_integrity(
        connection: sqlite3.Connection,
    ) -> None:
        execute_sql_statements(
            connection,
            """
            UPDATE jobs
            SET audio_revision_id = NULL
            WHERE audio_revision_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM audio_revisions
                  WHERE audio_revisions.id = jobs.audio_revision_id
              );

            UPDATE jobs
            SET transcript_revision_id = NULL
            WHERE transcript_revision_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM transcript_revisions
                  WHERE transcript_revisions.id = jobs.transcript_revision_id
              );

            DELETE FROM job_events
            WHERE NOT EXISTS (
                SELECT 1 FROM jobs WHERE jobs.id = job_events.job_id
            );

            DELETE FROM subtitle_validations
            WHERE NOT EXISTS (
                SELECT 1 FROM jobs
                WHERE jobs.id = subtitle_validations.job_id
            );

            DELETE FROM subtitle_publications
            WHERE NOT EXISTS (
                SELECT 1 FROM jobs
                WHERE jobs.id = subtitle_publications.job_id
            ) OR NOT EXISTS (
                SELECT 1 FROM subtitle_generations
                WHERE subtitle_generations.id =
                    subtitle_publications.subtitle_generation_id
                  AND subtitle_generations.job_id =
                    subtitle_publications.job_id
            ) OR NOT EXISTS (
                SELECT 1 FROM jobs
                WHERE jobs.id = subtitle_publications.job_id
                  AND jobs.source_rel = subtitle_publications.source_rel
            );

            UPDATE subtitle_generations
            SET translation_generation_id = NULL
            WHERE translation_generation_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM translation_generations
                  WHERE translation_generations.id =
                      subtitle_generations.translation_generation_id
                    AND translation_generations.job_id =
                      subtitle_generations.job_id
              );

            UPDATE subtitle_generations
            SET supersedes_generation_id = NULL
            WHERE supersedes_generation_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM subtitle_generations AS superseded
                  WHERE superseded.id =
                      subtitle_generations.supersedes_generation_id
                    AND superseded.job_id = subtitle_generations.job_id
              );

            DELETE FROM subtitle_generations
            WHERE NOT EXISTS (
                SELECT 1 FROM jobs
                WHERE jobs.id = subtitle_generations.job_id
            );

            DELETE FROM translation_items
            WHERE NOT EXISTS (
                SELECT 1 FROM translation_generations
                WHERE translation_generations.id =
                    translation_items.generation_id
            );

            DELETE FROM translation_batches
            WHERE NOT EXISTS (
                SELECT 1 FROM translation_generations
                WHERE translation_generations.id =
                    translation_batches.generation_id
            );

            UPDATE translation_generations
            SET transcript_revision_id = NULL
            WHERE transcript_revision_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM transcript_revisions
                  WHERE transcript_revisions.id =
                      translation_generations.transcript_revision_id
                    AND transcript_revisions.created_by_job_id =
                      translation_generations.job_id
              );

            UPDATE translation_generations
            SET prompt_revision_id = NULL
            WHERE prompt_revision_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM prompt_revisions
                  JOIN prompt_categories
                    ON prompt_categories.id = prompt_revisions.category_id
                  WHERE prompt_revisions.id =
                      translation_generations.prompt_revision_id
              );

            UPDATE translation_generations
            SET supersedes_generation_id = NULL
            WHERE supersedes_generation_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM translation_generations AS superseded
                  WHERE superseded.id =
                      translation_generations.supersedes_generation_id
                    AND superseded.job_id = translation_generations.job_id
              );

            DELETE FROM translation_generations
            WHERE NOT EXISTS (
                SELECT 1 FROM jobs
                WHERE jobs.id = translation_generations.job_id
            );

            UPDATE transcript_revisions
            SET audio_revision_id = NULL
            WHERE audio_revision_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM audio_revisions
                  WHERE audio_revisions.id =
                      transcript_revisions.audio_revision_id
              );

            UPDATE prompt_categories
            SET active_revision_id = NULL
            WHERE active_revision_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM prompt_revisions
                  WHERE prompt_revisions.id =
                      prompt_categories.active_revision_id
                    AND prompt_revisions.category_id = prompt_categories.id
              );

            DELETE FROM prompt_revisions
            WHERE NOT EXISTS (
                SELECT 1 FROM prompt_categories
                WHERE prompt_categories.id = prompt_revisions.category_id
            );
            """,
        )
    @staticmethod
    def _install_integrity_triggers(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS jobs_domain_insert_guard
            BEFORE INSERT ON jobs
            WHEN NEW.operation NOT IN ('extract', 'transcribe', 'translate', 'full')
              OR NEW.status NOT IN (
                  'queued', 'extracting', 'audio_ready',
                  'transcription_running', 'transcribed',
                  'translation_running', 'translated', 'rendering',
                  'audio_completed', 'transcription_completed',
                  'translation_paused', 'blocked', 'failed', 'completed'
              )
              OR NEW.phase NOT IN (
                  'extraction', 'transcription', 'translation',
                  'render', 'complete'
              )
              OR NEW.state NOT IN (
                  'waiting', 'running', 'paused', 'blocked',
                  'stopped', 'failed', 'done'
              )
              OR (
                  NEW.reason_code IS NOT NULL
                  AND NEW.reason_code NOT IN (
                      'user_stop', 'lm_unavailable', 'stt_unavailable',
                      'service_restarted', 'artifact_missing',
                      'model_output_invalid', 'invalid_input',
                      'auth_required', 'resource_exhausted',
                      'transcription_processing_error', 'internal_error'
                  )
              )
              OR NEW.attempt < 1
              OR NEW.chunks_created < 0
              OR NEW.chunks_completed < 0
              OR NEW.chunks_total_estimate < 0
              OR NEW.chunk_progress_every < 1
              OR NEW.translation_chunks_total < 0
              OR NEW.translation_chunks_completed < 0
              OR NEW.lease_token < 0
              OR NEW.force_overwrite NOT IN (0, 1)
              OR NEW.translation_pause_requested NOT IN (0, 1)
              OR NEW.job_stop_requested NOT IN (0, 1)
            BEGIN
                SELECT RAISE(ABORT, 'invalid jobs domain values');
            END;

            CREATE TRIGGER IF NOT EXISTS jobs_domain_update_guard
            BEFORE UPDATE OF operation, status, phase, state, reason_code,
                             attempt, chunks_created, chunks_completed,
                             chunks_total_estimate, chunk_progress_every,
                             translation_chunks_total,
                             translation_chunks_completed, lease_token,
                             force_overwrite, translation_pause_requested,
                             job_stop_requested
            ON jobs
            WHEN NEW.operation NOT IN ('extract', 'transcribe', 'translate', 'full')
              OR NEW.status NOT IN (
                  'queued', 'extracting', 'audio_ready',
                  'transcription_running', 'transcribed',
                  'translation_running', 'translated', 'rendering',
                  'audio_completed', 'transcription_completed',
                  'translation_paused', 'blocked', 'failed', 'completed'
              )
              OR NEW.phase NOT IN (
                  'extraction', 'transcription', 'translation',
                  'render', 'complete'
              )
              OR NEW.state NOT IN (
                  'waiting', 'running', 'paused', 'blocked',
                  'stopped', 'failed', 'done'
              )
              OR (
                  NEW.reason_code IS NOT NULL
                  AND NEW.reason_code NOT IN (
                      'user_stop', 'lm_unavailable', 'stt_unavailable',
                      'service_restarted', 'artifact_missing',
                      'model_output_invalid', 'invalid_input',
                      'auth_required', 'resource_exhausted',
                      'transcription_processing_error', 'internal_error'
                  )
              )
              OR NEW.attempt < 1
              OR NEW.chunks_created < 0
              OR NEW.chunks_completed < 0
              OR NEW.chunks_total_estimate < 0
              OR NEW.chunk_progress_every < 1
              OR NEW.translation_chunks_total < 0
              OR NEW.translation_chunks_completed < 0
              OR NEW.lease_token < 0
              OR NEW.force_overwrite NOT IN (0, 1)
              OR NEW.translation_pause_requested NOT IN (0, 1)
              OR NEW.job_stop_requested NOT IN (0, 1)
            BEGIN
                SELECT RAISE(ABORT, 'invalid jobs domain values');
            END;

            CREATE TRIGGER IF NOT EXISTS jobs_projection_insert_guard
            BEFORE INSERT ON jobs
            WHEN NOT (
                (NEW.status = 'queued' AND NEW.state = 'waiting'
                 AND NEW.phase = CASE WHEN NEW.operation = 'translate'
                                      THEN 'translation' ELSE 'extraction' END)
                OR (NEW.status = 'extracting' AND NEW.phase = 'extraction'
                    AND NEW.state = 'running')
                OR (NEW.status = 'audio_ready' AND NEW.phase = 'transcription'
                    AND NEW.state = 'waiting')
                OR (NEW.status = 'transcription_running'
                    AND NEW.phase = 'transcription' AND NEW.state = 'running')
                OR (NEW.status = 'transcribed' AND NEW.phase = 'translation'
                    AND NEW.state = 'waiting')
                OR (NEW.status = 'translation_running'
                    AND NEW.phase = 'translation' AND NEW.state = 'running')
                OR (NEW.status = 'translated' AND NEW.phase = 'render'
                    AND NEW.state = 'waiting')
                OR (NEW.status = 'rendering' AND NEW.phase = 'render'
                    AND NEW.state = 'running')
                OR (NEW.status IN (
                        'audio_completed', 'transcription_completed', 'completed'
                    ) AND NEW.phase = 'complete' AND NEW.state = 'done')
                OR (NEW.status = 'translation_paused'
                    AND NEW.phase = 'translation' AND NEW.state = 'paused')
                OR (NEW.status = 'blocked' AND NEW.phase != 'complete'
                    AND NEW.state IN ('blocked', 'stopped'))
                OR (NEW.status = 'failed' AND NEW.phase != 'complete'
                    AND NEW.state = 'failed')
            ) OR (NEW.state = 'stopped'
                  AND NEW.reason_code IS NOT 'user_stop')
              OR (NEW.state IN ('blocked', 'failed')
                  AND NEW.reason_code IS NULL)
              OR (NEW.state IN ('waiting', 'running', 'paused', 'done')
                  AND NEW.reason_code IS NOT NULL)
            BEGIN
                SELECT RAISE(ABORT, 'inconsistent jobs status projection');
            END;

            CREATE TRIGGER IF NOT EXISTS jobs_projection_update_guard
            BEFORE UPDATE OF operation, status, phase, state, reason_code
            ON jobs
            WHEN NOT (
                (NEW.status = 'queued' AND NEW.state = 'waiting'
                 AND NEW.phase = CASE WHEN NEW.operation = 'translate'
                                      THEN 'translation' ELSE 'extraction' END)
                OR (NEW.status = 'extracting' AND NEW.phase = 'extraction'
                    AND NEW.state = 'running')
                OR (NEW.status = 'audio_ready' AND NEW.phase = 'transcription'
                    AND NEW.state = 'waiting')
                OR (NEW.status = 'transcription_running'
                    AND NEW.phase = 'transcription' AND NEW.state = 'running')
                OR (NEW.status = 'transcribed' AND NEW.phase = 'translation'
                    AND NEW.state = 'waiting')
                OR (NEW.status = 'translation_running'
                    AND NEW.phase = 'translation' AND NEW.state = 'running')
                OR (NEW.status = 'translated' AND NEW.phase = 'render'
                    AND NEW.state = 'waiting')
                OR (NEW.status = 'rendering' AND NEW.phase = 'render'
                    AND NEW.state = 'running')
                OR (NEW.status IN (
                        'audio_completed', 'transcription_completed', 'completed'
                    ) AND NEW.phase = 'complete' AND NEW.state = 'done')
                OR (NEW.status = 'translation_paused'
                    AND NEW.phase = 'translation' AND NEW.state = 'paused')
                OR (NEW.status = 'blocked' AND NEW.phase != 'complete'
                    AND NEW.state IN ('blocked', 'stopped'))
                OR (NEW.status = 'failed' AND NEW.phase != 'complete'
                    AND NEW.state = 'failed')
            ) OR (NEW.state = 'stopped'
                  AND NEW.reason_code IS NOT 'user_stop')
              OR (NEW.state IN ('blocked', 'failed')
                  AND NEW.reason_code IS NULL)
              OR (NEW.state IN ('waiting', 'running', 'paused', 'done')
                  AND NEW.reason_code IS NOT NULL)
            BEGIN
                SELECT RAISE(ABORT, 'inconsistent jobs status projection');
            END;

            CREATE TRIGGER IF NOT EXISTS jobs_revision_pointer_insert_guard
            BEFORE INSERT ON jobs
            WHEN (NEW.audio_revision_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM audio_revisions
                      WHERE id = NEW.audio_revision_id
                  ))
              OR (NEW.transcript_revision_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM transcript_revisions
                      WHERE id = NEW.transcript_revision_id
                  ))
            BEGIN
                SELECT RAISE(ABORT, 'job revision pointer does not exist');
            END;

            CREATE TRIGGER IF NOT EXISTS jobs_revision_pointer_update_guard
            BEFORE UPDATE OF audio_revision_id, transcript_revision_id ON jobs
            WHEN (NEW.audio_revision_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM audio_revisions
                      WHERE id = NEW.audio_revision_id
                  ))
              OR (NEW.transcript_revision_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM transcript_revisions
                      WHERE id = NEW.transcript_revision_id
                  ))
            BEGIN
                SELECT RAISE(ABORT, 'job revision pointer does not exist');
            END;

            CREATE TRIGGER IF NOT EXISTS translation_revision_insert_guard
            BEFORE INSERT ON translation_generations
            WHEN (NEW.transcript_revision_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM transcript_revisions
                      WHERE id = NEW.transcript_revision_id
                        AND created_by_job_id = NEW.job_id
                  ))
              OR (NEW.supersedes_generation_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM translation_generations
                      WHERE id = NEW.supersedes_generation_id
                        AND job_id = NEW.job_id
                  ))
            BEGIN
                SELECT RAISE(ABORT, 'translation generation ownership mismatch');
            END;

            CREATE TRIGGER IF NOT EXISTS translation_revision_update_guard
            BEFORE UPDATE OF transcript_revision_id,
                             supersedes_generation_id, job_id
            ON translation_generations
            WHEN (NEW.transcript_revision_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM transcript_revisions
                      WHERE id = NEW.transcript_revision_id
                        AND created_by_job_id = NEW.job_id
                  ))
              OR (NEW.supersedes_generation_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM translation_generations
                      WHERE id = NEW.supersedes_generation_id
                        AND job_id = NEW.job_id
                  ))
            BEGIN
                SELECT RAISE(ABORT, 'translation generation ownership mismatch');
            END;

            CREATE TRIGGER IF NOT EXISTS subtitle_generation_insert_guard
            BEFORE INSERT ON subtitle_generations
            WHEN (NEW.translation_generation_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM translation_generations
                      WHERE id = NEW.translation_generation_id
                        AND job_id = NEW.job_id
                  ))
              OR (NEW.supersedes_generation_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM subtitle_generations
                      WHERE id = NEW.supersedes_generation_id
                        AND job_id = NEW.job_id
                  ))
            BEGIN
                SELECT RAISE(ABORT, 'subtitle generation ownership mismatch');
            END;

            CREATE TRIGGER IF NOT EXISTS subtitle_generation_update_guard
            BEFORE UPDATE OF translation_generation_id,
                             supersedes_generation_id, job_id
            ON subtitle_generations
            WHEN (NEW.translation_generation_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM translation_generations
                      WHERE id = NEW.translation_generation_id
                        AND job_id = NEW.job_id
                  ))
              OR (NEW.supersedes_generation_id IS NOT NULL AND NOT EXISTS (
                      SELECT 1 FROM subtitle_generations
                      WHERE id = NEW.supersedes_generation_id
                        AND job_id = NEW.job_id
                  ))
            BEGIN
                SELECT RAISE(ABORT, 'subtitle generation ownership mismatch');
            END;

            CREATE TRIGGER IF NOT EXISTS prompt_active_revision_insert_guard
            BEFORE INSERT ON prompt_categories
            WHEN NEW.active_revision_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM prompt_revisions
                WHERE id = NEW.active_revision_id
                  AND category_id = NEW.id
            )
            BEGIN
                SELECT RAISE(ABORT, 'prompt active revision mismatch');
            END;

            CREATE TRIGGER IF NOT EXISTS prompt_active_revision_update_guard
            BEFORE UPDATE OF active_revision_id ON prompt_categories
            WHEN NEW.active_revision_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM prompt_revisions
                WHERE id = NEW.active_revision_id
                  AND category_id = NEW.id
            )
            BEGIN
                SELECT RAISE(ABORT, 'prompt active revision mismatch');
            END;

            CREATE TRIGGER IF NOT EXISTS subtitle_publication_insert_guard
            BEFORE INSERT ON subtitle_publications
            WHEN NOT EXISTS (
                SELECT 1
                FROM jobs
                JOIN subtitle_generations
                  ON subtitle_generations.job_id = jobs.id
                WHERE jobs.id = NEW.job_id
                  AND jobs.source_rel = NEW.source_rel
                  AND subtitle_generations.id = NEW.subtitle_generation_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'subtitle publication ownership mismatch');
            END;

            CREATE TRIGGER IF NOT EXISTS subtitle_publication_update_guard
            BEFORE UPDATE ON subtitle_publications
            WHEN NOT EXISTS (
                SELECT 1
                FROM jobs
                JOIN subtitle_generations
                  ON subtitle_generations.job_id = jobs.id
                WHERE jobs.id = NEW.job_id
                  AND jobs.source_rel = NEW.source_rel
                  AND subtitle_generations.id = NEW.subtitle_generation_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'subtitle publication ownership mismatch');
            END;
            """
        )

    @staticmethod
    def _assert_foreign_key_integrity(connection: sqlite3.Connection) -> None:
        violation = connection.execute("PRAGMA foreign_key_check").fetchone()
        if violation is None:
            return
        raise RuntimeError(
            "SQLite foreign key violation remains after migration: "
            f"table={violation['table']}, rowid={violation['rowid']}"
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
            prompt_revision_id=str(row["active_revision_id"]),
            prompt_revision_number=int(row["revision_number"]),
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
        where = "" if include_archived else "WHERE category.archived = 0"
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT category.*, revision.revision_number "
                "FROM prompt_categories AS category "
                "JOIN prompt_revisions AS revision "
                "ON revision.id = category.active_revision_id "
                f"{where} "
                "ORDER BY category.archived, category.name COLLATE NOCASE, "
                "category.created_at"
            ).fetchall()
        return [
            category
            for row in rows
            if (category := self._prompt_category_from_row(row)) is not None
        ]

    def get_prompt_category(self, category_id: str) -> PromptCategory | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT category.*, revision.revision_number
                FROM prompt_categories AS category
                JOIN prompt_revisions AS revision
                  ON revision.id = category.active_revision_id
                WHERE category.id = ?
                """,
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
        revision_id = uuid4().hex
        now = time.time()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO prompt_categories (
                        id, name, translation_prompt, review_prompt,
                        active_revision_id, archived, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, 0, ?, ?)
                    """,
                    (category_id, *values, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO prompt_revisions (
                        id, category_id, revision_number,
                        translation_prompt, review_prompt,
                        content_hash, created_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        category_id,
                        values[1],
                        values[2],
                        _canonical_json_hash(
                            {
                                "translation_prompt": values[1],
                                "review_prompt": values[2],
                            }
                        ),
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE prompt_categories
                    SET active_revision_id = ?
                    WHERE id = ?
                    """,
                    (revision_id, category_id),
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
        now = time.time()
        try:
            with self._connect() as connection:
                current = connection.execute(
                    "SELECT * FROM prompt_categories WHERE id = ?",
                    (category_id,),
                ).fetchone()
                if current is None:
                    raise ValueError("프롬프트 카테고리를 찾을 수 없습니다.")
                revision_id = str(current["active_revision_id"])
                prompts_changed = (
                    str(current["translation_prompt"]) != values[1]
                    or str(current["review_prompt"]) != values[2]
                )
                if prompts_changed:
                    latest = connection.execute(
                        """
                        SELECT COALESCE(MAX(revision_number), 0) AS latest
                        FROM prompt_revisions
                        WHERE category_id = ?
                        """,
                        (category_id,),
                    ).fetchone()
                    revision_number = int(latest["latest"]) + 1
                    revision_id = uuid4().hex
                    connection.execute(
                        """
                        INSERT INTO prompt_revisions (
                            id, category_id, revision_number,
                            translation_prompt, review_prompt,
                            content_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision_id,
                            category_id,
                            revision_number,
                            values[1],
                            values[2],
                            _canonical_json_hash(
                                {
                                    "translation_prompt": values[1],
                                    "review_prompt": values[2],
                                }
                            ),
                            now,
                        ),
                    )
                result = connection.execute(
                    """
                    UPDATE prompt_categories
                    SET name = ?, translation_prompt = ?, review_prompt = ?,
                        active_revision_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (*values, revision_id, now, category_id),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("같은 이름의 프롬프트 카테고리가 있습니다.") from error
        if result.rowcount != 1:
            raise ValueError("프롬프트 카테고리를 찾을 수 없습니다.")
        updated = self.get_prompt_category(category_id)
        if updated is None:
            raise RuntimeError("updated prompt category could not be read")
        return updated

    def list_prompt_revisions(self, category_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM prompt_revisions
                WHERE category_id = ?
                ORDER BY revision_number
                """,
                (category_id,),
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "category_id": str(row["category_id"]),
                "revision_number": int(row["revision_number"]),
                "translation_prompt": str(row["translation_prompt"]),
                "review_prompt": str(row["review_prompt"]),
                "content_hash": str(row["content_hash"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def get_prompt_revision(
        self,
        category_id: str,
        revision_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM prompt_revisions
                WHERE id = ? AND category_id = ?
                """,
                (revision_id, category_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": str(row["id"]),
            "category_id": str(row["category_id"]),
            "revision_number": int(row["revision_number"]),
            "translation_prompt": str(row["translation_prompt"]),
            "review_prompt": str(row["review_prompt"]),
            "content_hash": str(row["content_hash"]),
            "created_at": float(row["created_at"]),
        }

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

    @staticmethod
    def _runtime_endpoint_from_row(
        row: sqlite3.Row | None,
    ) -> RuntimeEndpoint | None:
        if row is None:
            return None
        return RuntimeEndpoint(
            id=str(row["id"]),
            name=str(row["name"]),
            base_url=str(row["base_url"]),
            token=str(row["token"]),
            enabled=bool(row["enabled"]),
            capacity=int(row["capacity"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def list_runtime_endpoints(self) -> list[RuntimeEndpoint]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runtime_endpoints ORDER BY created_at, id"
            ).fetchall()
        return [
            endpoint
            for row in rows
            if (endpoint := self._runtime_endpoint_from_row(row)) is not None
        ]

    def get_runtime_endpoint(self, runtime_id: str) -> RuntimeEndpoint | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_endpoints WHERE id = ?",
                (runtime_id,),
            ).fetchone()
        return self._runtime_endpoint_from_row(row)

    def create_runtime_endpoint(
        self,
        *,
        name: str,
        base_url: str,
        token: str,
        enabled: bool,
        capacity: int,
    ) -> RuntimeEndpoint:
        runtime_id = uuid4().hex
        now = time.time()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO runtime_endpoints (
                        id, name, base_url, token, enabled, capacity,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        runtime_id,
                        name,
                        base_url,
                        token,
                        int(enabled),
                        capacity,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("같은 주소의 Runtime이 이미 등록되어 있습니다.") from error
        created = self.get_runtime_endpoint(runtime_id)
        if created is None:
            raise RuntimeError("created Runtime endpoint could not be read")
        return created

    def update_runtime_endpoint(
        self,
        runtime_id: str,
        *,
        name: str,
        base_url: str,
        token: str,
        enabled: bool,
        capacity: int,
    ) -> RuntimeEndpoint:
        try:
            with self._connect() as connection:
                result = connection.execute(
                    """
                    UPDATE runtime_endpoints
                    SET name = ?, base_url = ?, token = ?, enabled = ?,
                        capacity = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        name,
                        base_url,
                        token,
                        int(enabled),
                        capacity,
                        time.time(),
                        runtime_id,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("같은 주소의 Runtime이 이미 등록되어 있습니다.") from error
        if result.rowcount != 1:
            raise ValueError("Runtime을 찾을 수 없습니다.")
        updated = self.get_runtime_endpoint(runtime_id)
        if updated is None:
            raise RuntimeError("updated Runtime endpoint could not be read")
        return updated

    def delete_runtime_endpoint(self, runtime_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM runtime_batch_settings WHERE runtime_id = ?",
                (runtime_id,),
            )
            result = connection.execute(
                "DELETE FROM runtime_endpoints WHERE id = ?",
                (runtime_id,),
            )
        if result.rowcount != 1:
            raise ValueError("Runtime을 찾을 수 없습니다.")

    def runtime_batch_settings(self) -> dict[str, dict[str, int | None]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT runtime_id, kotoba_batch_size, whisperx_batch_size
                FROM runtime_batch_settings
                """
            ).fetchall()
        return {
            str(row["runtime_id"]): {
                "kotoba_batch_size": (
                    int(row["kotoba_batch_size"])
                    if row["kotoba_batch_size"] is not None
                    else None
                ),
                "whisperx_batch_size": (
                    int(row["whisperx_batch_size"])
                    if row["whisperx_batch_size"] is not None
                    else None
                ),
            }
            for row in rows
        }

    def save_runtime_batch_settings(
        self,
        runtime_id: str,
        *,
        kotoba_batch_size: int | None,
        whisperx_batch_size: int | None,
    ) -> None:
        with self._connect() as connection:
            if kotoba_batch_size is None and whisperx_batch_size is None:
                connection.execute(
                    "DELETE FROM runtime_batch_settings WHERE runtime_id = ?",
                    (runtime_id,),
                )
                return
            connection.execute(
                """
                INSERT INTO runtime_batch_settings (
                    runtime_id, kotoba_batch_size,
                    whisperx_batch_size, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(runtime_id) DO UPDATE SET
                    kotoba_batch_size = excluded.kotoba_batch_size,
                    whisperx_batch_size = excluded.whisperx_batch_size,
                    updated_at = excluded.updated_at
                """,
                (
                    runtime_id,
                    kotoba_batch_size,
                    whisperx_batch_size,
                    time.time(),
                ),
            )

    def transcription_runtime_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT COALESCE(stt_runtime_id, 'builtin') AS runtime_id,
                       COUNT(*) AS job_count
                FROM jobs
                WHERE status = 'transcription_running'
                GROUP BY COALESCE(stt_runtime_id, 'builtin')
                """
            ).fetchall()
        return {
            str(row["runtime_id"]): int(row["job_count"])
            for row in rows
        }

    def runtime_has_active_transcriptions(self, runtime_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE status = 'transcription_running'
                  AND COALESCE(stt_runtime_id, 'builtin') = ?
                LIMIT 1
                """,
                (runtime_id,),
            ).fetchone()
        return row is not None

    def get_remote_server_settings(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT stt_base_url, stt_token
                FROM builtin_runtime_settings
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "stt_base_url": str(row["stt_base_url"]),
            "stt_token": str(row["stt_token"]),
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
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO builtin_runtime_settings (
                    id, stt_base_url, stt_token, updated_at
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    stt_base_url = excluded.stt_base_url,
                    stt_token = excluded.stt_token,
                    updated_at = excluded.updated_at
                """,
                (
                    stt_base_url,
                    stt_token,
                    time.time(),
                ),
            )

    def get_subtitle_validator_settings(self) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT provider, base_url, token, model, region
                FROM subtitle_validator_settings
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "provider": str(row["provider"]),
            "base_url": str(row["base_url"]),
            "token": str(row["token"]),
            "model": str(row["model"]),
            "region": str(row["region"]),
        }

    def save_subtitle_validator_settings(
        self,
        *,
        provider: str,
        base_url: str,
        token: str,
        model: str,
        region: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO subtitle_validator_settings (
                    id, provider, base_url, token, model, region, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    provider = excluded.provider,
                    base_url = excluded.base_url,
                    token = excluded.token,
                    model = excluded.model,
                    region = excluded.region,
                    updated_at = excluded.updated_at
                """,
                (provider, base_url, token, model, region, time.time()),
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
            audio_revision_id=(
                str(row["audio_revision_id"])
                if row["audio_revision_id"]
                else None
            ),
            stt_job_id=str(row["stt_job_id"]) if row["stt_job_id"] else None,
            stt_runtime_id=(
                str(row["stt_runtime_id"]) if row["stt_runtime_id"] else None
            ),
            transcript_path=(
                str(row["transcript_path"]) if row["transcript_path"] else None
            ),
            transcript_revision_id=(
                str(row["transcript_revision_id"])
                if row["transcript_revision_id"]
                else None
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
            transcription_stage=(
                str(row["transcription_stage"])
                if row["transcription_stage"]
                else None
            ),
            transcription_stage_index=int(row["transcription_stage_index"]),
            transcription_stage_total=int(row["transcription_stage_total"]),
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
        audio_revision_id: str | None = None,
        transcript_path: str | None = None,
        transcript_revision_id: str | None = None,
        chunks_total_estimate: int = 0,
    ) -> PipelineJob:
        if operation not in JOB_OPERATIONS:
            raise ValueError("unsupported job operation")
        if status not in JOB_STATUSES:
            raise ValueError("unsupported job status")
        now = time.time()
        projected = structured_state_from_legacy(
            status=status,
            operation=operation,
        )
        with self._connect() as connection:
            if audio_revision_id is not None and connection.execute(
                "SELECT 1 FROM audio_revisions WHERE id = ?",
                (audio_revision_id,),
            ).fetchone() is None:
                raise ValueError("audio revision not found")
            if transcript_revision_id is not None and connection.execute(
                "SELECT 1 FROM transcript_revisions WHERE id = ?",
                (transcript_revision_id,),
            ).fetchone() is None:
                raise ValueError("transcript revision not found")
            connection.execute(
                """
                INSERT INTO jobs (
                    id, source_rel, status, force_overwrite, operation,
                    phase, state, reason_code, attempt,
                    options_json, audio_path, audio_sha256, audio_revision_id,
                    transcript_path, transcript_revision_id,
                    chunks_total_estimate,
                    created_at, status_updated_at, updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
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
                    audio_revision_id,
                    transcript_path,
                    transcript_revision_id,
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
            event_code="job.created",
            to_state=projected.state.value,
            phase=projected.phase.value,
            attempt=1,
            payload={"operation": operation, "legacy_status": status},
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
        operations: Collection[str] | None,
        statuses: Collection[str] | None,
        states: Collection[str] | None,
        phases: Collection[str] | None,
        reason_codes: Collection[str] | None,
        legacy_phase_statuses: Collection[str] | None,
        include_comparison_transcriptions: bool,
    ) -> tuple[str, list[object]] | None:
        def normalized(
            values: Collection[str] | None,
        ) -> tuple[str, ...] | None:
            return tuple(sorted(set(values))) if values is not None else None

        operation_values = normalized(operations)
        status_values = normalized(statuses)
        state_values = normalized(states)
        phase_values = normalized(phases)
        reason_values = normalized(reason_codes)
        legacy_phase_values = normalized(legacy_phase_statuses)
        if () in (
            operation_values,
            status_values,
            state_values,
            phase_values,
            reason_values,
        ):
            return None
        if legacy_phase_values == ():
            legacy_phase_values = None

        conditions: list[str] = []
        parameters: list[object] = []
        for column, values in (
            ("operation", operation_values),
            ("status", status_values),
            ("state", state_values),
            ("reason_code", reason_values),
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
        operations: Collection[str] | None = None,
        statuses: Collection[str] | None = None,
        states: Collection[str] | None = None,
        phases: Collection[str] | None = None,
        reason_codes: Collection[str] | None = None,
        legacy_phase_statuses: Collection[str] | None = None,
        include_comparison_transcriptions: bool = True,
    ) -> list[PipelineJob]:
        filtered = self._job_filter_clause(
            operations=operations,
            statuses=statuses,
            states=states,
            phases=phases,
            reason_codes=reason_codes,
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
        operations: Collection[str] | None = None,
        statuses: Collection[str] | None = None,
        states: Collection[str] | None = None,
        phases: Collection[str] | None = None,
        reason_codes: Collection[str] | None = None,
        legacy_phase_statuses: Collection[str] | None = None,
        include_comparison_transcriptions: bool = True,
    ) -> int:
        filtered = self._job_filter_clause(
            operations=operations,
            statuses=statuses,
            states=states,
            phases=phases,
            reason_codes=reason_codes,
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
        stt_runtime_id: str | None = None,
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
            runtime_assignment = (
                ", stt_runtime_id = ?" if stt_runtime_id is not None else ""
            )
            parameters: list[Any] = [
                running_status,
                projected.phase.value,
                projected.state.value,
                lease_owner,
                now + lease_seconds,
                now,
                now,
            ]
            if stt_runtime_id is not None:
                parameters.append(stt_runtime_id)
            parameters.extend((job_id, waiting_status, now))
            result = connection.execute(
                "UPDATE jobs SET status = ?, phase = ?, state = ?, "
                "reason_code = NULL, blocked_stage = NULL, error = NULL, "
                "lease_owner = ?, lease_expires_at = ?, "
                "lease_token = lease_token + 1, "
                "status_updated_at = ?, updated_at = ?"
                f"{runtime_assignment} "
                "WHERE id = ? AND status = ? AND job_stop_requested = 0 "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= ?)"
                f"{translation_condition}",
                parameters,
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

    def audio_revisions_for_signature(
        self,
        *,
        source_rel: str,
        source_hash: str,
        extraction_hash: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM audio_revisions
                WHERE source_rel = ? AND source_hash = ?
                  AND extraction_hash = ?
                ORDER BY created_at DESC
                """,
                (source_rel, source_hash, extraction_hash),
            ).fetchall()
        return [self._audio_revision_from_row(row) for row in rows]

    def get_audio_revision(self, revision_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM audio_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
        return self._audio_revision_from_row(row) if row is not None else None

    def record_audio_revision(
        self,
        *,
        revision_id: str,
        job_id: str,
        source_rel: str,
        source_hash: str,
        extraction_hash: str,
        artifact_path: str,
        content_hash: str,
        duration_seconds: float | None,
        status: str,
        chunks_total_estimate: int,
        lease_owner: str | None = None,
        lease_token: int | None = None,
    ) -> bool:
        if (lease_owner is None) != (lease_token is None):
            raise ValueError("lease owner and token must be provided together")
        now = time.time()
        with self._connect() as connection:
            job = connection.execute(
                "SELECT operation FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise ValueError("job not found")
            if lease_owner is not None and lease_token is not None:
                owned = connection.execute(
                    """
                    UPDATE jobs
                    SET lease_expires_at = lease_expires_at
                    WHERE id = ? AND status = 'extracting'
                      AND lease_owner = ? AND lease_token = ?
                      AND lease_expires_at > ?
                    """,
                    (job_id, lease_owner, lease_token, now),
                )
                if owned.rowcount != 1:
                    return False
            projected = structured_state_from_legacy(
                status=status,
                operation=str(job["operation"]),
            )
            connection.execute(
                """
                INSERT INTO audio_revisions (
                    id, created_by_job_id, source_rel, source_hash,
                    extraction_hash, artifact_path, content_hash,
                    duration_seconds, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    job_id,
                    source_rel,
                    source_hash,
                    extraction_hash,
                    artifact_path,
                    content_hash,
                    duration_seconds,
                    now,
                ),
            )
            updated = connection.execute(
                """
                UPDATE jobs
                SET status = ?, phase = ?, state = ?, reason_code = ?,
                    audio_path = ?, audio_sha256 = ?, audio_revision_id = ?,
                    chunks_total_estimate = ?, blocked_stage = NULL,
                    error = NULL, lease_owner = NULL,
                    lease_expires_at = NULL,
                    status_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    projected.phase.value,
                    projected.state.value,
                    (
                        projected.reason_code.value
                        if projected.reason_code is not None
                        else None
                    ),
                    artifact_path,
                    content_hash,
                    revision_id,
                    max(0, int(chunks_total_estimate)),
                    now,
                    now,
                    job_id,
                ),
            )
        persisted = updated.rowcount == 1
        if persisted:
            self._notify_change(job_id)
        return persisted

    def record_transcript_revision(
        self,
        *,
        revision_id: str,
        job_id: str,
        audio_revision_id: str | None,
        remote_job_id: str | None,
        backend: str,
        model_revision: str,
        options_hash: str,
        artifact_path: str,
        content_hash: str,
        origin: str,
        status: str | None,
        chunks_total: int,
        translation_pause_requested: bool = False,
        lease_owner: str | None = None,
        lease_token: int | None = None,
    ) -> bool:
        if origin not in {"automatic", "manual", "imported"}:
            raise ValueError("invalid transcript revision origin")
        if (lease_owner is None) != (lease_token is None):
            raise ValueError("lease owner and token must be provided together")
        now = time.time()
        with self._connect() as connection:
            job = connection.execute(
                "SELECT operation FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if job is None:
                raise ValueError("job not found")
            if audio_revision_id is not None and connection.execute(
                "SELECT 1 FROM audio_revisions WHERE id = ?",
                (audio_revision_id,),
            ).fetchone() is None:
                raise ValueError("audio revision not found")
            if lease_owner is not None and lease_token is not None:
                owned = connection.execute(
                    """
                    UPDATE jobs
                    SET lease_expires_at = lease_expires_at
                    WHERE id = ? AND status = 'transcription_running'
                      AND lease_owner = ? AND lease_token = ?
                      AND lease_expires_at > ?
                    """,
                    (job_id, lease_owner, lease_token, now),
                )
                if owned.rowcount != 1:
                    return False
            connection.execute(
                """
                INSERT INTO transcript_revisions (
                    id, created_by_job_id, audio_revision_id, remote_job_id,
                    backend, model_revision, options_hash, artifact_path,
                    content_hash, origin, chunks_total, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    revision_id,
                    job_id,
                    audio_revision_id,
                    remote_job_id,
                    backend,
                    model_revision,
                    options_hash,
                    artifact_path,
                    content_hash,
                    origin,
                    max(0, int(chunks_total)),
                    now,
                ),
            )
            assignments = [
                "transcript_path = ?",
                "transcript_revision_id = ?",
                "chunks_created = ?",
                "chunks_completed = ?",
                "chunks_total_estimate = ?",
                "updated_at = ?",
            ]
            values: list[Any] = [
                artifact_path,
                revision_id,
                max(0, int(chunks_total)),
                max(0, int(chunks_total)),
                max(0, int(chunks_total)),
                now,
            ]
            if status is not None:
                projected = structured_state_from_legacy(
                    status=status,
                    operation=str(job["operation"]),
                )
                assignments.extend(
                    [
                        "status = ?",
                        "phase = ?",
                        "state = ?",
                        "reason_code = ?",
                        "translation_pause_requested = ?",
                        "blocked_stage = NULL",
                        "error = NULL",
                        "lease_owner = NULL",
                        "lease_expires_at = NULL",
                        "status_updated_at = ?",
                    ]
                )
                values.extend(
                    [
                        status,
                        projected.phase.value,
                        projected.state.value,
                        (
                            projected.reason_code.value
                            if projected.reason_code is not None
                            else None
                        ),
                        int(translation_pause_requested),
                        now,
                    ]
                )
            values.append(job_id)
            updated = connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
        persisted = updated.rowcount == 1
        if persisted:
            self._notify_change(job_id)
        return persisted

    def transcript_revisions(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM transcript_revisions
                WHERE created_by_job_id = ?
                ORDER BY created_at
                """,
                (job_id,),
            ).fetchall()
        return [self._transcript_revision_from_row(row) for row in rows]

    def get_transcript_revision(
        self,
        job_id: str,
        revision_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM transcript_revisions
                WHERE id = ? AND created_by_job_id = ?
                """,
                (revision_id, job_id),
            ).fetchone()
        return (
            self._transcript_revision_from_row(row)
            if row is not None
            else None
        )

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
            if (
                "operation" in fields
                and str(fields["operation"]) not in JOB_OPERATIONS
            ):
                raise ValueError("unsupported job operation")
            if "status" in fields and str(fields["status"]) not in JOB_STATUSES:
                raise ValueError("unsupported job status")
            if "phase" in fields:
                JobPhase(str(fields["phase"]))
            if "state" in fields:
                JobState(str(fields["state"]))
            if fields.get("reason_code") is not None:
                JobReason(str(fields["reason_code"]))
            if (
                fields.get("transcription_stage") is not None
                and str(fields["transcription_stage"])
                not in TRANSCRIPTION_STAGE_LABELS
            ):
                raise ValueError("unsupported transcription stage")
        except ValueError as error:
            raise ValueError("invalid structured job state") from error
        if "attempt" in fields and int(fields["attempt"]) < 1:
            raise ValueError("job attempt must be at least 1")
        if (
            "chunk_progress_every" in fields
            and int(fields["chunk_progress_every"]) < 1
        ):
            raise ValueError("chunk progress interval must be at least 1")
        if {
            "transcription_stage",
            "transcription_stage_index",
            "transcription_stage_total",
        }.issubset(fields):
            stage = fields["transcription_stage"]
            index = int(fields["transcription_stage_index"])
            total = int(fields["transcription_stage_total"])
            if stage is None:
                if index or total:
                    raise ValueError("empty transcription stage has progress")
            elif total < 1 or not 1 <= index <= total:
                raise ValueError("invalid transcription stage position")
        for field in NONNEGATIVE_JOB_FIELDS & fields.keys():
            if int(fields[field]) < 0:
                raise ValueError(f"{field} must not be negative")
        for field in BOOLEAN_JOB_FIELDS & fields.keys():
            if int(fields[field]) not in {0, 1}:
                raise ValueError(f"{field} must be boolean")

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
                """
                DELETE FROM subtitle_publications
                WHERE job_id = ? OR subtitle_generation_id IN (
                    SELECT id FROM subtitle_generations WHERE job_id = ?
                )
                """,
                (job_id, job_id),
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
            if result.rowcount == 1:
                connection.execute(
                    """
                    DELETE FROM transcript_revisions
                    WHERE NOT EXISTS (
                        SELECT 1 FROM jobs
                        WHERE jobs.id =
                            transcript_revisions.created_by_job_id
                    )
                      AND NOT EXISTS (
                          SELECT 1 FROM jobs
                          WHERE jobs.transcript_revision_id =
                              transcript_revisions.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM translation_generations
                          WHERE translation_generations.transcript_revision_id =
                              transcript_revisions.id
                      )
                    """
                )
                connection.execute(
                    """
                    DELETE FROM audio_revisions
                    WHERE NOT EXISTS (
                        SELECT 1 FROM jobs
                        WHERE jobs.id = audio_revisions.created_by_job_id
                    )
                      AND NOT EXISTS (
                          SELECT 1 FROM jobs
                          WHERE jobs.audio_revision_id = audio_revisions.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM transcript_revisions
                          WHERE transcript_revisions.audio_revision_id =
                              audio_revisions.id
                      )
                    """
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
        transcript_revision_id: str | None = None,
        prompt_revision_id: str | None = None,
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
            if transcript_revision_id is not None:
                transcript_revision = connection.execute(
                    """
                    SELECT 1 FROM transcript_revisions
                    WHERE id = ? AND created_by_job_id = ?
                    """,
                    (transcript_revision_id, job_id),
                ).fetchone()
                if transcript_revision is None:
                    raise ValueError(
                        "transcript revision does not belong to job"
                    )
            if prompt_revision_id is not None and connection.execute(
                "SELECT 1 FROM prompt_revisions WHERE id = ?",
                (prompt_revision_id,),
            ).fetchone() is None:
                raise ValueError("prompt revision not found")
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
                    transcript_job_id, transcript_revision_id,
                    transcript_hash, prompt_hash, prompt_revision_id,
                    endpoint_key, model, config_hash,
                    state, attempt, origin, supersedes_generation_id,
                    artifact_path, last_error,
                    created_at, updated_at, completed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'partial', 0, ?, ?, ?,
                    NULL, ?, ?, NULL
                )
                """,
                (
                    generation_id,
                    job_id,
                    generation_number,
                    transcript_job_id,
                    transcript_revision_id,
                    transcript_hash,
                    prompt_hash,
                    prompt_revision_id,
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
                       transcript.artifact_path AS transcript_artifact_path,
                       prompt.revision_number AS prompt_revision_number,
                       category.name AS prompt_category_name,
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
                LEFT JOIN transcript_revisions AS transcript
                  ON transcript.id = generation.transcript_revision_id
                LEFT JOIN prompt_revisions AS prompt
                  ON prompt.id = generation.prompt_revision_id
                LEFT JOIN prompt_categories AS category
                  ON category.id = prompt.category_id
                WHERE generation.job_id = ?
                ORDER BY generation_number
                """,
                (job_id,),
            ).fetchall()
        return [self._translation_generation_from_row(row) for row in rows]

    def begin_translation_generation_attempt(
        self,
        generation_id: str,
        *,
        lease_owner: str | None = None,
        lease_token: int | None = None,
    ) -> int:
        if (lease_owner is None) != (lease_token is None):
            raise ValueError("lease owner and token must be provided together")
        lease_condition = ""
        parameters: list[Any] = [time.time(), generation_id]
        if lease_owner is not None and lease_token is not None:
            lease_condition = (
                " AND EXISTS (SELECT 1 FROM jobs "
                "WHERE jobs.id = translation_generations.job_id "
                "AND jobs.lease_owner = ? AND jobs.lease_token = ? "
                "AND jobs.lease_expires_at > ?)"
            )
            parameters.extend([lease_owner, lease_token, time.time()])
        with self._connect() as connection:
            updated = connection.execute(
                f"""
                UPDATE translation_generations
                SET state = 'running', attempt = attempt + 1,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                {lease_condition}
                """,
                parameters,
            )
            row = connection.execute(
                "SELECT attempt FROM translation_generations WHERE id = ?",
                (generation_id,),
            ).fetchone()
        if updated.rowcount != 1:
            if lease_owner is not None:
                raise WorkerLeaseLost("worker lease was superseded")
            raise ValueError("translation generation not found")
        if row is None:
            raise ValueError("translation generation not found")
        return int(row["attempt"])

    def reconcile_interrupted_translation_attempts(self) -> dict[str, int]:
        """Finalize the latest translation attempt after job recovery."""

        now = time.time()
        generation_count = 0
        batch_count = 0
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT generation.id, job.status AS job_status,
                       job.state AS job_state, job.error AS job_error
                FROM translation_generations AS generation
                JOIN jobs AS job ON job.id = generation.job_id
                WHERE generation.state IN ('running', 'partial')
                  AND job.status != 'translation_running'
                  AND (
                      generation.state = 'running'
                      OR generation.attempt > 0
                      OR EXISTS (
                          SELECT 1 FROM translation_batches AS active_batch
                          WHERE active_batch.generation_id = generation.id
                            AND active_batch.state = 'running'
                      )
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM translation_generations AS newer
                      WHERE newer.job_id = generation.job_id
                        AND newer.generation_number
                            > generation.generation_number
                  )
                ORDER BY generation.created_at
                """
            ).fetchall()
            for row in rows:
                job_status = str(row["job_status"])
                job_state = str(row["job_state"])
                job_error = (
                    str(row["job_error"]) if row["job_error"] else None
                )
                if job_state == JobState.STOPPED.value:
                    target_state = "stopped"
                elif job_status == "translation_paused":
                    target_state = "paused"
                elif job_status == "blocked":
                    target_state = "blocked"
                elif job_status == "failed":
                    target_state = "failed"
                else:
                    target_state = "interrupted"
                last_error = (
                    None
                    if target_state == "paused"
                    else job_error or TRANSLATION_RESTART_INTERRUPTED
                )
                generation_id = str(row["id"])
                updated = connection.execute(
                    """
                    UPDATE translation_generations
                    SET state = ?, last_error = ?, updated_at = ?
                    WHERE id = ? AND state IN ('running', 'partial')
                      AND (
                          state = 'running' OR attempt > 0
                          OR EXISTS (
                              SELECT 1
                              FROM translation_batches AS active_batch
                              WHERE active_batch.generation_id =
                                  translation_generations.id
                                AND active_batch.state = 'running'
                          )
                      )
                      AND EXISTS (
                          SELECT 1 FROM jobs
                          WHERE jobs.id = translation_generations.job_id
                            AND jobs.status != 'translation_running'
                      )
                    """,
                    (target_state, last_error, now, generation_id),
                )
                if updated.rowcount != 1:
                    continue
                generation_count += 1
                interrupted_batches = connection.execute(
                    """
                    UPDATE translation_batches
                    SET state = 'interrupted', error = ?, updated_at = ?
                    WHERE generation_id = ? AND state = 'running'
                    """,
                    (
                        TRANSLATION_BATCH_RESTART_INTERRUPTED,
                        now,
                        generation_id,
                    ),
                )
                batch_count += interrupted_batches.rowcount
        return {
            "generation_count": generation_count,
            "batch_count": batch_count,
        }

    def mark_translation_generation(
        self,
        generation_id: str,
        *,
        state: str,
        error: str | None = None,
        generation_attempt: int | None = None,
    ) -> None:
        if state not in {
            "partial",
            "paused",
            "blocked",
            "failed",
            "stopped",
            "interrupted",
        }:
            raise ValueError("invalid translation generation state")
        attempt_condition = ""
        parameters: list[Any] = [state, error, time.time(), generation_id]
        if generation_attempt is not None:
            attempt_condition = (
                " AND attempt = ? AND state IN ('running', 'partial')"
            )
            parameters.append(generation_attempt)
        with self._connect() as connection:
            updated = connection.execute(
                f"""
                UPDATE translation_generations
                SET state = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                {attempt_condition}
                """,
                parameters,
            )
        if updated.rowcount != 1:
            if generation_attempt is not None:
                raise WorkerLeaseLost("translation attempt was superseded")
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
                """
                UPDATE translation_generations
                SET updated_at = updated_at
                WHERE id = ? AND attempt = ?
                  AND state IN ('running', 'partial')
                """,
                (generation_id, generation_attempt),
            )
            if generation.rowcount != 1:
                raise WorkerLeaseLost("translation attempt was superseded")
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
        generation_attempt: int | None = None,
    ) -> None:
        attempt_condition = ""
        parameters: list[Any] = [
            error[:2000],
            time.time(),
            generation_id,
            batch_index,
        ]
        if generation_attempt is not None:
            attempt_condition = (
                " AND EXISTS (SELECT 1 FROM translation_generations "
                "WHERE translation_generations.id = "
                "translation_batches.generation_id "
                "AND translation_generations.attempt = ? "
                "AND translation_generations.state "
                "IN ('running', 'partial'))"
            )
            parameters.append(generation_attempt)
        with self._connect() as connection:
            updated = connection.execute(
                f"""
                UPDATE translation_batches
                SET state = 'failed', error = ?, updated_at = ?
                WHERE generation_id = ? AND batch_index = ?
                {attempt_condition}
                """,
                parameters,
            )
        if updated.rowcount != 1:
            if generation_attempt is not None:
                raise WorkerLeaseLost("translation attempt was superseded")
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
                """
                UPDATE translation_generations
                SET updated_at = updated_at
                WHERE id = ? AND attempt = ?
                  AND state IN ('running', 'partial')
                """,
                (generation_id, generation_attempt),
            )
            if generation.rowcount != 1:
                raise WorkerLeaseLost("translation attempt was superseded")
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
        *,
        generation_attempt: int | None = None,
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
            attempt_condition = ""
            parameters: list[Any] = [now, now, generation_id]
            if generation_attempt is not None:
                attempt_condition = (
                    " AND attempt = ? AND state IN ('running', 'partial')"
                )
                parameters.append(generation_attempt)
            updated = connection.execute(
                f"""
                UPDATE translation_generations
                SET state = 'completed', last_error = NULL,
                    completed_at = ?, updated_at = ?
                WHERE id = ?
                {attempt_condition}
                """,
                parameters,
            )
        if updated.rowcount != 1:
            if generation_attempt is not None:
                raise WorkerLeaseLost("translation attempt was superseded")
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

    def list_recoverable_subtitle_generations(self) -> list[dict[str, Any]]:
        """Return completed render artifacts abandoned by an expired worker."""

        now = time.time()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT generation.*, 0 AS is_published,
                       job.source_rel AS recovery_source_rel
                FROM subtitle_generations AS generation
                JOIN jobs AS job ON job.id = generation.job_id
                WHERE job.status = 'rendering'
                  AND (
                      job.lease_expires_at IS NULL
                      OR job.lease_expires_at <= ?
                  )
                  AND generation.generation_number = (
                      SELECT MAX(latest.generation_number)
                      FROM subtitle_generations AS latest
                      WHERE latest.job_id = generation.job_id
                  )
                ORDER BY generation.created_at
                """,
                (now,),
            ).fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            candidate = self._subtitle_generation_from_row(row)
            candidate["source_rel"] = str(row["recovery_source_rel"])
            candidates.append(candidate)
        return candidates

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
                SELECT generation.*, job.source_rel, job.operation
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
            projected = structured_state_from_legacy(
                status="completed",
                operation=str(generation["operation"]),
            )
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
                SET status = 'completed', phase = ?, state = ?,
                    reason_code = ?, srt_path = ?, ass_path = ?,
                    blocked_stage = NULL, error = NULL,
                    lease_owner = NULL, lease_expires_at = NULL,
                    status_updated_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    projected.phase.value,
                    projected.state.value,
                    (
                        projected.reason_code.value
                        if projected.reason_code is not None
                        else None
                    ),
                    srt_path,
                    ass_path,
                    now,
                    now,
                    job_id,
                ),
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
    def _audio_revision_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "created_by_job_id": str(row["created_by_job_id"]),
            "source_rel": str(row["source_rel"]),
            "source_hash": str(row["source_hash"]),
            "extraction_hash": str(row["extraction_hash"]),
            "artifact_path": str(row["artifact_path"]),
            "content_hash": str(row["content_hash"]),
            "duration_seconds": (
                float(row["duration_seconds"])
                if row["duration_seconds"] is not None
                else None
            ),
            "created_at": float(row["created_at"]),
        }

    @staticmethod
    def _transcript_revision_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"]),
            "created_by_job_id": str(row["created_by_job_id"]),
            "audio_revision_id": (
                str(row["audio_revision_id"])
                if row["audio_revision_id"]
                else None
            ),
            "remote_job_id": (
                str(row["remote_job_id"]) if row["remote_job_id"] else None
            ),
            "backend": str(row["backend"]),
            "model_revision": str(row["model_revision"]),
            "options_hash": str(row["options_hash"]),
            "artifact_path": str(row["artifact_path"]),
            "content_hash": str(row["content_hash"]),
            "origin": str(row["origin"]),
            "chunks_total": int(row["chunks_total"]),
            "created_at": float(row["created_at"]),
        }

    @staticmethod
    def _translation_generation_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "id": str(row["id"]),
            "job_id": str(row["job_id"]),
            "generation_number": int(row["generation_number"]),
            "transcript_job_id": str(row["transcript_job_id"]),
            "transcript_revision_id": (
                str(row["transcript_revision_id"])
                if row["transcript_revision_id"]
                else None
            ),
            "transcript_hash": str(row["transcript_hash"]),
            "prompt_hash": str(row["prompt_hash"]),
            "prompt_revision_id": (
                str(row["prompt_revision_id"])
                if row["prompt_revision_id"]
                else None
            ),
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
        if "transcript_artifact_path" in row.keys():
            result["transcript_artifact_path"] = (
                str(row["transcript_artifact_path"])
                if row["transcript_artifact_path"] is not None
                else None
            )
        if "prompt_revision_number" in row.keys():
            result["prompt_revision_number"] = (
                int(row["prompt_revision_number"])
                if row["prompt_revision_number"] is not None
                else None
            )
        if "prompt_category_name" in row.keys():
            result["prompt_category_name"] = (
                str(row["prompt_category_name"])
                if row["prompt_category_name"] is not None
                else None
            )
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
        provider: str,
        model: str,
        input_hash: str,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE subtitle_validations
                SET llm_json = ?, validator_provider = ?, validator_model = ?,
                    validator_input_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(dict(result), ensure_ascii=False, sort_keys=True),
                    provider,
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
            "validator_provider": (
                str(row["validator_provider"])
                if row["validator_provider"] is not None
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

    @staticmethod
    def _event_payload_json(payload: Mapping[str, Any] | None) -> str:
        document = dict(payload or {})
        credential_suffixes = tuple(
            f"_{part}" for part in SENSITIVE_EVENT_KEY_PARTS
        )

        def reject_sensitive_keys(value: Any) -> None:
            if isinstance(value, Mapping):
                for key, nested in value.items():
                    normalized = str(key).strip().lower()
                    if (
                        normalized != "lease_token"
                        and (
                            normalized in SENSITIVE_EVENT_KEY_PARTS
                            or normalized.endswith(credential_suffixes)
                        )
                    ):
                        raise ValueError(
                            "sensitive keys are not allowed in event payloads"
                        )
                    reject_sensitive_keys(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    reject_sensitive_keys(nested)

        reject_sensitive_keys(document)
        try:
            encoded = json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as error:
            raise ValueError("event payload must be JSON serializable") from error
        if len(encoded.encode("utf-8")) > EVENT_PAYLOAD_MAX_BYTES:
            raise ValueError("event payload exceeds 16 KiB")
        return encoded

    def add_event(
        self,
        job_id: str,
        level: str,
        message: str,
        *,
        event_code: str = "job.message",
        from_state: str | None = None,
        to_state: str | None = None,
        phase: str | None = None,
        attempt: int | None = None,
        correlation_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        normalized_code = event_code.strip().lower()
        if (
            not normalized_code
            or len(normalized_code) > 100
            or EVENT_CODE_PATTERN.fullmatch(normalized_code) is None
        ):
            raise ValueError("invalid event code")
        payload_json = self._event_payload_json(payload)
        event_created_at = time.time()
        stage_duration_observation: tuple[
            float,
            dict[str, str | int | bool],
        ] | None = None
        with self._connect() as connection:
            current = connection.execute(
                "SELECT phase, state, attempt FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            resolved_phase = (
                phase
                if phase is not None
                else (str(current["phase"]) if current is not None else None)
            )
            resolved_to_state = (
                to_state
                if to_state is not None
                else (str(current["state"]) if current is not None else None)
            )
            resolved_attempt = (
                attempt
                if attempt is not None
                else (int(current["attempt"]) if current is not None else None)
            )
            try:
                if from_state is not None:
                    JobState(from_state)
                if resolved_to_state is not None:
                    JobState(resolved_to_state)
                if resolved_phase is not None:
                    JobPhase(resolved_phase)
            except ValueError as error:
                raise ValueError("invalid structured event state") from error
            if resolved_attempt is not None and resolved_attempt < 1:
                raise ValueError("event attempt must be at least 1")
            resolved_correlation_id = (
                correlation_id.strip()
                if correlation_id is not None
                else (
                    f"{job_id}:{resolved_attempt}"
                    if resolved_attempt is not None
                    else None
                )
            )
            if resolved_correlation_id == "":
                resolved_correlation_id = None
            if (
                resolved_correlation_id is not None
                and len(resolved_correlation_id) > 200
            ):
                raise ValueError("event correlation ID exceeds 200 characters")
            connection.execute(
                """
                INSERT INTO job_events (
                    job_id, level, message, event_code, from_state, to_state,
                    phase, attempt, correlation_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    level,
                    message[:4000],
                    normalized_code,
                    from_state,
                    resolved_to_state,
                    resolved_phase,
                    resolved_attempt,
                    resolved_correlation_id,
                    payload_json,
                    event_created_at,
                ),
            )
            outcome = STAGE_DURATION_OUTCOMES.get(normalized_code)
            if (
                outcome is not None
                and resolved_phase is not None
                and resolved_attempt is not None
            ):
                started_attempt = resolved_attempt - int(
                    normalized_code == "transcription.runtime_failover"
                )
                started = connection.execute(
                    """
                    SELECT created_at, payload_json
                    FROM job_events
                    WHERE job_id = ?
                      AND event_code = 'stage.started'
                      AND phase = ?
                      AND attempt = ?
                      AND created_at <= ?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (
                        job_id,
                        resolved_phase,
                        started_attempt,
                        event_created_at,
                    ),
                ).fetchone()
                if started is not None:
                    labels: dict[str, str | int | bool] = {
                        "phase": resolved_phase,
                        "outcome": outcome,
                    }
                    if resolved_phase == JobPhase.TRANSCRIPTION.value:
                        started_payload = json.loads(
                            str(started["payload_json"])
                        )
                        runtime_id = started_payload.get("runtime_id")
                        if (
                            runtime_id is None
                            and normalized_code
                            == "transcription.runtime_failover"
                        ):
                            runtime_id = json.loads(payload_json).get(
                                "failed_runtime_id"
                            )
                        if runtime_id is not None:
                            labels["runtime_id"] = str(runtime_id)
                            runtime_row = connection.execute(
                                "SELECT name FROM runtime_endpoints WHERE id = ?",
                                (str(runtime_id),),
                            ).fetchone()
                            labels["runtime_name"] = (
                                str(runtime_row["name"])
                                if runtime_row is not None
                                else (
                                    "기본 Runtime"
                                    if str(runtime_id) == "builtin"
                                    else str(runtime_id)
                                )
                            )
                    media_row = connection.execute(
                        """
                        SELECT audio.duration_seconds
                        FROM jobs AS job
                        LEFT JOIN audio_revisions AS audio
                          ON audio.id = job.audio_revision_id
                        WHERE job.id = ?
                        """,
                        (job_id,),
                    ).fetchone()
                    duration_bucket = media_duration_bucket_minutes(
                        (
                            float(media_row["duration_seconds"])
                            if media_row is not None
                            and media_row["duration_seconds"] is not None
                            else None
                        )
                    )
                    if duration_bucket is not None:
                        labels["media_duration_bucket_minutes"] = (
                            duration_bucket
                        )
                    stage_duration_observation = (
                        max(
                            0.0,
                            event_created_at - float(started["created_at"]),
                        ),
                        labels,
                    )
        if stage_duration_observation is not None:
            duration_seconds, labels = stage_duration_observation
            try:
                self.record_operational_measurement(
                    "pipeline.stage.duration_seconds",
                    duration_seconds,
                    labels=labels,
                )
            except (OSError, RuntimeError, sqlite3.Error, ValueError):
                LOGGER.exception(
                    "pipeline stage duration measurement write failed"
                )
        self._notify_change(job_id)

    def events(self, job_id: str, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT level, message, event_code, from_state, to_state,
                       phase, attempt, correlation_id, payload_json, created_at
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
                "event_code": str(row["event_code"]),
                "from_state": (
                    str(row["from_state"])
                    if row["from_state"] is not None
                    else None
                ),
                "to_state": (
                    str(row["to_state"])
                    if row["to_state"] is not None
                    else None
                ),
                "phase": (
                    str(row["phase"])
                    if row["phase"] is not None
                    else None
                ),
                "attempt": (
                    int(row["attempt"])
                    if row["attempt"] is not None
                    else None
                ),
                "correlation_id": (
                    str(row["correlation_id"])
                    if row["correlation_id"] is not None
                    else None
                ),
                "payload": json.loads(str(row["payload_json"])),
                "created_at": float(row["created_at"]),
            }
            for row in reversed(rows)
        ]

    def media_duration_metrics(
        self,
        *,
        window_seconds: float = 30 * 24 * 60 * 60,
        end_at: float | None = None,
        phases: Collection[str] = ("transcription", "translation"),
    ) -> dict[str, Any]:
        """Group stage durations by nominal 15-minute media length."""

        if window_seconds <= 0:
            raise ValueError("metrics window must be positive")
        resolved_end = time.time() if end_at is None else float(end_at)
        if not math.isfinite(resolved_end):
            raise ValueError("metrics end time must be finite")
        resolved_start = resolved_end - window_seconds
        resolved_phases = tuple(dict.fromkeys(str(phase) for phase in phases))
        supported_phases = {
            phase.value for phase in JobPhase if phase is not JobPhase.COMPLETE
        }
        if not resolved_phases or not set(resolved_phases) <= supported_phases:
            raise ValueError("invalid metrics phase")

        event_codes = tuple(STAGE_DURATION_OUTCOMES)
        event_placeholders = ", ".join("?" for _ in event_codes)
        phase_placeholders = ", ".join("?" for _ in resolved_phases)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT terminal.event_code, terminal.phase,
                       terminal.payload_json, terminal.created_at,
                       started.created_at AS started_at,
                       started.payload_json AS started_payload_json,
                       audio.duration_seconds AS media_duration_seconds
                FROM job_events AS terminal
                LEFT JOIN job_events AS started
                  ON started.id = (
                      SELECT candidate.id
                      FROM job_events AS candidate
                      WHERE candidate.job_id = terminal.job_id
                        AND candidate.event_code = 'stage.started'
                        AND candidate.phase = terminal.phase
                        AND candidate.attempt = terminal.attempt - CASE
                            WHEN terminal.event_code =
                                 'transcription.runtime_failover'
                            THEN 1 ELSE 0 END
                        AND candidate.created_at <= terminal.created_at
                      ORDER BY candidate.id DESC
                      LIMIT 1
                  )
                LEFT JOIN jobs AS job ON job.id = terminal.job_id
                LEFT JOIN audio_revisions AS audio
                  ON audio.id = job.audio_revision_id
                WHERE terminal.event_code IN ({event_placeholders})
                  AND terminal.phase IN ({phase_placeholders})
                  AND terminal.created_at >= ?
                  AND terminal.created_at < ?
                ORDER BY terminal.created_at, terminal.id
                """,
                (
                    *event_codes,
                    *resolved_phases,
                    resolved_start,
                    resolved_end,
                ),
            ).fetchall()
            translation_pass_rows = connection.execute(
                """
                SELECT payload_json
                FROM job_events
                WHERE event_code = 'translation.pass.measured'
                  AND created_at >= ?
                  AND created_at < ?
                ORDER BY created_at, id
                """,
                (resolved_start, resolved_end),
            ).fetchall()
            runtime_names = {
                "builtin": "기본 Runtime",
                **{
                    str(row["id"]): str(row["name"])
                    for row in connection.execute(
                        "SELECT id, name FROM runtime_endpoints"
                    ).fetchall()
                },
            }

        groups: dict[
            tuple[int, str, str | None, str],
            dict[str, Any],
        ] = {}
        unmatched_terminal_events = 0
        missing_media_duration_events = 0
        for row in rows:
            if row["started_at"] is None:
                unmatched_terminal_events += 1
                continue
            media_duration = (
                float(row["media_duration_seconds"])
                if row["media_duration_seconds"] is not None
                else None
            )
            duration_bucket = media_duration_bucket_minutes(media_duration)
            if duration_bucket is None or media_duration is None:
                missing_media_duration_events += 1
                continue
            phase = str(row["phase"])
            event_code = str(row["event_code"])
            outcome = STAGE_DURATION_OUTCOMES[event_code]
            runtime_id: str | None = None
            if phase == JobPhase.TRANSCRIPTION.value:
                try:
                    started_payload = json.loads(
                        str(row["started_payload_json"])
                    )
                    runtime_value = started_payload.get("runtime_id")
                    if (
                        runtime_value is None
                        and event_code == "transcription.runtime_failover"
                    ):
                        runtime_value = json.loads(
                            str(row["payload_json"])
                        ).get("failed_runtime_id")
                    if runtime_value is not None:
                        runtime_id = str(runtime_value)
                except (AttributeError, json.JSONDecodeError, TypeError):
                    runtime_id = None
            processing_duration = max(
                0.0,
                float(row["created_at"]) - float(row["started_at"]),
            )
            key = (duration_bucket, phase, runtime_id, outcome)
            values = groups.setdefault(
                key,
                {
                    "sample_count": 0,
                    "processing_total_seconds": 0.0,
                    "processing_samples": [],
                    "processing_minimum_seconds": processing_duration,
                    "processing_maximum_seconds": 0.0,
                    "media_total_seconds": 0.0,
                    "media_minimum_seconds": media_duration,
                    "media_maximum_seconds": media_duration,
                },
            )
            values["sample_count"] = int(values["sample_count"]) + 1
            values["processing_total_seconds"] = (
                float(values["processing_total_seconds"])
                + processing_duration
            )
            values["processing_samples"].append(processing_duration)
            values["processing_minimum_seconds"] = min(
                float(values["processing_minimum_seconds"]),
                processing_duration,
            )
            values["processing_maximum_seconds"] = max(
                float(values["processing_maximum_seconds"]),
                processing_duration,
            )
            values["media_total_seconds"] = (
                float(values["media_total_seconds"]) + media_duration
            )
            values["media_minimum_seconds"] = min(
                float(values["media_minimum_seconds"]),
                media_duration,
            )
            values["media_maximum_seconds"] = max(
                float(values["media_maximum_seconds"]),
                media_duration,
            )

        group_values = []
        for (bucket, phase, runtime_id, outcome), values in sorted(
            groups.items(),
            key=lambda item: (
                item[0][0],
                item[0][1],
                item[0][2] or "",
                item[0][3],
            ),
        ):
            sample_count = int(values["sample_count"])
            processing_total = float(values["processing_total_seconds"])
            media_total = float(values["media_total_seconds"])
            processing_samples = sorted(values["processing_samples"])
            group_values.append(
                {
                    "media_duration_bucket_minutes": bucket,
                    "phase": phase,
                    "runtime_id": runtime_id,
                    "runtime_name": (
                        runtime_names.get(runtime_id, runtime_id)
                        if runtime_id is not None
                        else None
                    ),
                    "outcome": outcome,
                    "sample_count": sample_count,
                    "processing_total_seconds": round(processing_total, 3),
                    "processing_average_seconds": round(
                        processing_total / sample_count,
                        3,
                    ),
                    "processing_minimum_seconds": round(
                        float(values["processing_minimum_seconds"]),
                        3,
                    ),
                    "processing_p50_seconds": round(
                        self._percentile(processing_samples, 0.50),
                        3,
                    ),
                    "processing_p95_seconds": round(
                        self._percentile(processing_samples, 0.95),
                        3,
                    ),
                    "processing_maximum_seconds": round(
                        float(values["processing_maximum_seconds"]),
                        3,
                    ),
                    "media_average_seconds": round(
                        media_total / sample_count,
                        3,
                    ),
                    "media_minimum_seconds": round(
                        float(values["media_minimum_seconds"]),
                        3,
                    ),
                    "media_maximum_seconds": round(
                        float(values["media_maximum_seconds"]),
                        3,
                    ),
                }
            )

        translation_pass_groups: dict[
            tuple[int, str, str],
            dict[str, Any],
        ] = {}
        invalid_translation_pass_events = 0
        for row in translation_pass_rows:
            try:
                payload = json.loads(str(row["payload_json"]))
                pass_name = str(payload["pass"])
                outcome = str(payload["outcome"])
                duration_bucket = int(
                    payload["media_duration_bucket_minutes"]
                )
                active_duration = max(
                    0.0,
                    float(payload["active_seconds"]),
                )
                request_seconds = max(
                    0.0,
                    float(payload["request_seconds"]),
                )
                request_count = max(0, int(payload["request_count"]))
                media_duration = float(payload["media_duration_seconds"])
                if pass_name not in {"draft", "review"}:
                    raise ValueError("invalid translation pass")
            except (
                KeyError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                invalid_translation_pass_events += 1
                continue
            values = translation_pass_groups.setdefault(
                (duration_bucket, pass_name, outcome),
                {
                    "active_samples": [],
                    "request_seconds": 0.0,
                    "request_count": 0,
                    "media_total_seconds": 0.0,
                    "media_minimum_seconds": media_duration,
                    "media_maximum_seconds": media_duration,
                },
            )
            values["active_samples"].append(active_duration)
            values["request_seconds"] += request_seconds
            values["request_count"] += request_count
            values["media_total_seconds"] += media_duration
            values["media_minimum_seconds"] = min(
                values["media_minimum_seconds"],
                media_duration,
            )
            values["media_maximum_seconds"] = max(
                values["media_maximum_seconds"],
                media_duration,
            )

        translation_pass_values = []
        for (bucket, pass_name, outcome), values in sorted(
            translation_pass_groups.items(),
            key=lambda item: item[0],
        ):
            active_samples = sorted(values["active_samples"])
            sample_count = len(active_samples)
            active_total = sum(active_samples)
            translation_pass_values.append(
                {
                    "media_duration_bucket_minutes": bucket,
                    "pass": pass_name,
                    "outcome": outcome,
                    "sample_count": sample_count,
                    "active_total_seconds": round(active_total, 3),
                    "active_average_seconds": round(
                        active_total / sample_count,
                        3,
                    ),
                    "active_minimum_seconds": round(
                        active_samples[0],
                        3,
                    ),
                    "active_p50_seconds": round(
                        self._percentile(active_samples, 0.50),
                        3,
                    ),
                    "active_p95_seconds": round(
                        self._percentile(active_samples, 0.95),
                        3,
                    ),
                    "active_maximum_seconds": round(
                        active_samples[-1],
                        3,
                    ),
                    "request_total_seconds": round(
                        values["request_seconds"],
                        3,
                    ),
                    "request_count": values["request_count"],
                    "media_average_seconds": round(
                        values["media_total_seconds"] / sample_count,
                        3,
                    ),
                    "media_minimum_seconds": round(
                        values["media_minimum_seconds"],
                        3,
                    ),
                    "media_maximum_seconds": round(
                        values["media_maximum_seconds"],
                        3,
                    ),
                }
            )
        return {
            "schema_version": 1,
            "generated_at": time.time(),
            "window_start": resolved_start,
            "window_end": resolved_end,
            "window_seconds": window_seconds,
            "bucket_interval_minutes": 15,
            "bucket_strategy": "nearest",
            "phases": list(resolved_phases),
            "unmatched_terminal_events": unmatched_terminal_events,
            "missing_media_duration_events": missing_media_duration_events,
            "groups": group_values,
            "invalid_translation_pass_events": (
                invalid_translation_pass_events
            ),
            "translation_passes": translation_pass_values,
        }

    @staticmethod
    def _percentile(values: Sequence[float], quantile: float) -> float:
        if not values:
            raise ValueError("percentile requires at least one value")
        position = (len(values) - 1) * quantile
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return float(values[lower])
        fraction = position - lower
        return (
            float(values[lower]) * (1 - fraction)
            + float(values[upper]) * fraction
        )

    def operational_metrics(
        self,
        *,
        window_seconds: float = 24 * 60 * 60,
    ) -> dict[str, Any]:
        if window_seconds <= 0:
            raise ValueError("metrics window must be positive")
        now = time.time()
        database_integrity = self.database_integrity()
        with self._connect() as connection:
            jobs = connection.execute(
                """
                SELECT phase, state, reason_code, attempt, status,
                       status_updated_at, lease_owner, lease_expires_at,
                       stt_job_id, job_stop_requested
                FROM jobs
                """
            ).fetchall()
            dependencies = connection.execute(
                """
                SELECT dependency, state, reason_code, updated_at
                FROM dependency_states
                ORDER BY dependency
                """
            ).fetchall()
            event_count_rows = connection.execute(
                """
                SELECT event_code, COUNT(*) AS event_count
                FROM job_events
                WHERE created_at >= ?
                GROUP BY event_code
                ORDER BY event_code
                """,
                (now - window_seconds,),
            ).fetchall()
            event_rows = connection.execute(
                """
                SELECT id, job_id, event_code, phase, attempt,
                       from_state, to_state, created_at
                FROM job_events
                WHERE created_at >= ?
                  AND (
                      event_code IN (
                          'stage.started', 'stage.completed',
                          'stage.blocked', 'stage.failed', 'stage.paused',
                          'job.stopped'
                      )
                      OR to_state = 'waiting'
                  )
                ORDER BY id
                """,
                (now - window_seconds,),
            ).fetchall()

        state_counts = {state.value: 0 for state in JobState}
        phase_counts = {phase.value: 0 for phase in JobPhase}
        phase_state_counts = {
            phase.value: {state.value: 0 for state in JobState}
            for phase in JobPhase
        }
        reason_counts: dict[str, int] = {}
        oldest_waiting_seconds: dict[str, float | None] = {
            phase.value: None for phase in JobPhase
        }
        active_leases = 0
        expired_running_leases = 0
        total_retries = 0
        max_attempt = 0
        remote_running_job_ids: list[str] = []
        remote_cancel_pending_job_ids: list[str] = []
        for row in jobs:
            phase = str(row["phase"])
            state = str(row["state"])
            state_counts[state] = state_counts.get(state, 0) + 1
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
            phase_states = phase_state_counts.setdefault(phase, {})
            phase_states[state] = phase_states.get(state, 0) + 1
            reason = row["reason_code"]
            if reason is not None:
                reason_key = str(reason)
                reason_counts[reason_key] = reason_counts.get(reason_key, 0) + 1
            attempt = int(row["attempt"])
            total_retries += max(0, attempt - 1)
            max_attempt = max(max_attempt, attempt)
            if state == JobState.WAITING.value:
                age = max(0.0, now - float(row["status_updated_at"]))
                previous = oldest_waiting_seconds.get(phase)
                oldest_waiting_seconds[phase] = (
                    age if previous is None else max(previous, age)
                )
            lease_expires_at = row["lease_expires_at"]
            if row["lease_owner"] is not None and lease_expires_at is not None:
                if float(lease_expires_at) > now:
                    active_leases += 1
                elif str(row["status"]) in RUNNING_JOB_STATUSES:
                    expired_running_leases += 1
            if (
                str(row["status"]) == "transcription_running"
                and row["stt_job_id"] is not None
            ):
                remote_job_id = str(row["stt_job_id"])
                remote_running_job_ids.append(remote_job_id)
                if bool(row["job_stop_requested"]):
                    remote_cancel_pending_job_ids.append(remote_job_id)

        event_code_counts = {
            str(row["event_code"]): int(row["event_count"])
            for row in event_count_rows
        }
        stage_metrics = {
            phase.value: {
                "started": 0,
                "outcomes": {},
                "wait_seconds": [],
                "processing_seconds": [],
            }
            for phase in JobPhase
            if phase is not JobPhase.COMPLETE
        }
        waiting_since: dict[tuple[str, int], float] = {}
        active_starts: dict[tuple[str, int, str], float] = {}
        terminal_codes = {
            "stage.completed",
            "stage.blocked",
            "stage.failed",
            "stage.paused",
            "job.stopped",
        }
        for row in event_rows:
            event_code = str(row["event_code"])
            attempt_value = row["attempt"]
            phase_value = row["phase"]
            if attempt_value is None:
                continue
            attempt = int(attempt_value)
            job_key = (str(row["job_id"]), attempt)
            created_at = float(row["created_at"])
            if event_code == "stage.started" and phase_value is not None:
                phase = str(phase_value)
                metrics = stage_metrics.get(phase)
                if metrics is not None:
                    metrics["started"] += 1
                    entered_waiting_at = waiting_since.pop(job_key, None)
                    if entered_waiting_at is not None:
                        metrics["wait_seconds"].append(
                            max(0.0, created_at - entered_waiting_at)
                        )
                    active_starts[(job_key[0], attempt, phase)] = created_at
            elif event_code in terminal_codes and phase_value is not None:
                phase = str(phase_value)
                metrics = stage_metrics.get(phase)
                if metrics is not None:
                    outcomes = metrics["outcomes"]
                    outcomes[event_code] = outcomes.get(event_code, 0) + 1
                    started_at = active_starts.pop(
                        (job_key[0], attempt, phase),
                        None,
                    )
                    if started_at is not None:
                        metrics["processing_seconds"].append(
                            max(0.0, created_at - started_at)
                        )
            if row["to_state"] == JobState.WAITING.value:
                waiting_since[job_key] = created_at

        def summarize(values: list[float]) -> dict[str, float | int | None]:
            if not values:
                return {"samples": 0, "average": None, "maximum": None}
            return {
                "samples": len(values),
                "average": round(sum(values) / len(values), 3),
                "maximum": round(max(values), 3),
            }

        summarized_stages = {
            phase: {
                "started": int(metrics["started"]),
                "outcomes": dict(sorted(metrics["outcomes"].items())),
                "wait_seconds": summarize(metrics["wait_seconds"]),
                "processing_seconds": summarize(
                    metrics["processing_seconds"]
                ),
            }
            for phase, metrics in stage_metrics.items()
        }
        return {
            "schema_version": 1,
            "generated_at": now,
            "window_seconds": window_seconds,
            "jobs": {
                "total": len(jobs),
                "by_state": state_counts,
                "by_phase": phase_counts,
                "by_phase_state": phase_state_counts,
                "by_reason": dict(sorted(reason_counts.items())),
                "oldest_waiting_seconds": oldest_waiting_seconds,
                "total_retries": total_retries,
                "max_attempt": max_attempt,
            },
            "leases": {
                "active": active_leases,
                "expired_running": expired_running_leases,
            },
            "database": database_integrity,
            "remote_stt": {
                "running_job_ids": sorted(remote_running_job_ids),
                "cancel_pending_job_ids": sorted(
                    remote_cancel_pending_job_ids
                ),
            },
            "dependencies": [
                {
                    "dependency": str(row["dependency"]),
                    "state": str(row["state"]),
                    "reason_code": (
                        str(row["reason_code"])
                        if row["reason_code"] is not None
                        else None
                    ),
                    "updated_at": float(row["updated_at"]),
                }
                for row in dependencies
            ],
            "events": {
                "by_code": dict(sorted(event_code_counts.items())),
                "stages": summarized_stages,
            },
            "media_duration_metrics": self.media_duration_metrics(
                window_seconds=window_seconds,
                end_at=now,
            ),
            "measurements": self.operational_measurements(),
        }

    def database_integrity(self) -> dict[str, Any]:
        with self._connect() as connection:
            foreign_keys = connection.execute(
                "PRAGMA foreign_keys"
            ).fetchone()
            quick_check = connection.execute(
                "PRAGMA quick_check(1)"
            ).fetchone()
            violations = connection.execute(
                "PRAGMA foreign_key_check"
            ).fetchall()
            migration_row = connection.execute(
                """
                SELECT COUNT(*) AS applied_count,
                       MAX(sequence) AS latest_sequence,
                       SUM(CASE WHEN sequence IS NULL THEN 1 ELSE 0 END)
                           AS unsequenced_count
                FROM schema_migrations
                """
            ).fetchone()
        check_result = (
            str(quick_check[0]) if quick_check is not None else "unavailable"
        )
        return {
            "foreign_keys_enabled": bool(
                foreign_keys is not None and int(foreign_keys[0]) == 1
            ),
            "quick_check": check_result,
            "foreign_key_violation_count": len(violations),
            "valid": check_result == "ok" and not violations,
            "migrations": {
                "applied_count": int(migration_row["applied_count"]),
                "latest_sequence": (
                    int(migration_row["latest_sequence"])
                    if migration_row["latest_sequence"] is not None
                    else None
                ),
                "unsequenced_count": int(
                    migration_row["unsequenced_count"] or 0
                ),
            },
        }

    def record_operational_measurement(
        self,
        metric: str,
        value: float,
        *,
        labels: Mapping[str, str | int | bool] | None = None,
    ) -> None:
        normalized_metric = metric.strip().lower()
        if (
            not normalized_metric
            or len(normalized_metric) > 100
            or EVENT_CODE_PATTERN.fullmatch(normalized_metric) is None
        ):
            raise ValueError("invalid operational metric")
        numeric_value = float(value)
        if not math.isfinite(numeric_value) or numeric_value < 0:
            raise ValueError(
                "operational metric value must be finite and nonnegative"
            )
        normalized_labels: dict[str, str | int | bool] = {}
        credential_suffixes = tuple(
            f"_{part}" for part in SENSITIVE_EVENT_KEY_PARTS
        )
        for key, label_value in dict(labels or {}).items():
            normalized_key = str(key).strip().lower()
            if (
                not normalized_key
                or len(normalized_key) > 50
                or EVENT_CODE_PATTERN.fullmatch(normalized_key) is None
            ):
                raise ValueError("invalid operational metric label")
            if (
                normalized_key in SENSITIVE_EVENT_KEY_PARTS
                or normalized_key.endswith(credential_suffixes)
            ):
                raise ValueError("sensitive metric labels are not allowed")
            if not isinstance(label_value, (str, int, bool)):
                raise ValueError("operational metric labels must be scalar")
            normalized_value = (
                label_value.strip()
                if isinstance(label_value, str)
                else label_value
            )
            if len(str(normalized_value)) > 100:
                raise ValueError("operational metric label is too long")
            normalized_labels[normalized_key] = normalized_value
        if len(normalized_labels) > 8:
            raise ValueError("operational metrics support at most 8 labels")
        labels_json = json.dumps(
            normalized_labels,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operational_measurements (
                    metric, labels_json, sample_count, total,
                    maximum, last_value, updated_at
                ) VALUES (?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(metric, labels_json) DO UPDATE SET
                    sample_count = sample_count + 1,
                    total = total + excluded.last_value,
                    maximum = MAX(maximum, excluded.last_value),
                    last_value = excluded.last_value,
                    updated_at = excluded.updated_at
                """,
                (
                    normalized_metric,
                    labels_json,
                    numeric_value,
                    numeric_value,
                    numeric_value,
                    now,
                ),
            )

    def operational_measurements(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT metric, labels_json, sample_count, total,
                       maximum, last_value, updated_at
                FROM operational_measurements
                ORDER BY metric, labels_json
                """
            ).fetchall()
        return [
            {
                "metric": str(row["metric"]),
                "labels": json.loads(str(row["labels_json"])),
                "sample_count": int(row["sample_count"]),
                "total": float(row["total"]),
                "maximum": float(row["maximum"]),
                "last_value": float(row["last_value"]),
                "updated_at": float(row["updated_at"]),
            }
            for row in rows
        ]
