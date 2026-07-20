from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from sykit._schema import SCHEMA_TABLE

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "files" / "core"


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CORE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ApiKeyStore = load_module("sykit_schema_test_apikeys", "_apikeys.py").SqliteApiKeyStore
RateLimiter = load_module("sykit_schema_test_limits", "_limits.py").RateLimiter
SessionStore = load_module(
    "sykit_schema_test_sessions", "_sessions.py"
).SqliteSessionStore
TaskStore = load_module("sykit_schema_test_tasks", "_task_store.py").SqliteTaskStore


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="sykit-schema-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _versions(self, path: Path) -> dict[str, int]:
        connection = sqlite3.connect(path)
        try:
            return dict(
                connection.execute(
                    f"SELECT component, version FROM {SCHEMA_TABLE}"
                ).fetchall()
            )
        finally:
            connection.close()

    def test_0122_session_database_is_adopted_without_data_loss(self) -> None:
        path = self.root / "sessions.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                """
                CREATE TABLE sykit_sessions (
                    session_id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    expires INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO sykit_sessions VALUES (?, ?, ?)",
                ("session-id-0122", json.dumps({"role": "admin"}), time.time() + 60),
            )
            connection.commit()
        finally:
            connection.close()
        store = SessionStore(path)
        self.assertEqual(store.load("session-id-0122"), {"role": "admin"})
        self.assertEqual(self._versions(path), {"sessions": 1})

    def test_0122_api_key_and_task_databases_are_adopted(self) -> None:
        key_path = self.root / "keys.sqlite3"
        connection = sqlite3.connect(key_path)
        try:
            connection.execute(
                """
                CREATE TABLE sykit_api_keys (
                    key_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    scopes TEXT NOT NULL,
                    created INTEGER NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                "INSERT INTO sykit_api_keys VALUES (?, ?, ?, ?, ?, ?)",
                ("key-1", "fixture", "hash-1", '["read"]', 1, 0),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(ApiKeyStore(key_path).lookup("hash-1")["id"], "key-1")
        self.assertEqual(self._versions(key_path), {"api-keys": 1})

        task_path = self.root / "tasks.sqlite3"
        connection = sqlite3.connect(task_path)
        try:
            connection.executescript(
                """
                CREATE TABLE sykit_tasks (
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
                CREATE INDEX sykit_tasks_claim
                    ON sykit_tasks (status, available, lease_until, created);
                CREATE TABLE sykit_schedule_runs (
                    schedule_key TEXT PRIMARY KEY,
                    created REAL NOT NULL
                );
                INSERT INTO sykit_tasks (
                    task_id, task_name, args, kwargs, status, created, available
                ) VALUES ('task-1', 'fixture:run', '[]', '{}', 'queued', 1, 1);
                """
            )
            connection.commit()
        finally:
            connection.close()
        claimed = TaskStore(task_path).claim("worker", 30)
        self.assertEqual(claimed["id"], "task-1")
        self.assertEqual(self._versions(task_path), {"tasks": 1})

    def test_components_can_share_one_versioned_database(self) -> None:
        path = self.root / "shared.sqlite3"
        SessionStore(path).save("session-id-shared", {"ok": True}, 60)
        ApiKeyStore(path).list_keys()
        TaskStore(path).ready()
        limiter = RateLimiter(path)
        asyncio.run(
            limiter.check(
                "POST:ping",
                {"site-wide": {"requests": 2, "window": 60}},
                {},
            )
        )
        self.assertEqual(
            self._versions(path),
            {"api-keys": 1, "rate-limits": 1, "sessions": 1, "tasks": 1},
        )

    def test_newer_schema_is_rejected(self) -> None:
        path = self.root / "future.sqlite3"
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                f"CREATE TABLE {SCHEMA_TABLE} ("
                "component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            connection.execute(f"INSERT INTO {SCHEMA_TABLE} VALUES ('sessions', 99)")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(RuntimeError, "supports only version 1"):
            SessionStore(path).load("missing")


if __name__ == "__main__":
    unittest.main()
