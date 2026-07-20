from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from importlib import import_module
from pathlib import Path
from typing import Any

DEFAULT_SQLITE_FILE = ".sykit-tasks.sqlite3"
STORE_METHODS = (
    "enqueue",
    "enqueue_scheduled",
    "claim",
    "heartbeat",
    "complete",
    "fail",
    "release",
    "ready",
)


class TaskStore:
    """Persistence interface used by the background task runner."""

    def enqueue(
        self,
        task_name: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> str:
        raise NotImplementedError

    def enqueue_scheduled(
        self,
        task_name: str,
        args: list[Any],
        kwargs: dict[str, Any],
        schedule_key: str,
    ) -> str | None:
        raise NotImplementedError

    def claim(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        raise NotImplementedError

    def heartbeat(self, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        raise NotImplementedError

    def complete(self, task_id: str, worker_id: str) -> bool:
        raise NotImplementedError

    def fail(self, task_id: str, worker_id: str, error: str) -> bool:
        raise NotImplementedError

    def release(self, task_id: str, worker_id: str) -> bool:
        raise NotImplementedError

    def ready(self) -> None:
        raise NotImplementedError


class SqliteTaskStore(TaskStore):
    """A process-safe queue in one sqlite database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._schema_ready = False
        self._schema_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA synchronous=NORMAL")
        if not self._schema_ready:
            with self._schema_lock:
                if not self._schema_ready:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS sykit_tasks (
                            task_id TEXT PRIMARY KEY,
                            task_name TEXT NOT NULL,
                            args TEXT NOT NULL,
                            kwargs TEXT NOT NULL,
                            status TEXT NOT NULL,
                            created REAL NOT NULL,
                            available REAL NOT NULL,
                            claimed_by TEXT,
                            lease_until REAL,
                            attempts INTEGER NOT NULL DEFAULT 0,
                            finished REAL,
                            last_error TEXT
                        );
                        CREATE INDEX IF NOT EXISTS sykit_tasks_claim
                            ON sykit_tasks (status, available, lease_until, created);
                        CREATE TABLE IF NOT EXISTS sykit_schedule_runs (
                            schedule_key TEXT PRIMARY KEY,
                            created REAL NOT NULL
                        );
                        """
                    )
                    connection.commit()
                    self._schema_ready = True
        return connection

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    def _insert(
        self,
        connection: sqlite3.Connection,
        task_name: str,
        args: list[Any],
        kwargs: dict[str, Any],
        now: float,
    ) -> str:
        task_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO sykit_tasks "
            "(task_id, task_name, args, kwargs, status, created, available) "
            "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
            (
                task_id,
                task_name,
                self._json(args),
                self._json(kwargs),
                now,
                now,
            ),
        )
        return task_id

    def enqueue(
        self,
        task_name: str,
        args: list[Any],
        kwargs: dict[str, Any],
    ) -> str:
        connection = self._connect()
        try:
            task_id = self._insert(connection, task_name, args, kwargs, time.time())
            connection.commit()
            return task_id
        finally:
            connection.close()

    def enqueue_scheduled(
        self,
        task_name: str,
        args: list[Any],
        kwargs: dict[str, Any],
        schedule_key: str,
    ) -> str | None:
        connection = self._connect()
        try:
            now = time.time()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM sykit_schedule_runs WHERE created < ?",
                (now - 172_800,),
            )
            cursor = connection.execute(
                "INSERT OR IGNORE INTO sykit_schedule_runs "
                "(schedule_key, created) VALUES (?, ?)",
                (schedule_key, now),
            )
            if cursor.rowcount == 0:
                connection.commit()
                return None
            task_id = self._insert(connection, task_name, args, kwargs, now)
            connection.commit()
            return task_id
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim(self, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            now = time.time()
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT task_id, task_name, args, kwargs, attempts "
                "FROM sykit_tasks "
                "WHERE (status = 'queued' AND available <= ?) "
                "OR (status = 'running' AND lease_until <= ?) "
                "ORDER BY available, created, task_id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            cursor = connection.execute(
                "UPDATE sykit_tasks SET status = 'running', claimed_by = ?, "
                "lease_until = ?, attempts = attempts + 1 "
                "WHERE task_id = ? AND ((status = 'queued' AND available <= ?) "
                "OR (status = 'running' AND lease_until <= ?))",
                (worker_id, now + lease_seconds, row[0], now, now),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
            return {
                "id": row[0],
                "task": row[1],
                "args": json.loads(row[2]),
                "kwargs": json.loads(row[3]),
                "attempt": int(row[4]) + 1,
            }
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def heartbeat(self, task_id: str, worker_id: str, lease_seconds: int) -> bool:
        connection = self._connect()
        try:
            cursor = connection.execute(
                "UPDATE sykit_tasks SET lease_until = ? "
                "WHERE task_id = ? AND status = 'running' AND claimed_by = ?",
                (time.time() + lease_seconds, task_id, worker_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def complete(self, task_id: str, worker_id: str) -> bool:
        connection = self._connect()
        try:
            cursor = connection.execute(
                "DELETE FROM sykit_tasks "
                "WHERE task_id = ? AND status = 'running' AND claimed_by = ?",
                (task_id, worker_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def fail(self, task_id: str, worker_id: str, error: str) -> bool:
        connection = self._connect()
        try:
            cursor = connection.execute(
                "UPDATE sykit_tasks SET status = 'failed', claimed_by = NULL, "
                "lease_until = NULL, finished = ?, last_error = ? "
                "WHERE task_id = ? AND status = 'running' AND claimed_by = ?",
                (time.time(), error[:4000], task_id, worker_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def release(self, task_id: str, worker_id: str) -> bool:
        connection = self._connect()
        try:
            cursor = connection.execute(
                "UPDATE sykit_tasks SET status = 'queued', claimed_by = NULL, "
                "lease_until = NULL, available = ? "
                "WHERE task_id = ? AND status = 'running' AND claimed_by = ?",
                (time.time(), task_id, worker_id),
            )
            connection.commit()
            return cursor.rowcount == 1
        finally:
            connection.close()

    def ready(self) -> None:
        connection = self._connect()
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()


def resolve_task_store(spec: Any, default_dir: Path) -> TaskStore:
    """Resolve the task-store setting into a built-in or packaged store."""
    if spec is None:
        spec = ""
    if not isinstance(spec, str):
        raise RuntimeError('The "task-store" setting must be a string.')
    text = spec.strip()
    scheme, _, target = text.partition(":")
    scheme = scheme.strip().casefold()
    target = target.strip()
    if not text or scheme == "sqlite":
        path = Path(target) if target else Path(DEFAULT_SQLITE_FILE)
        if not path.is_absolute():
            path = default_dir / path
        return SqliteTaskStore(path)
    if not scheme.isidentifier():
        raise RuntimeError(f'Invalid "task-store" scheme {scheme!r}.')
    module_name = f"core._taskstore_{scheme}"
    try:
        module = import_module(module_name)
    except ImportError as error:
        raise RuntimeError(
            f'Unknown task store "{scheme}". Install a package that provides '
            f'files/core/_taskstore_{scheme}.py, or use "sqlite" or "".'
        ) from error
    factory = getattr(module, "create", None)
    if not callable(factory):
        raise RuntimeError(f"{module_name} must define a create(target) function.")
    store = factory(target)
    missing = [
        name for name in STORE_METHODS if not callable(getattr(store, name, None))
    ]
    if missing:
        raise RuntimeError(
            f"The {scheme!r} task store is missing: {', '.join(missing)}."
        )
    return store


__all__ = [
    "SqliteTaskStore",
    "TaskStore",
    "resolve_task_store",
]
