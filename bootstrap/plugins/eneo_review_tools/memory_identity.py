"""Stable finding identity helpers."""

from __future__ import annotations

import hashlib
import re
import sqlite3

try:
    from .memory_validation import (
        ReviewMemoryError,
        clean_text,
        normalize_path,
        normalize_repository,
        normalize_rule_id,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_validation import (
        ReviewMemoryError,
        clean_text,
        normalize_path,
        normalize_repository,
        normalize_rule_id,
    )


def _fingerprint_piece(value: str) -> str:
    return " ".join(value.strip().lower().split())


def compute_fingerprint(
    repository: str,
    rule_id: str,
    path: str,
    symbol: str,
    anchor: str,
) -> str:
    repository = normalize_repository(repository)
    rule_id = normalize_rule_id(rule_id)
    path = normalize_path(path)
    symbol = clean_text(symbol, field="symbol", maximum=200, required=False)
    anchor = clean_text(anchor, field="anchor", maximum=240)
    canonical = "\n".join(
        [
            repository,
            rule_id,
            path,
            _fingerprint_piece(symbol),
            _fingerprint_piece(anchor),
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_fingerprint(
    connection: sqlite3.Connection, value: str, *, min_prefix: int = 8
) -> str:
    candidate = str(value or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", candidate):
        row = connection.execute(
            "SELECT fingerprint FROM findings WHERE fingerprint = ?", (candidate,)
        ).fetchone()
        if not row:
            raise ReviewMemoryError("unknown fingerprint")
        return candidate
    if len(candidate) < min_prefix or not re.fullmatch(r"[0-9a-f]+", candidate):
        raise ReviewMemoryError(
            f"fingerprint prefix must contain at least {min_prefix} hex characters"
        )
    rows = connection.execute(
        "SELECT fingerprint FROM findings WHERE fingerprint LIKE ? ORDER BY fingerprint LIMIT 2",
        (candidate + "%",),
    ).fetchall()
    if not rows:
        raise ReviewMemoryError("unknown fingerprint prefix")
    if len(rows) > 1:
        raise ReviewMemoryError("ambiguous fingerprint prefix; provide more characters")
    return str(rows[0]["fingerprint"])
