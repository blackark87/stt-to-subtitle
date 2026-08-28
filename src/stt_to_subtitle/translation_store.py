"""Persistent server registry for one translation stage."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4


BUILTIN_TRANSLATION_SERVER_ID = "builtin"


@dataclass(frozen=True)
class TranslationServer:
    id: str
    name: str
    base_url: str
    token: str
    enabled: bool
    capacity: int
    builtin: bool
    batch_preferred: bool
    selected_model: str
    models: tuple[str, ...]
    checked_at: float | None
    created_at: float
    updated_at: float


class TranslationServerGroupStore:
    """Own the servers for one translation stage."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS translation_servers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    base_url TEXT NOT NULL UNIQUE,
                    token TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    capacity INTEGER NOT NULL DEFAULT 1,
                    builtin INTEGER NOT NULL DEFAULT 0,
                    batch_preferred INTEGER NOT NULL DEFAULT 0,
                    selected_model TEXT NOT NULL DEFAULT '',
                    models_json TEXT NOT NULL DEFAULT '[]',
                    checked_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    CHECK (enabled IN (0, 1)),
                    CHECK (builtin IN (0, 1)),
                    CHECK (batch_preferred IN (0, 1)),
                    CHECK (capacity BETWEEN 1 AND 8)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS
                    translation_builtin_server_idx
                ON translation_servers(builtin)
                WHERE builtin = 1;
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(translation_servers)"
                ).fetchall()
            }
            if "selected_model" not in columns:
                connection.execute(
                    "ALTER TABLE translation_servers "
                    "ADD COLUMN selected_model TEXT NOT NULL DEFAULT ''"
                )
                self._migrate_group_model(connection)
            connection.execute("DROP TABLE IF EXISTS translation_group_settings")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _migrate_group_model(connection: sqlite3.Connection) -> None:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'translation_group_settings'"
        ).fetchone()
        legacy = (
            connection.execute(
                "SELECT value FROM translation_group_settings WHERE key = 'model'"
            ).fetchone()
            if table is not None
            else None
        )
        legacy_model = str(legacy["value"]).strip() if legacy is not None else ""
        rows = connection.execute(
            "SELECT id, models_json FROM translation_servers"
        ).fetchall()
        for row in rows:
            try:
                parsed = json.loads(str(row["models_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = []
            models = [
                str(model).strip()
                for model in parsed
                if str(model).strip()
            ] if isinstance(parsed, list) else []
            selected = (
                legacy_model
                if legacy_model and legacy_model in models
                else models[0] if len(models) == 1 else ""
            )
            if selected:
                connection.execute(
                    "UPDATE translation_servers SET selected_model = ? WHERE id = ?",
                    (selected, str(row["id"])),
                )

    @staticmethod
    def _server(row: sqlite3.Row) -> TranslationServer:
        try:
            parsed_models = json.loads(str(row["models_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed_models = []
        models = tuple(
            str(model).strip()
            for model in parsed_models
            if str(model).strip()
        ) if isinstance(parsed_models, list) else ()
        return TranslationServer(
            id=str(row["id"]),
            name=str(row["name"]),
            base_url=str(row["base_url"]),
            token=str(row["token"]),
            enabled=bool(row["enabled"]),
            capacity=int(row["capacity"]),
            builtin=bool(row["builtin"]),
            batch_preferred=bool(row["batch_preferred"]),
            selected_model=str(row["selected_model"]),
            models=models,
            checked_at=(
                float(row["checked_at"])
                if row["checked_at"] is not None
                else None
            ),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def is_empty(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM translation_servers"
            ).fetchone()
        return row is None or int(row["count"]) == 0

    def sync_builtin(
        self,
        *,
        name: str,
        base_url: str,
        token: str,
        enabled: bool,
        capacity: int,
        batch_preferred: bool,
        legacy_names: Sequence[str] = (),
    ) -> None:
        now = time.time()
        resolved_batch_preferred = enabled and batch_preferred
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO translation_servers (
                    id, name, base_url, token, enabled, capacity, builtin,
                    batch_preferred, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (
                    BUILTIN_TRANSLATION_SERVER_ID,
                    name,
                    base_url,
                    token,
                    int(enabled),
                    capacity,
                    int(resolved_batch_preferred),
                    now,
                    now,
                ),
            )
            if legacy_names:
                placeholders = ", ".join("?" for _ in legacy_names)
                connection.execute(
                    f"""
                    UPDATE translation_servers
                    SET name = ?, updated_at = ?
                    WHERE id = ? AND name IN ({placeholders})
                    """,
                    (
                        name,
                        now,
                        BUILTIN_TRANSLATION_SERVER_ID,
                        *legacy_names,
                    ),
                )

    def list(self) -> list[TranslationServer]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM translation_servers
                ORDER BY builtin DESC, created_at, name COLLATE NOCASE
                """
            ).fetchall()
        return [self._server(row) for row in rows]

    def get(self, server_id: str) -> TranslationServer | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM translation_servers WHERE id = ?",
                (server_id,),
            ).fetchone()
        return self._server(row) if row is not None else None

    def create(
        self,
        *,
        name: str,
        base_url: str,
        token: str,
        enabled: bool,
        capacity: int,
        server_id: str | None = None,
        batch_preferred: bool = False,
        selected_model: str = "",
        models: tuple[str, ...] = (),
        checked_at: float | None = None,
    ) -> TranslationServer:
        resolved_id = server_id or uuid4().hex
        now = time.time()
        resolved_batch_preferred = enabled and batch_preferred
        resolved_selected_model = selected_model.strip()
        if resolved_selected_model and resolved_selected_model not in models:
            raise ValueError("선택한 모델을 번역 서버가 제공하지 않습니다.")
        with self._connect() as connection:
            if resolved_batch_preferred:
                connection.execute(
                    "UPDATE translation_servers SET batch_preferred = 0, updated_at = ?",
                    (now,),
                )
            connection.execute(
                """
                INSERT INTO translation_servers (
                    id, name, base_url, token, enabled, capacity, builtin,
                    batch_preferred, selected_model, models_json, checked_at,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resolved_id,
                    name,
                    base_url,
                    token,
                    int(enabled),
                    capacity,
                    int(resolved_batch_preferred),
                    resolved_selected_model,
                    json.dumps(list(models), ensure_ascii=False),
                    checked_at,
                    now,
                    now,
                ),
            )
        server = self.get(resolved_id)
        if server is None:
            raise RuntimeError("translation server was not created")
        return server

    def update(
        self,
        server_id: str,
        *,
        name: str,
        base_url: str,
        token: str,
        enabled: bool,
        capacity: int,
    ) -> TranslationServer:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE translation_servers
                SET name = ?,
                    models_json = CASE WHEN base_url != ? THEN '[]' ELSE models_json END,
                    selected_model = CASE WHEN base_url != ? THEN '' ELSE selected_model END,
                    checked_at = CASE WHEN base_url != ? THEN NULL ELSE checked_at END,
                    base_url = ?, token = ?, enabled = ?,
                    batch_preferred = CASE WHEN ? THEN batch_preferred ELSE 0 END,
                    capacity = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    base_url,
                    base_url,
                    base_url,
                    base_url,
                    token,
                    int(enabled),
                    int(enabled),
                    capacity,
                    time.time(),
                    server_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("번역 서버를 찾을 수 없습니다.")
        server = self.get(server_id)
        if server is None:
            raise RuntimeError("translation server disappeared")
        return server

    def delete(self, server_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM translation_servers WHERE id = ? AND builtin = 0",
                (server_id,),
            )
        return cursor.rowcount == 1

    def set_routing(
        self,
        server_id: str,
        *,
        enabled: bool,
        batch_preferred: bool,
    ) -> TranslationServer:
        now = time.time()
        resolved_batch_preferred = enabled and batch_preferred
        with self._connect() as connection:
            if resolved_batch_preferred:
                connection.execute(
                    "UPDATE translation_servers SET batch_preferred = 0, updated_at = ?",
                    (now,),
                )
            cursor = connection.execute(
                """
                UPDATE translation_servers
                SET enabled = ?, batch_preferred = ?, updated_at = ?
                WHERE id = ?
                """,
                (int(enabled), int(resolved_batch_preferred), now, server_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("번역 서버를 찾을 수 없습니다.")
        server = self.get(server_id)
        if server is None:
            raise RuntimeError("translation server disappeared")
        return server

    def save_models(
        self,
        server_id: str,
        models: list[str],
    ) -> TranslationServer:
        now = time.time()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT selected_model FROM translation_servers WHERE id = ?",
                (server_id,),
            ).fetchone()
            if current is None:
                raise ValueError("번역 서버를 찾을 수 없습니다.")
            current_model = str(current["selected_model"])
            selected_model = (
                current_model
                if current_model in models
                else models[0] if len(models) == 1 else ""
            )
            cursor = connection.execute(
                """
                UPDATE translation_servers
                SET models_json = ?, selected_model = ?,
                    checked_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(models, ensure_ascii=False),
                    selected_model,
                    now,
                    now,
                    server_id,
                ),
            )
        if cursor.rowcount != 1:
            raise ValueError("번역 서버를 찾을 수 없습니다.")
        server = self.get(server_id)
        if server is None:
            raise RuntimeError("translation server disappeared")
        return server

    def set_selected_model(
        self,
        server_id: str,
        model: str,
    ) -> TranslationServer:
        normalized = model.strip()
        if not normalized:
            raise ValueError("번역 모델을 선택해야 합니다.")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT models_json FROM translation_servers WHERE id = ?",
                (server_id,),
            ).fetchone()
            if row is None:
                raise ValueError("번역 서버를 찾을 수 없습니다.")
            try:
                parsed = json.loads(str(row["models_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = []
            models = {
                str(item).strip()
                for item in parsed
                if str(item).strip()
            } if isinstance(parsed, list) else set()
            if normalized not in models:
                raise ValueError("선택한 모델을 번역 서버가 제공하지 않습니다.")
            connection.execute(
                """
                UPDATE translation_servers
                SET selected_model = ?, updated_at = ?
                WHERE id = ?
                """,
                (normalized, time.time(), server_id),
            )
        server = self.get(server_id)
        if server is None:
            raise RuntimeError("translation server disappeared")
        return server


def migrate_legacy_translation_endpoints(
    legacy_path: Path,
    *,
    draft_store: TranslationServerGroupStore,
    review_store: TranslationServerGroupStore,
) -> None:
    """Copy the previous shared registry once into the two independent groups."""
    if (
        not legacy_path.is_file()
        or not draft_store.is_empty()
        or not review_store.is_empty()
    ):
        return
    connection = sqlite3.connect(legacy_path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'translation_endpoints'"
        ).fetchone()
        if table is None:
            return
        rows = connection.execute(
            "SELECT * FROM translation_endpoints "
            "ORDER BY builtin DESC, created_at"
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        if bool(row["builtin"]):
            continue
        try:
            models_value = json.loads(str(row["models_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            models_value = []
        models = (
            tuple(str(value) for value in models_value)
            if isinstance(models_value, list)
            else ()
        )
        common = {
            "name": str(row["name"]),
            "base_url": str(row["base_url"]),
            "token": str(row["token"]),
            "enabled": bool(row["enabled"]),
            "capacity": int(row["capacity"]),
            "server_id": str(row["id"]),
            "batch_preferred": bool(row["batch_preferred"]),
            "models": models,
            "checked_at": (
                float(row["checked_at"])
                if row["checked_at"] is not None
                else None
            ),
        }
        draft_model = str(row["draft_model"]).strip()
        if draft_model:
            draft_store.create(
                **common,
                selected_model=(
                    draft_model
                    if draft_model in models
                    else models[0] if len(models) == 1 else ""
                ),
            )
        review_model = str(row["review_model"]).strip()
        if bool(row["review_enabled"]) and review_model:
            review_store.create(
                **common,
                selected_model=(
                    review_model
                    if review_model in models
                    else models[0] if len(models) == 1 else ""
                ),
            )
