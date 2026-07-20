from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from importlib import import_module
from pathlib import Path
from typing import Any

from sykit._schema import migrate_schema

KEY_PREFIX = "sykit"
KEY_HEADER = "x-api-key"
DEFAULT_SQLITE_FILE = ".sykit-apikeys.sqlite3"
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.:-]{0,63}")
SCOPE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
STORE_METHODS = ("lookup", "create", "list_keys", "revoke")
API_KEY_MIGRATIONS = (
    (
        """
        CREATE TABLE IF NOT EXISTS sykit_api_keys (
            key_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            key_hash TEXT NOT NULL UNIQUE,
            scopes TEXT NOT NULL,
            created INTEGER NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0
        )
        """,
    ),
)


class ApiKeyError(RuntimeError):
    """A user-facing API key failure."""


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str]:
    """Return (key_id, key). Only the hash of the key is ever stored."""
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    return key_id, f"{KEY_PREFIX}_{key_id}_{secret}"


def valid_name(value: Any) -> str:
    if not isinstance(value, str) or not NAME_PATTERN.fullmatch(value.strip()):
        raise ApiKeyError(
            "key names must be 1-64 characters of letters, digits, spaces, "
            'or "_", ".", ":", "-", starting with a letter or digit.'
        )
    return value.strip()


def valid_scopes(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(entry, str) and SCOPE_PATTERN.fullmatch(entry) for entry in value
    ):
        raise ApiKeyError(
            "scopes must be a list of simple names (letters, digits, "
            '"_", ".", ":", "-").'
        )
    folded = [entry.casefold() for entry in value]
    if len(set(folded)) != len(folded):
        raise ApiKeyError("scopes may not contain duplicates.")
    return list(value)


class ApiKeyStore:
    """Interface for API key backends.

    Implementations persist key records keyed by the sha256 hash of the
    full key string. lookup() is called from a thread pool on every
    keyed request; the management methods are called by the
    "python SyKit keys" command. Records are dicts with "id", "name",
    "scopes" (list of strings), "created" (unix seconds), and "revoked"
    (bool).
    """

    def lookup(self, key_hash: str) -> dict[str, Any] | None:
        """Return the record for a key hash, or None when unknown."""
        raise NotImplementedError

    def create(self, record: dict[str, Any], key_hash: str) -> None:
        """Store a new key record under its hash."""
        raise NotImplementedError

    def list_keys(self) -> list[dict[str, Any]]:
        """Return every record, oldest first, without hashes."""
        raise NotImplementedError

    def revoke(self, key_id: str) -> bool:
        """Mark a key revoked; False when the id is unknown."""
        raise NotImplementedError


class SqliteApiKeyStore(ApiKeyStore):
    """Default store: one sqlite file in the project root.

    The file lives outside built/ so issued keys survive rebuilds.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        try:
            connection.execute("PRAGMA synchronous=NORMAL")
            if not self._schema_ready:
                connection.execute("PRAGMA journal_mode=WAL")
                migrate_schema(connection, "api-keys", API_KEY_MIGRATIONS)
                self._schema_ready = True
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _record(row: tuple) -> dict[str, Any]:
        scopes = json.loads(row[2])
        return {
            "id": row[0],
            "name": row[1],
            "scopes": scopes if isinstance(scopes, list) else [],
            "created": row[3],
            "revoked": bool(row[4]),
        }

    def lookup(self, key_hash: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT key_id, name, scopes, created, revoked "
                "FROM sykit_api_keys WHERE key_hash = ?",
                (key_hash,),
            ).fetchone()
            return None if row is None else self._record(row)
        finally:
            connection.close()

    def create(self, record: dict[str, Any], key_hash: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO sykit_api_keys "
                "(key_id, name, key_hash, scopes, created, revoked) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (
                    record["id"],
                    record["name"],
                    key_hash,
                    json.dumps(record["scopes"]),
                    record["created"],
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def list_keys(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT key_id, name, scopes, created, revoked "
                "FROM sykit_api_keys ORDER BY created, rowid"
            ).fetchall()
            return [self._record(row) for row in rows]
        finally:
            connection.close()

    def revoke(self, key_id: str) -> bool:
        connection = self._connect()
        try:
            cursor = connection.execute(
                "UPDATE sykit_api_keys SET revoked = 1 WHERE key_id = ?",
                (key_id,),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()


def issue_key(
    store: ApiKeyStore, name: str, scopes: list[str] | None = None
) -> tuple[str, dict[str, Any]]:
    """Create and store a new key; returns (key, record).

    The key string is shown once and never stored; only its hash is.
    """
    key_id, key = generate_key()
    record = {
        "id": key_id,
        "name": valid_name(name),
        "scopes": valid_scopes(scopes),
        "created": int(time.time()),
        "revoked": False,
    }
    store.create(record, hash_key(key))
    return key, record


def resolve_key_store(spec: Any, default_dir: Path) -> ApiKeyStore:
    """Turn the "apikey-store" setting into a store.

    "" or "sqlite" opens the default sqlite file in default_dir;
    "sqlite:path" a custom path. Any other "scheme:target" imports
    core/_keystore_<scheme>.py (added by a package) and calls its
    create(target).
    """
    if spec is None:
        spec = ""
    if not isinstance(spec, str):
        raise RuntimeError('The "apikey-store" setting must be a string.')
    text = spec.strip()
    scheme, _, target = text.partition(":")
    scheme = scheme.strip().casefold()
    target = target.strip()
    if not text or scheme == "sqlite":
        path = Path(target) if target else Path(DEFAULT_SQLITE_FILE)
        if not path.is_absolute():
            path = default_dir / path
        return SqliteApiKeyStore(path)
    if not scheme.isidentifier():
        raise RuntimeError(f'Invalid "apikey-store" scheme {scheme!r}.')
    module_name = f"core._keystore_{scheme}"
    try:
        module = import_module(module_name)
    except ImportError as error:
        raise RuntimeError(
            f'Unknown API key store "{scheme}". Install a package that '
            f'provides files/core/_keystore_{scheme}.py, or use "sqlite" '
            'or "".'
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
            f"The {scheme!r} API key store is missing: {', '.join(missing)}."
        )
    return store


__all__ = [
    "ApiKeyError",
    "ApiKeyStore",
    "KEY_HEADER",
    "SqliteApiKeyStore",
    "generate_key",
    "hash_key",
    "issue_key",
    "resolve_key_store",
    "valid_name",
    "valid_scopes",
]
