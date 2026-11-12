from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    job_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    negative_prompt TEXT,
    steps INTEGER,
    seed INTEGER,
    width INTEGER NOT NULL DEFAULT 1024,
    height INTEGER NOT NULL DEFAULT 1024,
    layout_json TEXT,
    lora_filename TEXT,
    lora_strength REAL,
    lora_disabled INTEGER NOT NULL DEFAULT 0,
    comfy_prompt_id TEXT,
    queue_position INTEGER,
    result_path TEXT,
    result_media_type TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    completed_at TEXT,
    expires_at TEXT,
    last_checked_at TEXT,
    auto_requeue_count INTEGER NOT NULL DEFAULT 0,
    download_retry_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks (created_at DESC);
"""

_TASK_FIELDS = (
    "job_id",
    "status",
    "prompt",
    "negative_prompt",
    "steps",
    "seed",
    "width",
    "height",
    "layout_json",
    "lora_filename",
    "lora_strength",
    "lora_disabled",
    "comfy_prompt_id",
    "queue_position",
    "result_path",
    "result_media_type",
    "error_message",
    "created_at",
    "submitted_at",
    "completed_at",
    "expires_at",
    "last_checked_at",
    "auto_requeue_count",
    "download_retry_count",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class TaskStore:
    """本地单人任务的轻量 SQLite 存储。"""

    def __init__(self, database_file: Path):
        self.database_file = database_file
        self.database_file.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_file, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def create_task(self, **fields: Any) -> str:
        columns = [key for key in _TASK_FIELDS if key in fields]
        placeholders = ", ".join("?" for _ in columns)
        values = [fields[key] for key in columns]
        with self._connect() as connection:
            connection.execute(
                f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        return str(fields["job_id"])

    def get_task(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE job_id = ?", (job_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_tasks(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, job_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE status IN ('queued','submitted','running') "
                "ORDER BY created_at ASC, job_id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def tasks_with_prompt_id(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE comfy_prompt_id IS NOT NULL "
                "AND status IN ('submitted','running') ORDER BY created_at ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def update_task(self, job_id: str, **fields: Any) -> bool:
        columns = [key for key in _TASK_FIELDS if key in fields]
        if not columns:
            return True
        assignments = ", ".join(f"{key} = ?" for key in columns)
        values = [fields[key] for key in columns] + [job_id]
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {assignments} WHERE job_id = ?", values
            )
        return cursor.rowcount > 0

    def delete_task(self, job_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM tasks WHERE job_id = ?", (job_id,)
            )
        return cursor.rowcount > 0

    def delete_tasks(self, job_ids: list[str]) -> int:
        if not job_ids:
            return 0
        placeholders = ", ".join("?" for _ in job_ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM tasks WHERE job_id IN ({placeholders})", job_ids
            )
        return cursor.rowcount

    def expired_job_ids(self, now: datetime) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM tasks WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (iso(now),),
            ).fetchall()
        return [str(row["job_id"]) for row in rows]
