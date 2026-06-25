"""Operational review-memory database migration helpers."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import TypedDict

try:
    from .memory_schema import SCHEMA_VERSION, open_connection, verify_schema
    from .memory_validation import ReviewMemoryError
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_schema import SCHEMA_VERSION, open_connection, verify_schema
    from memory_validation import ReviewMemoryError


class MigrationResult(TypedDict):
    source: str
    destination: str
    schema_version: int
    table_counts: dict[str, int]


def migrate_volume(
    source: str,
    destination: str,
    *,
    owner_uid: int | None = None,
    owner_gid: int | None = None,
) -> MigrationResult:
    source_path = Path(source).expanduser()
    destination_path = Path(destination).expanduser()
    if not source_path.exists():
        raise ReviewMemoryError(f"source database does not exist: {source_path}")
    if destination_path.exists():
        raise ReviewMemoryError(f"destination database already exists: {destination_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    source_connection = open_connection(source_path)
    temporary_path: Path | None = None
    try:
        verify_schema(source_connection)
        checkpoint = source_connection.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
        busy = int(checkpoint[0]) if checkpoint is not None else 1
        if busy:
            raise ReviewMemoryError(
                "could not checkpoint source WAL; stop writers and retry migration"
            )
        _verify_integrity(source_connection)
        source_counts = _table_counts(source_connection)

        with tempfile.NamedTemporaryFile(
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=destination_path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)

        destination_connection = open_connection(temporary_path)
        try:
            source_connection.backup(destination_connection)
            destination_connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            destination_connection.commit()
            _verify_integrity(destination_connection)
            destination_counts = _table_counts(destination_connection)
        finally:
            destination_connection.close()

        if destination_counts != source_counts:
            raise ReviewMemoryError("destination table counts do not match source")

        _set_destination_permissions(
            destination_path.parent,
            temporary_path,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
        )
        temporary_path.replace(destination_path)
        return {
            "source": str(source_path),
            "destination": str(destination_path),
            "schema_version": SCHEMA_VERSION,
            "table_counts": source_counts,
        }
    finally:
        source_connection.close()
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _set_destination_permissions(
    directory: Path,
    database_file: Path,
    *,
    owner_uid: int | None,
    owner_gid: int | None,
) -> None:
    if owner_uid is not None or owner_gid is not None:
        uid = owner_uid if owner_uid is not None else -1
        gid = owner_gid if owner_gid is not None else -1
        os.chown(directory, uid, gid)
    directory.chmod(0o750)
    database_file.chmod(0o640)
    if owner_uid is not None or owner_gid is not None:
        uid = owner_uid if owner_uid is not None else -1
        gid = owner_gid if owner_gid is not None else -1
        os.chown(database_file, uid, gid)


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        str(row["name"])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
    ]
    counts: dict[str, int] = {}
    for table in tables:
        quoted = '"' + table.replace('"', '""') + '"'
        counts[table] = int(
            connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        )
    return counts


def _verify_integrity(connection: sqlite3.Connection) -> None:
    integrity = connection.execute("PRAGMA integrity_check").fetchone()
    if integrity is None or str(integrity[0]).lower() != "ok":
        raise ReviewMemoryError("SQLite integrity_check failed")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise ReviewMemoryError("SQLite foreign_key_check failed")
