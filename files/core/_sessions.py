from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import secrets
import sqlite3
import time
from importlib import import_module
from pathlib import Path
from typing import Any

import itsdangerous
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.requests import cookie_parser
from starlette.responses import JSONResponse

from sykit._schema import migrate_schema

LOGGER = logging.getLogger("sykit.server")

# Set by sykit.auth.login(); popped before the session is persisted. In
# store mode it makes the middleware issue a fresh session id so a login
# cannot be fixated onto an id the client presented earlier.
ROTATE_KEY = "__sykit_rotate"
_INTERNAL_PREFIX = "__sykit_"

DEFAULT_SQLITE_FILE = ".sykit-sessions.sqlite3"
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}")
STORE_METHODS = ("load", "save", "touch", "delete")
_COOKIE_WARN_BYTES = 4000
SESSION_MIGRATIONS = (
    (
        """
        CREATE TABLE IF NOT EXISTS sykit_sessions (
            session_id TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            expires INTEGER NOT NULL
        )
        """,
    ),
)


class SessionStore:
    """Interface for server-side session backends.

    Implementations persist one JSON-serializable dict per session id.
    All methods are called from a thread pool, so blocking calls are
    fine; they must be safe to call from multiple threads and multiple
    worker processes at once.
    """

    def load(self, session_id: str) -> dict[str, Any] | None:
        """Return the stored dict, or None when missing or expired."""
        raise NotImplementedError

    def save(self, session_id: str, data: dict[str, Any], max_age: int) -> None:
        """Store the dict and (re)set its expiry max_age seconds ahead."""
        raise NotImplementedError

    def touch(self, session_id: str, max_age: int) -> None:
        """Push an existing session's expiry max_age seconds ahead."""
        raise NotImplementedError

    def delete(self, session_id: str) -> None:
        """Remove the session; missing ids are not an error."""
        raise NotImplementedError


class SqliteSessionStore(SessionStore):
    """Default server-side store: one sqlite file next to the built app."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._schema_ready = False
        self._last_cleanup = 0

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        try:
            connection.execute("PRAGMA synchronous=NORMAL")
            if not self._schema_ready:
                connection.execute("PRAGMA journal_mode=WAL")
                migrate_schema(connection, "sessions", SESSION_MIGRATIONS)
                self._schema_ready = True
            return connection
        except BaseException:
            connection.close()
            raise

    def load(self, session_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT data, expires FROM sykit_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            if row[1] < int(time.time()):
                connection.execute(
                    "DELETE FROM sykit_sessions WHERE session_id = ?",
                    (session_id,),
                )
                connection.commit()
                return None
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        finally:
            connection.close()

    def save(self, session_id: str, data: dict[str, Any], max_age: int) -> None:
        now = int(time.time())
        connection = self._connect()
        try:
            connection.execute(
                "INSERT OR REPLACE INTO sykit_sessions (session_id, data, expires) "
                "VALUES (?, ?, ?)",
                (session_id, json.dumps(data), now + max_age),
            )
            if now - self._last_cleanup >= 3600:
                connection.execute(
                    "DELETE FROM sykit_sessions WHERE expires < ?",
                    (now,),
                )
                self._last_cleanup = now
            connection.commit()
        finally:
            connection.close()

    def touch(self, session_id: str, max_age: int) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE sykit_sessions SET expires = ? WHERE session_id = ?",
                (int(time.time()) + max_age, session_id),
            )
            connection.commit()
        finally:
            connection.close()

    def delete(self, session_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "DELETE FROM sykit_sessions WHERE session_id = ?",
                (session_id,),
            )
            connection.commit()
        finally:
            connection.close()


def resolve_store(spec: Any, root: Path) -> SessionStore | None:
    """Turn the "session-store" setting into a store, or None for cookies.

    "" keeps signed-cookie sessions. "sqlite" or "sqlite:path" opens the
    built-in sqlite store. Any other "scheme:target" imports
    core/_store_<scheme>.py (added by a package) and calls its
    create(target).
    """
    if spec is None:
        return None
    if not isinstance(spec, str):
        raise RuntimeError('The "session-store" setting must be a string.')
    text = spec.strip()
    if not text:
        return None
    scheme, _, target = text.partition(":")
    scheme = scheme.strip().casefold()
    target = target.strip()
    if scheme == "sqlite":
        path = Path(target) if target else Path(DEFAULT_SQLITE_FILE)
        if not path.is_absolute():
            path = root / path
        return SqliteSessionStore(path)
    if not scheme.isidentifier():
        raise RuntimeError(f'Invalid "session-store" scheme {scheme!r}.')
    module_name = f"core._store_{scheme}"
    try:
        module = import_module(module_name)
    except ImportError as error:
        raise RuntimeError(
            f'Unknown session store "{scheme}". Install a package that '
            f'provides files/core/_store_{scheme}.py, or use "sqlite" or '
            '"" (signed cookies).'
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
            f"The {scheme!r} session store is missing: {', '.join(missing)}."
        )
    return store


def _decode_session_payload(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(base64.b64decode(raw))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _snapshot(session: dict[str, Any]) -> str:
    try:
        return json.dumps(session, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""


class SessionMiddleware:
    """SyKit's session layer.

    Without a store this signs the whole session dict into the cookie in
    the same format Starlette's SessionMiddleware uses, so existing
    cookies stay valid. With a store the cookie only carries a signed
    random session id and the data lives server-side, which lifts the
    4 KB cookie ceiling and makes logout an actual server-side
    revocation.
    """

    def __init__(
        self,
        application,
        secret: str,
        store: SessionStore | None,
        cookie_name: str,
        max_age: int,
        https_only: bool,
    ) -> None:
        self.application = application
        self.signer = itsdangerous.TimestampSigner(secret)
        self.store = store
        self.cookie_name = cookie_name
        self.max_age = max_age
        self.security_flags = "HttpOnly; SameSite=lax" + (
            "; Secure" if https_only else ""
        )

    def _cookie_value(self, scope: dict[str, Any]) -> str | None:
        headers = dict(scope.get("headers", []))
        raw = headers.get(b"cookie")
        if raw is None:
            return None
        return cookie_parser(raw.decode("latin-1")).get(self.cookie_name)

    def _unsign(self, value: str) -> bytes | None:
        try:
            return self.signer.unsign(value.encode("utf-8"), max_age=self.max_age)
        except (itsdangerous.BadSignature, UnicodeEncodeError):
            return None

    def _set_cookie(self, message: dict[str, Any], value: str) -> None:
        headers = MutableHeaders(scope=message)
        headers.append(
            "Set-Cookie",
            f"{self.cookie_name}={value}; Path=/; "
            f"Max-Age={self.max_age}; {self.security_flags}",
        )

    def _expire_cookie(self, message: dict[str, Any]) -> None:
        headers = MutableHeaders(scope=message)
        headers.append(
            "Set-Cookie",
            f"{self.cookie_name}=null; Path=/; "
            f"Expires=Thu, 01 Jan 1970 00:00:00 GMT; {self.security_flags}",
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        cookie_value = self._cookie_value(scope)
        session: dict[str, Any] = {}
        session_id: str | None = None
        accepted_cookie = False
        if cookie_value is not None:
            unsigned = self._unsign(cookie_value)
            if unsigned is not None:
                if self.store is None:
                    session = _decode_session_payload(unsigned)
                    accepted_cookie = True
                else:
                    candidate = unsigned.decode("utf-8", "replace")
                    if SESSION_ID_PATTERN.fullmatch(candidate):
                        try:
                            loaded = await run_in_threadpool(self.store.load, candidate)
                        except Exception:
                            LOGGER.exception("The session store is unavailable.")
                            response = JSONResponse(
                                {"error": "Sessions are temporarily unavailable."},
                                status_code=503,
                            )
                            await response(scope, receive, send)
                            return
                        if loaded:
                            session = loaded
                            session_id = candidate
                            accepted_cookie = True

        scope["session"] = session
        had_cookie = cookie_value is not None
        loaded_snapshot = _snapshot(session)

        async def send_wrapper(message) -> None:
            if message.get("type") == "http.response.start":
                await self._persist(
                    scope,
                    message,
                    session_id,
                    had_cookie,
                    accepted_cookie,
                    loaded_snapshot,
                )
            await send(message)

        await self.application(scope, receive, send_wrapper)

    async def _persist(
        self,
        scope: dict[str, Any],
        message: dict[str, Any],
        session_id: str | None,
        had_cookie: bool,
        accepted_cookie: bool,
        loaded_snapshot: str,
    ) -> None:
        session = scope["session"]
        rotate = bool(session.pop(ROTATE_KEY, False))

        if (
            not accepted_cookie
            and session
            and all(
                isinstance(key, str) and key.startswith(_INTERNAL_PREFIX)
                for key in session
            )
        ):
            session.clear()

        if self.store is None:
            if session:
                payload = base64.b64encode(json.dumps(session).encode("utf-8"))
                value = self.signer.sign(payload).decode("utf-8")
                if len(value) > _COOKIE_WARN_BYTES:
                    LOGGER.warning(
                        "The session cookie is %d bytes; browsers drop cookies "
                        'near 4 KB. Consider the "session-store" setting.',
                        len(value),
                    )
                self._set_cookie(message, value)
            elif had_cookie:
                self._expire_cookie(message)
            return

        try:
            if session:
                changed = _snapshot(session) != loaded_snapshot
                if session_id is None or rotate:
                    previous = session_id
                    session_id = secrets.token_urlsafe(32)
                    if previous is not None:
                        await run_in_threadpool(self.store.delete, previous)
                    await run_in_threadpool(
                        self.store.save, session_id, session, self.max_age
                    )
                elif changed:
                    await run_in_threadpool(
                        self.store.save, session_id, session, self.max_age
                    )
                else:
                    await run_in_threadpool(self.store.touch, session_id, self.max_age)
                value = self.signer.sign(session_id.encode("ascii")).decode("utf-8")
                self._set_cookie(message, value)
            else:
                if session_id is not None:
                    await run_in_threadpool(self.store.delete, session_id)
                if had_cookie:
                    self._expire_cookie(message)
        except Exception:
            # The response has already started; losing the write is better
            # than crashing mid-response, but it must be loud in the log.
            LOGGER.exception("The session store failed while saving.")


__all__ = [
    "ROTATE_KEY",
    "SessionMiddleware",
    "SessionStore",
    "SqliteSessionStore",
    "resolve_store",
]
