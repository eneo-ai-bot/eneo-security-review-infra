"""SQLite schema, migrations, and connection setup for review memory."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

try:
    from .memory_validation import (
        HASH_RE,
        ReviewMemoryError,
        clean_text,
        current_policy_revision,
        isoformat,
        normalize_repository,
        parse_time,
        utc_now,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_validation import (
        HASH_RE,
        ReviewMemoryError,
        clean_text,
        current_policy_revision,
        isoformat,
        normalize_repository,
        parse_time,
        utc_now,
    )

DEFAULT_DB_NAME = "review_memory.sqlite3"
SCHEMA_VERSION = 13
OBSERVATION_BACKFILL_VERSION = 3
REQUIRED_TABLES = frozenset(
    {
        "findings",
        "decisions",
        "review_runs",
        "review_run_files",
        "coach_runs",
        "coach_candidates",
        "review_subjects",
        "finding_observations",
        "pr_finding_references",
        "review_publications",
        "review_publication_comments",
        "publication_findings",
        "review_quality_feedback",
        "processed_feedback_events",
        "decision_audit",
    }
)


def database_path(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get("ENEO_REVIEW_DB")
    if raw:
        return Path(raw).expanduser()
    hermes_home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    return hermes_home / "review-memory" / DEFAULT_DB_NAME


def connect(explicit: str | None = None) -> sqlite3.Connection:
    path = database_path(explicit)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = open_connection(path)
    connection.execute("PRAGMA journal_mode = WAL")
    init_schema(connection)
    return connection


def connect_existing(explicit: str | None = None) -> sqlite3.Connection:
    path = database_path(explicit)
    if not path.exists():
        raise ReviewMemoryError(
            f"review memory database does not exist at {path}; "
            "run `eneo-review-memory init` first"
        )
    connection = open_connection(path)
    try:
        verify_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection


def open_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def verify_schema(connection: sqlite3.Connection) -> None:
    existing_version = _user_version(connection)
    if existing_version != SCHEMA_VERSION:
        raise ReviewMemoryError(
            f"review memory schema version {existing_version} does not match "
            f"expected version {SCHEMA_VERSION}; run `eneo-review-memory init`"
        )
    existing_tables = {
        str(row["name"])
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
            """
        )
    }
    missing = sorted(REQUIRED_TABLES - existing_tables)
    if missing:
        raise ReviewMemoryError(
            "review memory database is missing required tables: "
            + ", ".join(missing)
        )


def verify_database_ready(explicit: str | None = None) -> dict[str, object]:
    path = database_path(explicit)
    with connect_existing(explicit) as connection:
        connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        connection.execute("BEGIN IMMEDIATE")
        connection.rollback()
    return {"path": str(path), "schema_version": SCHEMA_VERSION}


def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    existing = {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _user_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None:
        return 0
    return int(row[0])


def _findings_has_legacy_severity_check(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'findings'"
    ).fetchone()
    sql = str((row["sql"] if isinstance(row, sqlite3.Row) else row[0]) if row else "")
    return "severity IN ('Critical', 'High')" in sql


def _table_sql(connection: sqlite3.Connection, table: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return str((row["sql"] if isinstance(row, sqlite3.Row) else row[0]) if row else "")


def _decisions_has_legacy_check(connection: sqlite3.Connection) -> bool:
    sql = _table_sql(connection, "decisions")
    return bool(sql) and "intentional_by_design" not in sql


def _review_runs_has_legacy_status_check(connection: sqlite3.Connection) -> bool:
    sql = _table_sql(connection, "review_runs")
    return bool(sql) and "generated" not in sql


def _review_runs_needs_lifecycle_migration(connection: sqlite3.Connection) -> bool:
    sql = _table_sql(connection, "review_runs")
    if not sql:
        return False
    return (
        "last_heartbeat_at" not in sql
        or "failure_code" not in sql
        or "phase IN" not in sql
        or "status = 'running' AND phase" not in sql
    )


def _finding_observations_needs_run_scope_migration(
    connection: sqlite3.Connection,
) -> bool:
    sql = _table_sql(connection, "finding_observations")
    if not sql:
        return False
    return (
        "review_run_id" not in sql
        or "UNIQUE(review_subject_id, fingerprint)" in sql
    )


def _ensure_current_publication_unique_index(connection: sqlite3.Connection) -> None:
    duplicate = connection.execute(
        """
        SELECT repository, pr_number, COUNT(*) AS count
        FROM review_publications
        WHERE delivery_status = 'posted'
          AND superseded_at IS NULL
          AND comment_id IS NOT NULL
        GROUP BY repository, pr_number
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate:
        raise ReviewMemoryError(
            "multiple current review publications exist for "
            f"{duplicate['repository']}#{duplicate['pr_number']}; "
            "resolve the duplicate rows before migrating"
        )
    connection.execute(
        "DROP INDEX IF EXISTS uq_current_review_publication"
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_current_posted_publication
            ON review_publications(repository, pr_number)
            WHERE delivery_status = 'posted'
              AND superseded_at IS NULL
              AND comment_id IS NOT NULL
        """
    )


def _migrate_findings_severity_check(connection: sqlite3.Connection) -> None:
    if not _findings_has_legacy_severity_check(connection):
        return

    columns = """
        fingerprint, repository, rule_id, path, line, symbol, anchor, title, severity,
        category, publication_score, confidence, context_hash, pr_number, head_sha,
        evidence, disproof_checks, impact, smallest_fix, introduced_by_diff,
        first_seen_at, last_seen_at, occurrences
    """
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN")
        connection.execute("DROP TABLE IF EXISTS findings_migration")
        connection.execute(
            """
            CREATE TABLE findings_migration (
                fingerprint TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                path TEXT NOT NULL,
                line INTEGER,
                symbol TEXT NOT NULL DEFAULT '',
                anchor TEXT NOT NULL,
                title TEXT NOT NULL,
                severity TEXT NOT NULL,
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
            )
            """
        )
        connection.execute(
            f"INSERT INTO findings_migration ({columns}) SELECT {columns} FROM findings"
        )
        connection.execute("DROP TABLE findings")
        connection.execute("ALTER TABLE findings_migration RENAME TO findings")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_findings_repository_path
                ON findings(repository, path, last_seen_at DESC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_findings_repository_seen
                ON findings(repository, last_seen_at DESC)
            """
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")

    broken = connection.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise ReviewMemoryError("findings severity migration left broken foreign keys")


def _migrate_decisions_check(connection: sqlite3.Connection) -> None:
    if not _decisions_has_legacy_check(connection):
        return

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN")
        connection.execute("DROP TABLE IF EXISTS decisions_migration")
        connection.execute(
            """
            CREATE TABLE decisions_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL,
                decision TEXT NOT NULL CHECK (
                    decision IN (
                        'false_positive', 'intentional_by_design', 'accepted_risk',
                        'duplicate', 'resolved', 'reopen'
                    )
                ),
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                context_hash TEXT NOT NULL DEFAULT '',
                adr_id TEXT NOT NULL DEFAULT '',
                observation_id INTEGER,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint),
                FOREIGN KEY (observation_id) REFERENCES finding_observations(id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO decisions_migration (
                id, fingerprint, decision, reason, actor, context_hash,
                observation_id, created_at, expires_at
            )
            SELECT id, fingerprint, decision, reason, actor, context_hash,
                   NULL, created_at, expires_at
            FROM decisions
            """
        )
        connection.execute("DROP TABLE decisions")
        connection.execute("ALTER TABLE decisions_migration RENAME TO decisions")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_decisions_fingerprint
                ON decisions(fingerprint, id DESC)
            """
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")

    broken = connection.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise ReviewMemoryError("decisions migration left broken foreign keys")


def _migrate_review_runs_status(connection: sqlite3.Connection) -> None:
    if not _review_runs_has_legacy_status_check(connection):
        return

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN")
        connection.execute("DROP TABLE IF EXISTS review_runs_migration")
        connection.execute(
            """
            CREATE TABLE review_runs_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                trigger_comment_id INTEGER,
                trigger_user TEXT NOT NULL DEFAULT '',
                base_sha TEXT NOT NULL DEFAULT '',
                head_sha TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN ('running', 'generated', 'failed')),
                findings_count INTEGER CHECK (findings_count IS NULL OR findings_count >= 0),
                posted_comment_id INTEGER,
                started_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO review_runs_migration (
                id, repository, pr_number, trigger_comment_id, trigger_user, base_sha, head_sha,
                status, findings_count, posted_comment_id, started_at, completed_at
            )
            SELECT id, repository, pr_number, trigger_comment_id, trigger_user,
                   COALESCE(base_sha, ''), head_sha,
                   CASE WHEN status = 'done' THEN 'generated' ELSE status END,
                   findings_count, posted_comment_id, started_at, completed_at
            FROM review_runs
            """
        )
        connection.execute("DROP TABLE review_runs")
        connection.execute("ALTER TABLE review_runs_migration RENAME TO review_runs")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_review_runs_repo_started
                ON review_runs(repository, started_at DESC)
            """
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")

    broken = connection.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise ReviewMemoryError("review_runs migration left broken foreign keys")


def _migrate_review_runs_lifecycle(connection: sqlite3.Connection) -> None:
    if not _review_runs_needs_lifecycle_migration(connection):
        return

    existing_columns = {
        str(row["name"]) for row in connection.execute("PRAGMA table_info(review_runs)")
    }
    rows = [dict(row) for row in connection.execute("SELECT * FROM review_runs")]
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN")
        connection.execute("DROP TABLE IF EXISTS review_runs_migration")
        connection.execute(
            """
            CREATE TABLE review_runs_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                trigger_comment_id INTEGER,
                trigger_user TEXT NOT NULL DEFAULT '',
                base_sha TEXT NOT NULL DEFAULT '',
                head_sha TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN ('running', 'generated', 'failed')),
                phase TEXT NOT NULL CHECK (
                    phase IN (
                        'accepted', 'fetching_pr', 'collecting_diff', 'reviewing',
                        'rendering', 'publishing', 'posted', 'failed'
                    )
                ),
                findings_count INTEGER CHECK (findings_count IS NULL OR findings_count >= 0),
                changed_files_reported INTEGER CHECK (
                    changed_files_reported IS NULL OR changed_files_reported >= 0
                ),
                changed_files_registered INTEGER CHECK (
                    changed_files_registered IS NULL OR changed_files_registered >= 0
                ),
                changed_file_registration_complete INTEGER NOT NULL DEFAULT 0 CHECK (
                    changed_file_registration_complete IN (0, 1)
                ),
                posted_comment_id INTEGER,
                failure_code TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                last_heartbeat_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT,
                CHECK (
                    (status = 'running' AND phase IN (
                        'accepted', 'fetching_pr', 'collecting_diff',
                        'reviewing', 'rendering', 'publishing'
                    ))
                    OR (status = 'generated' AND phase = 'posted')
                    OR (status = 'failed' AND phase = 'failed')
                )
            )
            """
        )
        for row in rows:
            status = "generated" if row.get("status") == "done" else str(row["status"])
            if status not in {"running", "generated", "failed"}:
                status = "failed"
            phase = str(row.get("phase") or "")
            if status == "generated":
                phase = "posted"
            elif status == "failed":
                phase = "failed"
            elif phase not in {
                "accepted",
                "fetching_pr",
                "collecting_diff",
                "reviewing",
                "rendering",
                "publishing",
            }:
                phase = "accepted"
            started_at = str(row["started_at"])
            completed_at = row.get("completed_at")
            heartbeat = (
                str(row.get("last_heartbeat_at") or "")
                if "last_heartbeat_at" in existing_columns
                else ""
            )
            if not heartbeat:
                heartbeat = str(completed_at or started_at)
            connection.execute(
                """
                INSERT INTO review_runs_migration (
                    id, repository, pr_number, trigger_comment_id, trigger_user,
                    base_sha, head_sha, status, phase, findings_count,
                    changed_files_reported, changed_files_registered,
                    changed_file_registration_complete, posted_comment_id,
                    failure_code, started_at,
                    last_heartbeat_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["repository"],
                    row["pr_number"],
                    row.get("trigger_comment_id"),
                    row.get("trigger_user") or "",
                    row.get("base_sha") or "",
                    row.get("head_sha") or "",
                    status,
                    phase,
                    row.get("findings_count"),
                    row.get("changed_files_reported")
                    if "changed_files_reported" in existing_columns
                    else None,
                    row.get("changed_files_registered")
                    if "changed_files_registered" in existing_columns
                    else None,
                    int(row.get("changed_file_registration_complete") or 0)
                    if "changed_file_registration_complete" in existing_columns
                    else 0,
                    row.get("posted_comment_id"),
                    row.get("failure_code") or "",
                    started_at,
                    heartbeat,
                    completed_at,
                ),
            )
        connection.execute("DROP TABLE review_runs")
        connection.execute("ALTER TABLE review_runs_migration RENAME TO review_runs")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_review_runs_repo_started
                ON review_runs(repository, started_at DESC)
            """
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")

    broken = connection.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise ReviewMemoryError("review_runs lifecycle migration left broken foreign keys")


def _migrate_finding_observations_run_scope(connection: sqlite3.Connection) -> None:
    if not _finding_observations_needs_run_scope_migration(connection):
        return

    rows = [dict(row) for row in connection.execute("SELECT * FROM finding_observations")]
    existing_columns = {
        str(row["name"])
        for row in connection.execute("PRAGMA table_info(finding_observations)")
    }
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("BEGIN")
        connection.execute("DROP TABLE IF EXISTS finding_observations_migration")
        connection.execute(
            """
            CREATE TABLE finding_observations_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                review_subject_id INTEGER NOT NULL,
                review_run_id INTEGER,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                policy_revision TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                path TEXT NOT NULL,
                line INTEGER NOT NULL,
                symbol TEXT NOT NULL DEFAULT '',
                anchor TEXT NOT NULL,
                title TEXT NOT NULL,
                severity TEXT NOT NULL,
                category TEXT NOT NULL,
                publication_score INTEGER NOT NULL,
                confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                context_hash TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL,
                disproof_checks TEXT NOT NULL DEFAULT '',
                impact TEXT NOT NULL DEFAULT '',
                smallest_fix TEXT NOT NULL,
                introduced_by_diff INTEGER NOT NULL CHECK (introduced_by_diff IN (0, 1)),
                observed_at TEXT NOT NULL,
                FOREIGN KEY (review_subject_id) REFERENCES review_subjects(id),
                FOREIGN KEY (review_run_id) REFERENCES review_runs(id),
                FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint)
            )
            """
        )
        for row in rows:
            connection.execute(
                """
                INSERT INTO finding_observations_migration (
                    id, review_subject_id, review_run_id, repository, pr_number,
                    head_sha, policy_revision, fingerprint, rule_id, path, line,
                    symbol, anchor, title, severity, category, publication_score,
                    confidence, context_hash, evidence, disproof_checks, impact,
                    smallest_fix, introduced_by_diff, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["review_subject_id"],
                    row.get("review_run_id") if "review_run_id" in existing_columns else None,
                    row["repository"],
                    row["pr_number"],
                    row["head_sha"],
                    row["policy_revision"],
                    row["fingerprint"],
                    row["rule_id"],
                    row["path"],
                    row["line"],
                    row["symbol"],
                    row["anchor"],
                    row["title"],
                    row["severity"],
                    row["category"],
                    row["publication_score"],
                    row["confidence"],
                    row["context_hash"],
                    row["evidence"],
                    row["disproof_checks"],
                    row["impact"],
                    row["smallest_fix"],
                    row["introduced_by_diff"],
                    row["observed_at"],
                ),
            )
        connection.execute("DROP TABLE finding_observations")
        connection.execute(
            "ALTER TABLE finding_observations_migration RENAME TO finding_observations"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys = ON")

    broken = connection.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise ReviewMemoryError(
            "finding_observations run-scope migration left broken foreign keys"
        )


def _ensure_active_run_unique_index(connection: sqlite3.Connection) -> None:
    moment = isoformat(utc_now())
    duplicate_groups = connection.execute(
        """
        SELECT repository, pr_number, MAX(id) AS keep_id
        FROM review_runs
        WHERE status = 'running'
        GROUP BY repository, pr_number
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    with connection:
        for row in duplicate_groups:
            connection.execute(
                """
                UPDATE review_runs
                SET status = 'failed',
                    phase = 'failed',
                    completed_at = ?,
                    last_heartbeat_at = ?,
                    failure_code = 'superseded_duplicate_migration'
                WHERE repository = ?
                  AND pr_number = ?
                  AND status = 'running'
                  AND id != ?
                """,
                (
                    moment,
                    moment,
                    row["repository"],
                    int(row["pr_number"]),
                    int(row["keep_id"]),
                ),
            )
    connection.execute("DROP INDEX IF EXISTS uq_review_runs_active_pr")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_review_runs_active_pr
            ON review_runs(repository, pr_number)
            WHERE status = 'running'
        """
    )


def _backfill_review_subject_id(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    head_sha: str,
    *,
    policy_revision: str,
    now: datetime,
) -> int:
    repository = normalize_repository(repository)
    if int(pr_number) < 1:
        raise ReviewMemoryError("pr_number must be positive")
    head_sha = clean_text(head_sha, field="head_sha", maximum=64).lower()
    if not HASH_RE.fullmatch(head_sha):
        raise ReviewMemoryError(
            "head_sha must be an exact 40 to 64 character hexadecimal commit SHA"
        )
    connection.execute(
        """
        INSERT OR IGNORE INTO review_subjects (
            repository, pr_number, head_sha, policy_revision, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (repository, int(pr_number), head_sha, policy_revision, isoformat(now)),
    )
    row = connection.execute(
        """
        SELECT id FROM review_subjects
        WHERE repository = ? AND pr_number = ? AND head_sha = ? AND policy_revision = ?
        """,
        (repository, int(pr_number), head_sha, policy_revision),
    ).fetchone()
    if not row:
        raise ReviewMemoryError("failed to create review subject")
    return int(row["id"])


def _backfill_observations(connection: sqlite3.Connection) -> None:
    """Create one observation from each legacy findings row.

    The old schema stored only the latest observation on the identity row. Earlier
    observations overwritten by ON CONFLICT are not recoverable, so this backfill
    deliberately preserves the current retained value and no more.
    """
    rows = connection.execute(
        """
        SELECT fingerprint, repository, rule_id, path, line, symbol, anchor, title,
               severity, category, publication_score, confidence, context_hash,
               pr_number, head_sha, evidence, disproof_checks, impact, smallest_fix,
               introduced_by_diff, last_seen_at
        FROM findings
        """
    ).fetchall()
    if not rows:
        return

    with connection:
        for row in rows:
            item = dict(row)
            policy_revision = current_policy_revision()
            subject_id = _backfill_review_subject_id(
                connection,
                item["repository"],
                int(item["pr_number"]),
                item["head_sha"],
                policy_revision=policy_revision,
                now=parse_time(item.get("last_seen_at")) or utc_now(),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO finding_observations (
                    review_subject_id, review_run_id, repository, pr_number,
                    head_sha, policy_revision, fingerprint, rule_id, path, line,
                    symbol, anchor, title, severity, category, publication_score,
                    confidence, context_hash, evidence, disproof_checks, impact,
                    smallest_fix, introduced_by_diff, observed_at
                ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    subject_id,
                    item["repository"],
                    int(item["pr_number"]),
                    item["head_sha"],
                    policy_revision,
                    item["fingerprint"],
                    item["rule_id"],
                    item["path"],
                    int(item["line"] or 1),
                    item["symbol"],
                    item["anchor"],
                    item["title"],
                    item["severity"],
                    item["category"],
                    int(item["publication_score"]),
                    float(item["confidence"]),
                    item["context_hash"],
                    item["evidence"],
                    item["disproof_checks"],
                    item["impact"],
                    item["smallest_fix"],
                    int(item["introduced_by_diff"]),
                    item["last_seen_at"],
                ),
            )


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
            severity TEXT NOT NULL,
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
                decision IN (
                    'false_positive', 'intentional_by_design', 'accepted_risk',
                    'duplicate', 'resolved', 'reopen'
                )
            ),
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            context_hash TEXT NOT NULL DEFAULT '',
            adr_id TEXT NOT NULL DEFAULT '',
            observation_id INTEGER,
            created_at TEXT NOT NULL,
            expires_at TEXT,
            FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint),
            FOREIGN KEY (observation_id) REFERENCES finding_observations(id)
        );

        CREATE INDEX IF NOT EXISTS idx_decisions_fingerprint
            ON decisions(fingerprint, id DESC);

        CREATE TABLE IF NOT EXISTS review_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            trigger_comment_id INTEGER,
            trigger_user TEXT NOT NULL DEFAULT '',
            base_sha TEXT NOT NULL DEFAULT '',
            head_sha TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (status IN ('running', 'generated', 'failed')),
            phase TEXT NOT NULL CHECK (
                phase IN (
                    'accepted', 'fetching_pr', 'collecting_diff', 'reviewing',
                    'rendering', 'publishing', 'posted', 'failed'
                )
            ) DEFAULT 'accepted',
            findings_count INTEGER CHECK (findings_count IS NULL OR findings_count >= 0),
            posted_comment_id INTEGER,
            failure_code TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL,
            last_heartbeat_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT,
            CHECK (
                (status = 'running' AND phase IN (
                    'accepted', 'fetching_pr', 'collecting_diff',
                    'reviewing', 'rendering', 'publishing'
                ))
                OR (status = 'generated' AND phase = 'posted')
                OR (status = 'failed' AND phase = 'failed')
            )
        );

        CREATE INDEX IF NOT EXISTS idx_review_runs_repo_started
            ON review_runs(repository, started_at DESC);

        CREATE TABLE IF NOT EXISTS review_run_files (
            run_id INTEGER NOT NULL,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            path TEXT NOT NULL,
                change_status TEXT NOT NULL DEFAULT '',
                is_changed_path INTEGER NOT NULL DEFAULT 0 CHECK (is_changed_path IN (0, 1)),
                domain TEXT NOT NULL DEFAULT '',
                review_mode TEXT NOT NULL DEFAULT 'normal',
                diff_requested INTEGER NOT NULL DEFAULT 0 CHECK (diff_requested IN (0, 1)),
                diff_returned INTEGER NOT NULL DEFAULT 0 CHECK (diff_returned IN (0, 1)),
                diff_truncated INTEGER NOT NULL DEFAULT 0 CHECK (diff_truncated IN (0, 1)),
                diff_state TEXT NOT NULL DEFAULT 'unseen' CHECK (
                    diff_state IN ('unseen', 'complete', 'truncated', 'unavailable')
                ),
            head_ranges_read_json TEXT NOT NULL DEFAULT '[]',
            base_ranges_read_json TEXT NOT NULL DEFAULT '[]',
            unavailable_reason TEXT NOT NULL DEFAULT '',
            first_accessed_at TEXT NOT NULL,
            last_accessed_at TEXT NOT NULL,
            PRIMARY KEY (run_id, path),
            FOREIGN KEY (run_id) REFERENCES review_runs(id)
        );

        CREATE TABLE IF NOT EXISTS coach_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL DEFAULT '',
            source_event_set_id TEXT NOT NULL,
            source_snapshot_id TEXT NOT NULL DEFAULT '',
            proposal_set_id TEXT NOT NULL,
            decision TEXT NOT NULL CHECK (decision IN ('propose', 'no_change')),
            events_considered INTEGER NOT NULL CHECK (events_considered >= 0),
            candidates_count INTEGER NOT NULL CHECK (candidates_count >= 0),
            artifact_dir TEXT NOT NULL DEFAULT '',
            recorded_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_coach_runs_repo_recorded
            ON coach_runs(repository, recorded_at DESC);

        CREATE TABLE IF NOT EXISTS coach_candidates (
            repository TEXT NOT NULL DEFAULT '',
            candidate_key TEXT NOT NULL,
            proposal_set_id TEXT NOT NULL,
            source_event_set_id TEXT NOT NULL,
            target_owner TEXT NOT NULL,
            suggested_route TEXT NOT NULL,
            event_type TEXT NOT NULL,
            independent_episode_count INTEGER NOT NULL CHECK (
                independent_episode_count >= 1
            ),
            evidence_event_ids_json TEXT NOT NULL,
            evidence_events_total INTEGER NOT NULL CHECK (evidence_events_total >= 0),
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL DEFAULT 1 CHECK (seen_count >= 1),
            PRIMARY KEY (repository, candidate_key)
        );

        CREATE INDEX IF NOT EXISTS idx_coach_candidates_repo_seen
            ON coach_candidates(repository, last_seen_at DESC);

        CREATE TABLE IF NOT EXISTS review_subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            base_sha TEXT NOT NULL DEFAULT '',
            head_sha TEXT NOT NULL,
            policy_revision TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(repository, pr_number, base_sha, head_sha, policy_revision)
        );

        CREATE INDEX IF NOT EXISTS idx_review_subjects_repo_pr
            ON review_subjects(repository, pr_number, id DESC);

        CREATE TABLE IF NOT EXISTS finding_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_subject_id INTEGER NOT NULL,
            review_run_id INTEGER,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            policy_revision TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            path TEXT NOT NULL,
            line INTEGER NOT NULL,
            symbol TEXT NOT NULL DEFAULT '',
            anchor TEXT NOT NULL,
            title TEXT NOT NULL,
            severity TEXT NOT NULL,
            category TEXT NOT NULL,
            publication_score INTEGER NOT NULL,
            confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
            context_hash TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL,
            disproof_checks TEXT NOT NULL DEFAULT '',
            impact TEXT NOT NULL DEFAULT '',
            smallest_fix TEXT NOT NULL,
            introduced_by_diff INTEGER NOT NULL CHECK (introduced_by_diff IN (0, 1)),
            observed_at TEXT NOT NULL,
            FOREIGN KEY (review_subject_id) REFERENCES review_subjects(id),
            FOREIGN KEY (review_run_id) REFERENCES review_runs(id),
            FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint)
        );

        CREATE INDEX IF NOT EXISTS idx_observations_repo_pr_path_seen
            ON finding_observations(repository, pr_number, path, observed_at DESC);

        CREATE INDEX IF NOT EXISTS idx_observations_fingerprint_seen
            ON finding_observations(fingerprint, observed_at DESC);

        CREATE TABLE IF NOT EXISTS pr_finding_references (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            local_reference TEXT NOT NULL,
            first_assigned_at TEXT NOT NULL,
            UNIQUE(repository, pr_number, fingerprint),
            UNIQUE(repository, pr_number, local_reference),
            FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint)
        );

        CREATE TABLE IF NOT EXISTS review_publications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_run_id INTEGER UNIQUE,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            base_sha TEXT NOT NULL DEFAULT '',
            head_sha TEXT NOT NULL,
            policy_revision TEXT NOT NULL,
            publication_key TEXT NOT NULL DEFAULT '',
            comment_id INTEGER,
            rendered_markdown TEXT,
            rendered_blocks_json TEXT NOT NULL DEFAULT '',
            rendered_hash TEXT NOT NULL DEFAULT '',
            delivery_status TEXT NOT NULL DEFAULT 'generated' CHECK (
                delivery_status IN (
                    'legacy_unverified', 'generated', 'posting', 'posted',
                    'publish_failed', 'stale'
                )
            ),
            published_at TEXT NOT NULL,
            generated_at TEXT NOT NULL DEFAULT '',
            posting_started_at TEXT,
            posted_at TEXT,
            publish_failed_at TEXT,
            failure_code TEXT NOT NULL DEFAULT '',
            superseded_at TEXT,
            review_number INTEGER,
            supersedes_publication_id INTEGER,
            superseded_by_publication_id INTEGER,
            supersession_rendered_at TEXT,
            supersession_failure_code TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (supersedes_publication_id) REFERENCES review_publications(id),
            FOREIGN KEY (superseded_by_publication_id) REFERENCES review_publications(id),
            FOREIGN KEY (review_run_id) REFERENCES review_runs(id)
        );

        CREATE TABLE IF NOT EXISTS review_publication_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publication_id INTEGER NOT NULL,
            part_number INTEGER NOT NULL CHECK (part_number >= 1),
            comment_id INTEGER NOT NULL CHECK (comment_id >= 1),
            posted_at TEXT NOT NULL,
            UNIQUE(publication_id, part_number),
            FOREIGN KEY (publication_id) REFERENCES review_publications(id)
        );

        CREATE TABLE IF NOT EXISTS publication_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publication_id INTEGER NOT NULL,
            local_reference TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            observation_id INTEGER,
            context_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'current' CHECK (
                status IN ('current', 'resolved')
            ),
            UNIQUE(publication_id, local_reference),
            UNIQUE(publication_id, fingerprint),
            FOREIGN KEY (publication_id) REFERENCES review_publications(id),
            FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint),
            FOREIGN KEY (observation_id) REFERENCES finding_observations(id)
        );

        CREATE TABLE IF NOT EXISTS review_quality_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            publication_id INTEGER,
            head_sha TEXT NOT NULL DEFAULT '',
            local_reference TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            actor_user_id TEXT NOT NULL,
            actor_login TEXT NOT NULL DEFAULT '',
            author_association TEXT NOT NULL DEFAULT '',
            source_comment_id INTEGER,
            source_comment_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (publication_id) REFERENCES review_publications(id)
        );

        -- Idempotency / replay protection: every processed feedback event id is recorded
        -- once, so a duplicate or replayed delivery is a clean no-op.
        CREATE TABLE IF NOT EXISTS processed_feedback_events (
            event_id TEXT PRIMARY KEY,
            outcome TEXT NOT NULL DEFAULT 'pending',
            processed_at TEXT NOT NULL
        );

        -- Append-only audit trail for decisions recorded via in-PR feedback. One row per
        -- recorded feedback decision, linked to the decisions row it produced.
        CREATE TABLE IF NOT EXISTS decision_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER NOT NULL,
            actor_user_id TEXT NOT NULL,
            actor_login TEXT NOT NULL DEFAULT '',
            author_association TEXT NOT NULL DEFAULT '',
            allowlist_version TEXT NOT NULL DEFAULT '',
            -- Reserved for a future inline bridge; summary-comment feedback writes NULL.
            review_comment_id INTEGER,
            source_comment_id INTEGER,
            source_comment_url TEXT NOT NULL DEFAULT '',
            classifier_version TEXT NOT NULL DEFAULT '',
            classifier_output TEXT NOT NULL DEFAULT '',
            hmac_key_version TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (decision_id) REFERENCES decisions(id)
        );
        """
    )
    existing_version = _user_version(connection)
    # Non-destructive migration from the first starter bundle.
    _ensure_column(
        connection, "findings", "category", "TEXT NOT NULL DEFAULT 'correctness'"
    )
    _ensure_column(
        connection, "findings", "publication_score", "INTEGER NOT NULL DEFAULT 8"
    )
    _ensure_column(connection, "findings", "context_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(
        connection, "review_runs", "base_sha", "TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(connection, "decisions", "context_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "decisions", "adr_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(
        connection,
        "decisions",
        "observation_id",
        "INTEGER REFERENCES finding_observations(id)",
    )
    _ensure_column(
        connection, "finding_observations", "rule_id", "TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(
        connection,
        "processed_feedback_events",
        "outcome",
        "TEXT NOT NULL DEFAULT 'pending'",
    )
    _ensure_column(
        connection,
        "publication_findings",
        "observation_id",
        "INTEGER REFERENCES finding_observations(id)",
    )
    _ensure_column(
        connection,
        "review_quality_feedback",
        "publication_id",
        "INTEGER REFERENCES review_publications(id)",
    )
    _ensure_column(
        connection,
        "review_quality_feedback",
        "head_sha",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        connection, "review_subjects", "base_sha", "TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(
        connection,
        "review_publications",
        "review_run_id",
        "INTEGER REFERENCES review_runs(id)",
    )
    _ensure_column(
        connection, "review_publications", "base_sha", "TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(
        connection, "review_publications", "publication_key", "TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(connection, "review_publications", "rendered_markdown", "TEXT")
    _ensure_column(
        connection,
        "review_publications",
        "rendered_blocks_json",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_column(
        connection,
        "review_publications",
        "delivery_status",
        "TEXT NOT NULL DEFAULT 'legacy_unverified'",
    )
    _ensure_column(
        connection, "review_publications", "generated_at", "TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(connection, "review_publications", "posting_started_at", "TEXT")
    _ensure_column(connection, "review_publications", "posted_at", "TEXT")
    _ensure_column(connection, "review_publications", "publish_failed_at", "TEXT")
    _ensure_column(
        connection, "review_publications", "failure_code", "TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(connection, "review_publications", "superseded_at", "TEXT")
    _ensure_column(connection, "review_publications", "review_number", "INTEGER")
    _ensure_column(
        connection,
        "review_publications",
        "supersedes_publication_id",
        "INTEGER REFERENCES review_publications(id)",
    )
    _ensure_column(
        connection,
        "review_publications",
        "superseded_by_publication_id",
        "INTEGER REFERENCES review_publications(id)",
    )
    _ensure_column(connection, "review_publications", "supersession_rendered_at", "TEXT")
    _ensure_column(
        connection,
        "review_publications",
        "supersession_failure_code",
        "TEXT NOT NULL DEFAULT ''",
    )
    if existing_version < 12:
        counters: dict[tuple[str, int], int] = {}
        rows = connection.execute(
            """
            SELECT id, repository, pr_number
            FROM review_publications
            WHERE review_number IS NULL
              AND delivery_status = 'posted'
              AND comment_id IS NOT NULL
            ORDER BY repository, pr_number, posted_at, generated_at, id
            """
        ).fetchall()
        for row in rows:
            key = (str(row["repository"]), int(row["pr_number"]))
            counters[key] = counters.get(key, 0) + 1
            connection.execute(
                "UPDATE review_publications SET review_number = ? WHERE id = ?",
                (counters[key], int(row["id"])),
            )
    if existing_version < 13:
        connection.execute(
            """
            UPDATE review_publications
            SET review_number = NULL
            WHERE delivery_status != 'posted'
               OR comment_id IS NULL
            """
        )
    connection.execute(
        """
        UPDATE review_publications
        SET generated_at = published_at
        WHERE generated_at = ''
        """
    )
    connection.execute(
        """
        UPDATE review_publications
        SET delivery_status = 'legacy_unverified'
        WHERE delivery_status = ''
           OR delivery_status NOT IN (
                'legacy_unverified', 'generated', 'posting', 'posted',
                'publish_failed', 'stale'
           )
        """
    )
    connection.commit()
    _migrate_findings_severity_check(connection)
    _migrate_decisions_check(connection)
    _migrate_review_runs_status(connection)
    _migrate_review_runs_lifecycle(connection)
    _ensure_column(
        connection,
        "review_runs",
        "changed_files_reported",
        "INTEGER CHECK (changed_files_reported IS NULL OR changed_files_reported >= 0)",
    )
    _ensure_column(
        connection,
        "review_runs",
        "changed_files_registered",
        "INTEGER CHECK (changed_files_registered IS NULL OR changed_files_registered >= 0)",
    )
    _ensure_column(
        connection,
        "review_runs",
        "changed_file_registration_complete",
        "INTEGER NOT NULL DEFAULT 0 CHECK (changed_file_registration_complete IN (0, 1))",
    )
    _ensure_column(
        connection,
        "review_run_files",
        "is_changed_path",
        "INTEGER NOT NULL DEFAULT 0 CHECK (is_changed_path IN (0, 1))",
    )
    _ensure_column(
        connection,
        "review_run_files",
        "diff_state",
        "TEXT NOT NULL DEFAULT 'unseen' CHECK (diff_state IN ('unseen', 'complete', 'truncated', 'unavailable'))",
    )
    connection.execute(
        """
        UPDATE review_run_files
        SET is_changed_path = 1
        WHERE change_status != ''
        """
    )
    connection.execute(
        """
        UPDATE review_run_files
        SET diff_state = CASE
            WHEN unavailable_reason != '' THEN 'unavailable'
            WHEN diff_truncated = 1 THEN 'truncated'
            WHEN diff_returned = 1 THEN 'complete'
            ELSE 'unseen'
        END
        WHERE diff_state = 'unseen'
        """
    )
    connection.execute(
        """
        UPDATE review_runs
        SET changed_files_registered = (
                SELECT COUNT(*)
                FROM review_run_files
                WHERE review_run_files.run_id = review_runs.id
                  AND review_run_files.is_changed_path = 1
            )
        WHERE changed_files_registered IS NULL
        """
    )
    connection.execute(
        """
        UPDATE review_runs
        SET changed_files_reported = changed_files_registered,
            changed_file_registration_complete = CASE
                WHEN changed_files_registered IS NOT NULL
                     AND changed_files_registered > 0 THEN 1
                ELSE changed_file_registration_complete
            END
        WHERE changed_files_reported IS NULL
        """
    )
    _migrate_finding_observations_run_scope(connection)
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_decisions_observation
            ON decisions(observation_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_publication_findings_observation
            ON publication_findings(observation_id)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_review_publications_run
            ON review_publications(review_run_id)
            WHERE review_run_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_review_run_files_repo_pr
            ON review_run_files(repository, pr_number, run_id)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_observations_run_fingerprint
            ON finding_observations(review_run_id, fingerprint)
            WHERE review_run_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_observations_run
            ON finding_observations(review_run_id, id DESC)
            WHERE review_run_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_review_publications_delivery
            ON review_publications(repository, pr_number, delivery_status, id DESC)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_review_publications_current
            ON review_publications(repository, pr_number, superseded_at, id DESC)
        """
    )
    connection.execute(
        """
        DROP INDEX IF EXISTS uq_review_publication_number
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_review_publication_number
            ON review_publications(repository, pr_number, review_number)
            WHERE review_number IS NOT NULL
              AND delivery_status = 'posted'
              AND comment_id IS NOT NULL
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS review_publication_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publication_id INTEGER NOT NULL,
            part_number INTEGER NOT NULL CHECK (part_number >= 1),
            comment_id INTEGER NOT NULL CHECK (comment_id >= 1),
            posted_at TEXT NOT NULL,
            UNIQUE(publication_id, part_number),
            FOREIGN KEY (publication_id) REFERENCES review_publications(id)
        )
        """
    )
    if existing_version < 8:
        connection.execute(
            """
            INSERT OR IGNORE INTO review_publication_comments (
                publication_id, part_number, comment_id, posted_at
            )
            SELECT id, 1, comment_id, COALESCE(NULLIF(posted_at, ''), published_at)
            FROM review_publications
            WHERE comment_id IS NOT NULL
              AND delivery_status = 'posted'
            """
        )
    _ensure_active_run_unique_index(connection)
    _ensure_current_publication_unique_index(connection)
    if existing_version < OBSERVATION_BACKFILL_VERSION:
        _backfill_observations(connection)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()
