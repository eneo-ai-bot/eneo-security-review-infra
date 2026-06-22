"""SQLite-backed finding history and human-curated suppression decisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_DB_NAME = "review_memory.sqlite3"
SEVERITIES = {"Critical", "High"}
CATEGORIES = {
    "security",
    "correctness",
    "reliability",
    "contracts",
    "tests",
    "maintainability",
    "performance",
    "migration",
}
DECISIONS = {"false_positive", "accepted_risk", "duplicate", "resolved", "reopen"}
SUPPRESSIVE_DECISIONS = {"false_positive", "accepted_risk", "duplicate"}
MIN_CONFIDENCE = 0.85
MIN_PUBLICATION_SCORE = 8
_RULE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,80}$")
_HASH_RE = re.compile(r"^[0-9a-f]{40,64}$")


class ReviewMemoryError(ValueError):
    """Raised for invalid memory input."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def database_path(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get("ENEO_REVIEW_DB")
    if raw:
        return Path(raw).expanduser()
    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return hermes_home / "review-memory" / DEFAULT_DB_NAME


def connect(explicit: str | None = None) -> sqlite3.Connection:
    path = database_path(explicit)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    init_schema(connection)
    return connection


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS findings (
            fingerprint TEXT PRIMARY KEY,
            repository TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            path TEXT NOT NULL,
            line INTEGER,
            symbol TEXT NOT NULL DEFAULT '',
            anchor TEXT NOT NULL,
            title TEXT NOT NULL,
            severity TEXT NOT NULL CHECK (severity IN ('Critical', 'High')),
            category TEXT NOT NULL DEFAULT 'correctness',
            publication_score INTEGER NOT NULL DEFAULT 8,
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            context_hash TEXT NOT NULL DEFAULT '',
            pr_number INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            evidence TEXT NOT NULL,
            disproof_checks TEXT NOT NULL DEFAULT '',
            impact TEXT NOT NULL DEFAULT '',
            smallest_fix TEXT NOT NULL,
            introduced_by_diff INTEGER NOT NULL CHECK (introduced_by_diff IN (0, 1)),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            occurrences INTEGER NOT NULL DEFAULT 1
        );

        CREATE INDEX IF NOT EXISTS idx_findings_repository_path
            ON findings(repository, path, last_seen_at DESC);
        CREATE INDEX IF NOT EXISTS idx_findings_repository_seen
            ON findings(repository, last_seen_at DESC);

        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fingerprint TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (
                decision IN ('false_positive', 'accepted_risk', 'duplicate', 'resolved', 'reopen')
            ),
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            context_hash TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            expires_at TEXT,
            FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint)
        );

        CREATE INDEX IF NOT EXISTS idx_decisions_fingerprint
            ON decisions(fingerprint, id DESC);
        """
    )
    # Non-destructive migration from the first starter bundle.
    _ensure_column(connection, "findings", "category", "TEXT NOT NULL DEFAULT 'correctness'")
    _ensure_column(connection, "findings", "publication_score", "INTEGER NOT NULL DEFAULT 8")
    _ensure_column(connection, "findings", "context_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "decisions", "context_hash", "TEXT NOT NULL DEFAULT ''")
    connection.commit()


def _clean_text(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    text = " ".join(str(value or "").strip().split())
    if required and not text:
        raise ReviewMemoryError(f"{field} is required")
    if len(text) > maximum:
        raise ReviewMemoryError(f"{field} exceeds {maximum} characters")
    return text


def _clean_multiline(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ReviewMemoryError(f"{field} is required")
    if len(text) > maximum:
        raise ReviewMemoryError(f"{field} exceeds {maximum} characters")
    return text


def normalize_repository(repository: str) -> str:
    value = repository.strip().lower()
    if value.count("/") != 1 or not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", value):
        raise ReviewMemoryError("repository must be owner/name")
    return value


def normalize_path(path: str) -> str:
    value = path.strip().replace("\\", "/")
    if not value or value.startswith("/") or "\x00" in value:
        raise ReviewMemoryError("invalid repository path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReviewMemoryError("repository path may not contain traversal segments")
    if len(value) > 500:
        raise ReviewMemoryError("repository path is too long")
    return value


def normalize_rule_id(rule_id: str) -> str:
    value = rule_id.strip().lower()
    if not _RULE_RE.fullmatch(value):
        raise ReviewMemoryError(
            "rule_id must be stable lower-case letters, digits, dots, dashes, or underscores"
        )
    return value


def normalize_context_hash(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if not _HASH_RE.fullmatch(cleaned):
        raise ReviewMemoryError("context_hash must be a 40 to 64 character hexadecimal hash")
    return cleaned


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
    symbol = _clean_text(symbol, field="symbol", maximum=200, required=False)
    anchor = _clean_text(anchor, field="anchor", maximum=240)
    canonical = "\n".join(
        [repository, rule_id, path, _fingerprint_piece(symbol), _fingerprint_piece(anchor)]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_fingerprint(connection: sqlite3.Connection, value: str, *, min_prefix: int = 8) -> str:
    candidate = str(value or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", candidate):
        row = connection.execute(
            "SELECT fingerprint FROM findings WHERE fingerprint = ?", (candidate,)
        ).fetchone()
        if not row:
            raise ReviewMemoryError("unknown fingerprint")
        return candidate
    if len(candidate) < min_prefix or not re.fullmatch(r"[0-9a-f]+", candidate):
        raise ReviewMemoryError(f"fingerprint prefix must contain at least {min_prefix} hex characters")
    rows = connection.execute(
        "SELECT fingerprint FROM findings WHERE fingerprint LIKE ? ORDER BY fingerprint LIMIT 2",
        (candidate + "%",),
    ).fetchall()
    if not rows:
        raise ReviewMemoryError("unknown fingerprint prefix")
    if len(rows) > 1:
        raise ReviewMemoryError("ambiguous fingerprint prefix; provide more characters")
    return str(rows[0]["fingerprint"])


def latest_decision(connection: sqlite3.Connection, fingerprint: str) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT id, fingerprint, decision, reason, actor, context_hash, created_at, expires_at
        FROM decisions
        WHERE fingerprint = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()
    return dict(row) if row else None


def _current_context_hash(connection: sqlite3.Connection, fingerprint: str) -> str:
    row = connection.execute(
        "SELECT context_hash FROM findings WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    return str(row["context_hash"] or "") if row else ""


def active_suppression(
    connection: sqlite3.Connection,
    fingerprint: str,
    *,
    context_hash: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    decision = latest_decision(connection, fingerprint)
    if not decision or decision["decision"] not in SUPPRESSIVE_DECISIONS:
        return None
    expires = parse_time(decision.get("expires_at"))
    if expires is not None and expires <= (now or utc_now()):
        return None

    current_hash = context_hash or _current_context_hash(connection, fingerprint)
    decision_hash = str(decision.get("context_hash") or "")
    # A suppression is deliberately narrow: it applies only to the exact file
    # version that a human reviewed. Any later file change forces re-validation.
    if not current_hash or not decision_hash or current_hash != decision_hash:
        return None
    return decision


def _validated_finding(repository: str, raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ReviewMemoryError("each finding must be an object")

    rule_id = normalize_rule_id(str(raw.get("rule_id", "")))
    path = normalize_path(str(raw.get("path", "")))
    symbol = _clean_text(raw.get("symbol"), field="symbol", maximum=200, required=False)
    anchor = _clean_text(raw.get("anchor"), field="anchor", maximum=240)
    title = _clean_text(raw.get("title"), field="title", maximum=180)
    severity = str(raw.get("severity", "")).strip().title()
    if severity not in SEVERITIES:
        raise ReviewMemoryError("severity must be Critical or High")

    category = str(raw.get("category", "")).strip().lower()
    if category not in CATEGORIES:
        raise ReviewMemoryError(f"category must be one of: {', '.join(sorted(CATEGORIES))}")

    try:
        publication_score = int(raw.get("publication_score"))
    except (TypeError, ValueError) as exc:
        raise ReviewMemoryError("publication_score must be an integer") from exc
    if publication_score < MIN_PUBLICATION_SCORE or publication_score > 10:
        raise ReviewMemoryError("publication_score must be between 8 and 10")

    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ReviewMemoryError("confidence must be a number") from exc
    if confidence < MIN_CONFIDENCE or confidence > 1.0:
        raise ReviewMemoryError("confidence must be between 0.85 and 1.00")

    introduced = raw.get("introduced_by_diff")
    if introduced is not True:
        raise ReviewMemoryError("introduced_by_diff must be true")

    try:
        line = int(raw.get("line"))
    except (TypeError, ValueError) as exc:
        raise ReviewMemoryError("line is required and must be an integer") from exc
    if line < 1:
        raise ReviewMemoryError("line must be positive")

    evidence = _clean_multiline(raw.get("evidence"), field="evidence", maximum=4000)
    disproof_checks = _clean_multiline(
        raw.get("disproof_checks"), field="disproof_checks", maximum=2500
    )
    impact = _clean_multiline(raw.get("impact"), field="impact", maximum=2000)
    smallest_fix = _clean_multiline(raw.get("smallest_fix"), field="smallest_fix", maximum=2500)

    fingerprint = compute_fingerprint(repository, rule_id, path, symbol, anchor)
    return {
        "fingerprint": fingerprint,
        "repository": normalize_repository(repository),
        "rule_id": rule_id,
        "path": path,
        "line": line,
        "symbol": symbol,
        "anchor": anchor,
        "title": title,
        "severity": severity,
        "category": category,
        "publication_score": publication_score,
        "confidence": confidence,
        "evidence": evidence,
        "disproof_checks": disproof_checks,
        "impact": impact,
        "smallest_fix": smallest_fix,
        "introduced_by_diff": 1,
    }


def record_findings(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    head_sha: str,
    findings: Sequence[dict[str, Any]],
    *,
    context_hashes: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    repository = normalize_repository(repository)
    if not isinstance(pr_number, int) or pr_number < 1:
        raise ReviewMemoryError("pr_number must be positive")
    head_sha = _clean_text(head_sha, field="head_sha", maximum=64).lower()
    if not _HASH_RE.fullmatch(head_sha):
        raise ReviewMemoryError("head_sha must be an exact 40 to 64 character hexadecimal commit SHA")
    if not isinstance(findings, Sequence) or isinstance(findings, (str, bytes)):
        raise ReviewMemoryError("findings must be an array")
    if len(findings) > 3:
        raise ReviewMemoryError("at most three findings may be recorded per review")

    normalized_hashes = {
        normalize_path(path): normalize_context_hash(value)
        for path, value in (context_hashes or {}).items()
    }
    now = isoformat()
    results: list[dict[str, Any]] = []
    with connection:
        for raw in findings:
            item = _validated_finding(repository, raw)
            context_hash = normalized_hashes.get(item["path"], "")
            if not context_hash:
                raise ReviewMemoryError(f"missing trusted context hash for {item['path']}")
            connection.execute(
                """
                INSERT INTO findings (
                    fingerprint, repository, rule_id, path, line, symbol, anchor,
                    title, severity, category, publication_score, confidence,
                    context_hash, pr_number, head_sha, evidence, disproof_checks,
                    impact, smallest_fix, introduced_by_diff, first_seen_at,
                    last_seen_at, occurrences
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    line = excluded.line,
                    title = excluded.title,
                    severity = excluded.severity,
                    category = excluded.category,
                    publication_score = excluded.publication_score,
                    confidence = excluded.confidence,
                    context_hash = excluded.context_hash,
                    pr_number = excluded.pr_number,
                    head_sha = excluded.head_sha,
                    evidence = excluded.evidence,
                    disproof_checks = excluded.disproof_checks,
                    impact = excluded.impact,
                    smallest_fix = excluded.smallest_fix,
                    introduced_by_diff = excluded.introduced_by_diff,
                    last_seen_at = excluded.last_seen_at,
                    occurrences = findings.occurrences + 1
                """,
                (
                    item["fingerprint"], item["repository"], item["rule_id"], item["path"],
                    item["line"], item["symbol"], item["anchor"], item["title"],
                    item["severity"], item["category"], item["publication_score"],
                    item["confidence"], context_hash, pr_number, head_sha, item["evidence"],
                    item["disproof_checks"], item["impact"], item["smallest_fix"],
                    item["introduced_by_diff"], now, now,
                ),
            )
            suppression = active_suppression(
                connection, item["fingerprint"], context_hash=context_hash
            )
            results.append(
                {
                    "fingerprint": item["fingerprint"],
                    "fingerprint_short": item["fingerprint"][:12],
                    "path": item["path"],
                    "rule_id": item["rule_id"],
                    "context_hash": context_hash,
                    "suppressed": suppression is not None,
                    "decision": suppression,
                }
            )
    return results


def memory_context(
    connection: sqlite3.Connection,
    repository: str,
    paths: Iterable[str] | None = None,
    *,
    limit: int = 30,
) -> dict[str, Any]:
    repository = normalize_repository(repository)
    clean_paths = sorted({normalize_path(path) for path in (paths or []) if str(path).strip()})
    limit = max(1, min(int(limit), 50))

    params: list[Any] = [repository]
    where = "repository = ?"
    if clean_paths:
        placeholders = ",".join("?" for _ in clean_paths)
        where += f" AND path IN ({placeholders})"
        params.extend(clean_paths)
    params.append(limit)

    rows = connection.execute(
        f"""
        SELECT fingerprint, rule_id, path, line, symbol, anchor, title, severity,
               category, publication_score, confidence, context_hash, pr_number,
               head_sha, last_seen_at, occurrences
        FROM findings
        WHERE {where}
        ORDER BY last_seen_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    historical_suppressions: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        decision = latest_decision(connection, item["fingerprint"])
        matches_last_seen = active_suppression(
            connection, item["fingerprint"], context_hash=item["context_hash"]
        )
        if matches_last_seen:
            historical_suppressions.append(
                {
                    "fingerprint": item["fingerprint"],
                    "rule_id": item["rule_id"],
                    "path": item["path"],
                    "symbol": item["symbol"],
                    "anchor": item["anchor"],
                    "decision": matches_last_seen["decision"],
                    "reason": matches_last_seen["reason"],
                    "actor": matches_last_seen["actor"],
                    "expires_at": matches_last_seen["expires_at"],
                    "warning": "Historical hint only; the final record tool checks the current file hash.",
                }
            )
        recent.append(
            {
                **item,
                "suppressed_for_last_seen_file_version": matches_last_seen is not None,
                "latest_decision": decision,
            }
        )

    return {
        "repository": repository,
        "paths": clean_paths,
        "policy": (
            "Human decisions are historical hints during analysis. The record tool suppresses only "
            "when the current trusted file hash matches the hash reviewed by the human. A file change "
            "forces re-validation."
        ),
        "historical_suppressions": historical_suppressions,
        "recent_findings": recent,
    }


def add_decision(
    connection: sqlite3.Connection,
    fingerprint: str,
    decision: str,
    reason: str,
    actor: str,
    *,
    expires_days: int | None = None,
) -> dict[str, Any]:
    fingerprint = resolve_fingerprint(connection, fingerprint)
    decision = decision.strip().lower()
    if decision not in DECISIONS:
        raise ReviewMemoryError(f"decision must be one of: {', '.join(sorted(DECISIONS))}")
    reason = _clean_multiline(reason, field="reason", maximum=2000)
    actor = _clean_text(actor, field="actor", maximum=200)

    row = connection.execute(
        "SELECT context_hash FROM findings WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    if not row:
        raise ReviewMemoryError("unknown fingerprint; record the finding before deciding it")
    context_hash = str(row["context_hash"] or "")
    if decision in SUPPRESSIVE_DECISIONS and not context_hash:
        raise ReviewMemoryError("finding has no trusted file hash; re-run the review before suppressing")

    expires_at: str | None = None
    if decision in SUPPRESSIVE_DECISIONS:
        days = 180 if expires_days is None else int(expires_days)
        if days < 1 or days > 3650:
            raise ReviewMemoryError("expires_days must be between 1 and 3650")
        expires_at = isoformat(utc_now() + timedelta(days=days))
    elif expires_days is not None:
        raise ReviewMemoryError("expires_days only applies to suppressive decisions")

    created_at = isoformat()
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO decisions (
                fingerprint, decision, reason, actor, context_hash, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (fingerprint, decision, reason, actor, context_hash, created_at, expires_at),
        )
    return {
        "id": cursor.lastrowid,
        "fingerprint": fingerprint,
        "decision": decision,
        "reason": reason,
        "actor": actor,
        "context_hash": context_hash,
        "created_at": created_at,
        "expires_at": expires_at,
    }


def list_findings(
    connection: sqlite3.Connection,
    *,
    repository: str | None = None,
    limit: int = 50,
    include_suppressed: bool = True,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    params: list[Any] = []
    where = ""
    if repository:
        where = "WHERE repository = ?"
        params.append(normalize_repository(repository))
    params.append(limit)
    rows = connection.execute(
        f"""
        SELECT * FROM findings
        {where}
        ORDER BY last_seen_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["latest_decision"] = latest_decision(connection, item["fingerprint"])
        item["suppressed"] = (
            active_suppression(
                connection, item["fingerprint"], context_hash=item.get("context_hash")
            )
            is not None
        )
        if include_suppressed or not item["suppressed"]:
            output.append(item)
    return output


def export_state(connection: sqlite3.Connection) -> dict[str, Any]:
    findings = [dict(row) for row in connection.execute("SELECT * FROM findings ORDER BY first_seen_at")]
    decisions = [dict(row) for row in connection.execute("SELECT * FROM decisions ORDER BY id")]
    return {"schema_version": 2, "exported_at": isoformat(), "findings": findings, "decisions": decisions}


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
