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

        CREATE TABLE IF NOT EXISTS review_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            trigger_comment_id INTEGER,
            trigger_user TEXT NOT NULL DEFAULT '',
            head_sha TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
            findings_count INTEGER CHECK (findings_count IS NULL OR findings_count >= 0),
            posted_comment_id INTEGER,
            started_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_review_runs_repo_started
            ON review_runs(repository, started_at DESC);
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


def compute_stats(
    connection: sqlite3.Connection,
    *,
    repository: str | None = None,
    expiring_within_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only summary of findings and human decisions for operator triage.

    Active-suppression counts always go through active_suppression(), so expired
    decisions, context-hash-invalid decisions, and non-suppressive states
    (resolved / reopen) are never counted as active.
    """
    moment = now or utc_now()
    repo = normalize_repository(repository) if repository else None
    expiring_within_days = max(0, int(expiring_within_days))
    expiry_cutoff = moment + timedelta(days=expiring_within_days)

    params: list[Any] = []
    where = ""
    if repo:
        where = "WHERE repository = ?"
        params.append(repo)
    rows = connection.execute(f"SELECT * FROM findings {where}", params).fetchall()

    by_severity = {severity: 0 for severity in sorted(SEVERITIES)}
    by_category = {category: 0 for category in sorted(CATEGORIES)}
    latest_decision_by_type = {decision: 0 for decision in sorted(DECISIONS)}
    findings_without_decision = 0
    active_suppressions = 0
    nearing_expiry = 0
    repeats_after_decision = 0

    for row in rows:
        item = dict(row)
        fingerprint = item["fingerprint"]
        if item.get("severity") in by_severity:
            by_severity[item["severity"]] += 1
        if item.get("category") in by_category:
            by_category[item["category"]] += 1

        decision = latest_decision(connection, fingerprint)
        if decision is None:
            findings_without_decision += 1
        elif decision["decision"] in latest_decision_by_type:
            latest_decision_by_type[decision["decision"]] += 1

        suppression = active_suppression(
            connection, fingerprint, context_hash=item.get("context_hash"), now=moment
        )
        if suppression is not None:
            active_suppressions += 1
            expires = parse_time(suppression.get("expires_at"))
            if expires is not None and expires <= expiry_cutoff:
                nearing_expiry += 1

        # Approximate "repeated after a human decided it": the finding was re-recorded
        # (occurrences > 1) and its most recent decision was made at or before the last
        # sighting. Timestamps are second-granular, so use <= to avoid missing same-second
        # re-records; a decision made strictly after the last sighting is not counted.
        if int(item.get("occurrences", 1) or 1) > 1 and decision is not None:
            last_seen = parse_time(item.get("last_seen_at"))
            decided_at = parse_time(decision.get("created_at"))
            if last_seen is not None and decided_at is not None and decided_at <= last_seen:
                repeats_after_decision += 1

    return {
        "repository": repo,
        "generated_at": isoformat(moment),
        "findings_total": len(rows),
        "findings_without_decision": findings_without_decision,
        "findings_by_severity": by_severity,
        "findings_by_category": by_category,
        "latest_decision_by_type": latest_decision_by_type,
        "active_suppressions": active_suppressions,
        "active_suppressions_expiring_within_days": expiring_within_days,
        "active_suppressions_nearing_expiry": nearing_expiry,
        "repeats_after_decision_approx": repeats_after_decision,
    }


def start_run(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    *,
    trigger_comment_id: int | None = None,
    trigger_user: str = "",
    head_sha: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the start of a review run. Operational telemetry only — this is a
    separate table from findings/decisions and never affects suppression."""
    repository = normalize_repository(repository)
    if not isinstance(pr_number, int) or pr_number < 1:
        raise ReviewMemoryError("pr_number must be positive")
    started = isoformat(now)
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO review_runs (
                repository, pr_number, trigger_comment_id, trigger_user, head_sha,
                status, started_at
            ) VALUES (?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                repository,
                pr_number,
                int(trigger_comment_id) if trigger_comment_id is not None else None,
                _clean_text(trigger_user, field="trigger_user", maximum=200, required=False),
                _clean_text(head_sha, field="head_sha", maximum=64, required=False),
                started,
            ),
        )
    return {
        "id": cursor.lastrowid,
        "repository": repository,
        "pr_number": pr_number,
        "trigger_comment_id": trigger_comment_id,
        "status": "running",
        "started_at": started,
    }


def complete_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    repository: str | None = None,
    pr_number: int | None = None,
    status: str = "done",
    findings_count: int | None = None,
    posted_comment_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Mark one specific running run (by id) as done or failed, atomically. Completing
    by id — not by latest-running — prevents one review from completing another when
    reviews of the same pull request overlap. The optional repository/pr_number guard
    further scopes the update. Returns None when no running run with that id (and guard)
    exists, so a duplicate or losing completer is a clean no-op rather than a corruption."""
    if status not in {"done", "failed"}:
        raise ReviewMemoryError("status must be done or failed")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        raise ReviewMemoryError("run_id must be a positive integer")
    # Validated here so the >= 0 invariant holds for every database, including a
    # review_runs table created before the column CHECK existed. This function is the
    # authoritative guard; the table-level CHECK is incremental defense-in-depth for
    # freshly created databases only.
    if findings_count is not None and int(findings_count) < 0:
        raise ReviewMemoryError("findings_count must be zero or greater")
    conditions = ["id = ?", "status = 'running'"]
    params: list[Any] = [run_id]
    if repository is not None:
        conditions.append("repository = ?")
        params.append(normalize_repository(repository))
    if pr_number is not None:
        conditions.append("pr_number = ?")
        params.append(int(pr_number))
    completed = isoformat(now)
    with connection:
        cursor = connection.execute(
            f"""
            UPDATE review_runs
            SET status = ?, findings_count = ?, posted_comment_id = ?, completed_at = ?
            WHERE {" AND ".join(conditions)}
            """,
            (
                status,
                int(findings_count) if findings_count is not None else None,
                int(posted_comment_id) if posted_comment_id is not None else None,
                completed,
                *params,
            ),
        )
    if cursor.rowcount == 0:
        return None
    return {"id": run_id, "status": status, "findings_count": findings_count, "completed_at": completed}


def list_runs(
    connection: sqlite3.Connection,
    *,
    repository: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    params: list[Any] = []
    where = ""
    if repository:
        where = "WHERE repository = ?"
        params.append(normalize_repository(repository))
    params.append(limit)
    rows = connection.execute(
        f"SELECT * FROM review_runs {where} ORDER BY started_at DESC, id DESC LIMIT ?", params
    ).fetchall()
    return [dict(row) for row in rows]


def run_stats(
    connection: sqlite3.Connection,
    *,
    repository: str | None = None,
    days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read-only operational metrics over review_runs: counts by status, time-to-answer
    percentiles, and average findings per completed run."""
    moment = now or utc_now()
    repo = normalize_repository(repository) if repository else None
    days = max(1, int(days))
    since = isoformat(moment - timedelta(days=days))
    params: list[Any] = [since]
    where = "WHERE started_at >= ?"
    if repo:
        where += " AND repository = ?"
        params.append(repo)
    rows = connection.execute(
        f"SELECT status, started_at, completed_at, findings_count FROM review_runs {where}", params
    ).fetchall()

    by_status = {"running": 0, "done": 0, "failed": 0}
    durations: list[float] = []
    findings_total = 0
    completed_with_count = 0
    for row in rows:
        item = dict(row)
        if item.get("status") in by_status:
            by_status[item["status"]] += 1
        started = parse_time(item.get("started_at"))
        completed = parse_time(item.get("completed_at"))
        if started is not None and completed is not None:
            durations.append((completed - started).total_seconds())
        if item.get("status") == "done" and item.get("findings_count") is not None:
            findings_total += int(item["findings_count"])
            completed_with_count += 1
    durations.sort()

    def _pct(p: float) -> float | None:
        if not durations:
            return None
        index = min(len(durations) - 1, int(round((p / 100.0) * (len(durations) - 1))))
        return round(durations[index], 1)

    return {
        "repository": repo,
        "window_days": days,
        "generated_at": isoformat(moment),
        "total": len(rows),
        "by_status": by_status,
        "time_to_answer_seconds": {"p50": _pct(50), "p95": _pct(95)},
        "avg_findings_per_completed_run": (
            round(findings_total / completed_with_count, 2) if completed_with_count else None
        ),
    }


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
