from __future__ import annotations

import asyncio
import math
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool

from sykit._schema import migrate_schema

_SESSION_ID_KEY = "__sykit_rate_id"
RATE_LIMIT_MIGRATIONS = (
    (
        """
        CREATE TABLE IF NOT EXISTS sykit_rate_limits (
            scope TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            identity TEXT NOT NULL,
            window_seconds INTEGER NOT NULL,
            window_start INTEGER NOT NULL,
            request_count INTEGER NOT NULL,
            PRIMARY KEY (
                scope,
                endpoint,
                identity,
                window_seconds,
                window_start
            )
        )
        """,
    ),
)


class RateLimitExceeded(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("Rate limit exceeded.")
        self.retry_after = max(1, retry_after)


class RateLimitUnavailable(RuntimeError):
    pass


class RateLimiter:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._endpoint_locks: dict[str, asyncio.Lock] = {}
        self._shared_lock = asyncio.Lock()
        self._worker_counts: dict[tuple[str, int, int], int] = {}
        self._schema_ready = False
        self._last_cleanup = 0
        self._last_worker_cleanup = 0.0

    async def check(
        self,
        endpoint: str,
        limits: dict[str, dict[str, int] | None] | None,
        session: dict[str, Any],
        client: str = "",
        key_id: str = "",
    ) -> None:
        if not limits or not any(limits.values()):
            return

        now = time.time()
        worker_limit = limits.get("per-worker")
        shared: list[tuple[str, str, dict[str, int]]] = []
        session_limit = limits.get("per-session")
        if session_limit is not None:
            session_id = session.get(_SESSION_ID_KEY)
            if not isinstance(session_id, str) or not session_id:
                session_id = uuid.uuid4().hex
                session[_SESSION_ID_KEY] = session_id
            shared.append(("session", session_id, session_limit))
        site_limit = limits.get("site-wide")
        if site_limit is not None:
            shared.append(("site", "", site_limit))
        client_limit = limits.get("per-client")
        if client_limit is not None:
            # This is the direct peer unless trusted proxy handling is enabled;
            # Uvicorn accepts forwarded addresses only from its allowed proxies.
            shared.append(("client", client, client_limit))
        key_limit = limits.get("per-key")
        if key_limit is not None and key_id:
            # Build guarantees per-key limits only appear on @api_key
            # endpoints, so a validated key id is always present here.
            shared.append(("key", key_id, key_limit))

        lock = self._endpoint_locks.setdefault(endpoint, asyncio.Lock())
        async with lock:
            if worker_limit is not None:
                retry_after = self._worker_retry(endpoint, worker_limit, now)
                if retry_after is not None:
                    raise RateLimitExceeded(retry_after)

            if shared:
                try:
                    async with self._shared_lock:
                        retry_after = await run_in_threadpool(
                            self._check_shared,
                            endpoint,
                            shared,
                            now,
                        )
                except sqlite3.Error as error:
                    raise RateLimitUnavailable(
                        "The shared rate-limit store is unavailable."
                    ) from error
                if retry_after is not None:
                    raise RateLimitExceeded(retry_after)

            if worker_limit is not None:
                self._increment_worker(endpoint, worker_limit, now)

    @staticmethod
    def _window(rate: dict[str, int], now: float) -> tuple[int, int]:
        seconds = rate["window"]
        start = int(now // seconds) * seconds
        retry_after = max(1, math.ceil(start + seconds - now))
        return start, retry_after

    def _worker_retry(
        self,
        endpoint: str,
        rate: dict[str, int],
        now: float,
    ) -> int | None:
        start, retry_after = self._window(rate, now)
        if now < self._last_worker_cleanup or now - self._last_worker_cleanup >= 60:
            self._worker_counts = {
                key: count
                for key, count in self._worker_counts.items()
                if key[2] + key[1] > now
            }
            self._last_worker_cleanup = now
        key = (endpoint, rate["window"], start)
        if self._worker_counts.get(key, 0) >= rate["requests"]:
            return retry_after
        return None

    def _increment_worker(
        self,
        endpoint: str,
        rate: dict[str, int],
        now: float,
    ) -> None:
        start, _ = self._window(rate, now)
        key = (endpoint, rate["window"], start)
        self._worker_counts[key] = self._worker_counts.get(key, 0) + 1

    def _prepare_database(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA synchronous=NORMAL")
        if self._schema_ready:
            return
        connection.execute("PRAGMA journal_mode=WAL")
        migrate_schema(connection, "rate-limits", RATE_LIMIT_MIGRATIONS)
        self._schema_ready = True

    def _check_shared(
        self,
        endpoint: str,
        shared: list[tuple[str, str, dict[str, int]]],
        now: float,
    ) -> int | None:
        connection = sqlite3.connect(self.database_path, timeout=5)
        try:
            self._prepare_database(connection)
            connection.execute("BEGIN IMMEDIATE")
            retry_after: int | None = None
            for scope, identity, rate in shared:
                start, retry = self._window(rate, now)
                row = connection.execute(
                    """
                    INSERT INTO sykit_rate_limits (
                        scope, endpoint, identity, window_seconds,
                        window_start, request_count
                    ) VALUES (?, ?, ?, ?, ?, 1)
                    ON CONFLICT (
                        scope, endpoint, identity, window_seconds, window_start
                    ) DO UPDATE SET request_count = request_count + 1
                    RETURNING request_count
                    """,
                    (scope, endpoint, identity, rate["window"], start),
                ).fetchone()
                if row is None:
                    raise sqlite3.DatabaseError("Rate-limit update returned no row.")
                if row[0] > rate["requests"]:
                    retry_after = max(retry_after or 0, retry)

            if retry_after is not None:
                connection.rollback()
                return retry_after

            current_second = int(now)
            if current_second - self._last_cleanup >= 3600:
                connection.execute(
                    """
                    DELETE FROM sykit_rate_limits
                    WHERE window_start + window_seconds < ?
                    """,
                    (current_second - 3600,),
                )
                self._last_cleanup = current_second
            connection.commit()
            return None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


__all__ = ["RateLimitExceeded", "RateLimiter", "RateLimitUnavailable"]
