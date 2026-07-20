from __future__ import annotations

import sqlite3
from collections.abc import Sequence

SCHEMA_TABLE = "sykit_schema_versions"


def migrate_schema(
    connection: sqlite3.Connection,
    component: str,
    migrations: Sequence[Sequence[str]],
) -> None:
    """Apply ordered component migrations and record the resulting version."""
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            CHECK (version >= 0)
        )
        """
    )
    connection.commit()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            f"SELECT version FROM {SCHEMA_TABLE} WHERE component = ?",
            (component,),
        ).fetchone()
        current = int(row[0]) if row is not None else 0
        supported = len(migrations)
        if current > supported:
            raise RuntimeError(
                f"The {component} database schema is version {current}, but "
                f"this SyKit supports only version {supported}."
            )
        for version in range(current + 1, supported + 1):
            for statement in migrations[version - 1]:
                connection.execute(statement)
            connection.execute(
                f"""
                INSERT INTO {SCHEMA_TABLE} (component, version)
                VALUES (?, ?)
                ON CONFLICT (component) DO UPDATE SET version = excluded.version
                """,
                (component, version),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


__all__ = ["SCHEMA_TABLE", "migrate_schema"]
