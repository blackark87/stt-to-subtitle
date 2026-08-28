"""Persistent registry for OpenAI-compatible translation endpoints."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


BUILTIN_TRANSLATION_ENDPOINT_ID = "builtin"


@dataclass(frozen=True)
class TranslationEndpoint:
    id: str
    name: str
    base_url: str
    token: str
    enabled: bool
    capacity: int
    builtin: bool
    draft_model: str
    review_model: str
    review_enabled: bool
    batch_preferred: bool
    models: tuple[str, ...]
    checked_at: float | None
    created_at: float
    updated_at: float


class TranslationEndpointStore:
    """Own translation endpoint topology outside the Backend job database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS translation_endpoints (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL UNIQUE,
                    token TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    capacity INTEGER NOT NULL DEFAULT 1,
                    builtin INTEGER NOT NULL DEFAULT 0,
                    draft_model TEXT NOT NULL DEFAULT '',
                    review_model TEXT NOT NULL DEFAULT '',
                    review_enabled INTEGER NOT NULL DEFAULT 0,
                    batch_preferred INTEGER NOT NULL DEFAULT 0,
                    models_json TEXT NOT NULL DEFAULT '[]',
                    checked_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    CHECK (enabled IN (0, 1)),
                    CHECK (builtin IN (0, 1)),
                    CHECK (review_enabled IN (0, 1)),
                    CHECK (batch_preferred IN (0, 1)),
                    CHECK (capacity BETWEEN 1 AND 8)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    translation_builtin_endpoint_idx
                ON translation_endpoints(builtin)
                WHERE builtin = 1;
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(translation_endpoints)"
                ).fetchall()
            }
            if "review_enabled" not in columns:
                connection.execute(
                    "ALTER TABLE translation_endpoints ADD COLUMN "
                    "review_enabled INTEGER NOT NULL DEFAULT 0"
                )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _endpoint(row: sqlite3.Row) -> TranslationEndpoint:
        try:
            parsed_models = json.loads(str(row["models_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_models = []
        models = tuple(
            str(model).strip()
            for model in parsed_models
            if str(model).strip()
        ) if isinstance(parsed_models, list) else ()
        return TranslationEndpoint(
            id=str(row["id"]),
            name=str(row["name"]),
            base_url=str(row["base_url"]),
            token=str(row["token"]),
            enabled=bool(row["enabled"]),
            capacity=int(row["capacity"]),
            builtin=bool(row["builtin"]),
            draft_model=str(row["draft_model"]),
            review_model=str(row["review_model"]),
            review_enabled=bool(row["review_enabled"]),
            batch_preferred=bool(row["batch_preferred"]),
            models=models,
            checked_at=(
                float(row["checked_at"])
                if row["checked_at"] is not None
                else None
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def sync_builtin(
        self,
        *,
        name: str,
        base_url: str,
        token: str,
        capacity: int,
        draft_model: str,
        review_model: str,
        review_enabled: bool,
        batch_preferred: bool,
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO translation_endpoints (
                    id, name, base_url, token, enabled, capacity, builtin,
                    draft_model, review_model, review_enabled,
                    batch_preferred,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    models_json = CASE
                        WHEN translation_endpoints.base_url != excluded.base_url
                        THEN '[]'
                        ELSE translation_endpoints.models_json
                    END,
                    checked_at = CASE
                        WHEN translation_endpoints.base_url != excluded.base_url
                        THEN NULL
                        ELSE translation_endpoints.checked_at
                    END,
                    base_url = excluded.base_url,
                    token = excluded.token,
                    enabled = 1,
                    capacity = excluded.capacity,
                    builtin = 1,
                    updated_at = excluded.updated_at
                """,
                (
                    BUILTIN_TRANSLATION_ENDPOINT_ID,
                    name,
                    base_url,
                    token,
                    capacity,
                    draft_model,
                    review_model,
                    int(review_enabled),
                    int(batch_preferred),
                    now,
                    now,
                ),
            )

    def remove_builtin(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM translation_endpoints WHERE builtin = 1"
            )

    def list(self) -> list[TranslationEndpoint]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM translation_endpoints
                ORDER BY builtin DESC, created_at, name COLLATE NOCASE
                """
            ).fetchall()
        return [self._endpoint(row) for row in rows]

    def get(self, endpoint_id: str) -> TranslationEndpoint | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM translation_endpoints WHERE id = ?",
                (endpoint_id,),
            ).fetchone()
        return self._endpoint(row) if row is not None else None

    def create(
        self,
        *,
        name: str,
        base_url: str,
        token: str,
        enabled: bool,
        capacity: int,
    ) -> TranslationEndpoint:
        endpoint_id = uuid4().hex
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO translation_endpoints (
                    id, name, base_url, token, enabled, capacity, builtin,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    endpoint_id,
                    name,
                    base_url,
                    token,
                    int(enabled),
                    capacity,
                    now,
                    now,
                ),
            )
        endpoint = self.get(endpoint_id)
        if endpoint is None:
            raise RuntimeError("translation endpoint was not created")
        return endpoint

    def update(
        self,
        endpoint_id: str,
        *,
        name: str,
        base_url: str,
        token: str,
        enabled: bool,
        capacity: int,
    ) -> TranslationEndpoint:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE translation_endpoints
                SET name = ?, base_url = ?, token = ?, enabled = ?,
                    capacity = ?, updated_at = ?
                WHERE id = ? AND builtin = 0
                """,
                (
                    name,
                    base_url,
                    token,
                    int(enabled),
                    capacity,
                    time.time(),
                    endpoint_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("추가 번역 서버를 찾을 수 없습니다.")
        endpoint = self.get(endpoint_id)
        if endpoint is None:
            raise RuntimeError("translation endpoint disappeared")
        return endpoint

    def delete(self, endpoint_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM translation_endpoints "
                "WHERE id = ? AND builtin = 0",
                (endpoint_id,),
            )
        return cursor.rowcount == 1

    def set_models(
        self,
        endpoint_id: str,
        *,
        draft_model: str,
        review_model: str,
        review_enabled: bool,
        batch_preferred: bool,
    ) -> TranslationEndpoint:
        with self._connect() as connection:
            if batch_preferred:
                connection.execute(
                    "UPDATE translation_endpoints "
                    "SET batch_preferred = 0, updated_at = ?",
                    (time.time(),),
                )
            cursor = connection.execute(
                """
                UPDATE translation_endpoints
                SET draft_model = ?, review_model = ?,
                    review_enabled = ?, batch_preferred = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    draft_model,
                    review_model,
                    int(review_enabled),
                    int(batch_preferred),
                    time.time(),
                    endpoint_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("번역 서버를 찾을 수 없습니다.")
        endpoint = self.get(endpoint_id)
        if endpoint is None:
            raise RuntimeError("translation endpoint disappeared")
        return endpoint

    def save_models(
        self,
        endpoint_id: str,
        models: list[str],
    ) -> TranslationEndpoint:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE translation_endpoints
                SET models_json = ?, checked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(models, ensure_ascii=False),
                    time.time(),
                    time.time(),
                    endpoint_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("번역 서버를 찾을 수 없습니다.")
        endpoint = self.get(endpoint_id)
        if endpoint is None:
            raise RuntimeError("translation endpoint disappeared")
        return endpoint
