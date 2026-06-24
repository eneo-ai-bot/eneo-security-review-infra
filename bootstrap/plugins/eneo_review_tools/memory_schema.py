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
SCHEMA_VERSION = 4
OBSERVATION_BACKFILL_VERSION = 3


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
                created_at TEXT NOT NULL,
                expires_at TEXT,
                FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO decisions_migration (
                id, fingerprint, decision, reason, actor, context_hash, created_at, expires_at
            )
            SELECT id, fingerprint, decision, reason, actor, context_hash, created_at, expires_at
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
                id, repository, pr_number, trigger_comment_id, trigger_user, head_sha,
                status, findings_count, posted_comment_id, started_at, completed_at
            )
            SELECT id, repository, pr_number, trigger_comment_id, trigger_user, head_sha,
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
                    review_subject_id, repository, pr_number, head_sha, policy_revision,
                    fingerprint, rule_id, path, line, symbol, anchor, title, severity, category,
                    publication_score, confidence, context_hash, evidence, disproof_checks,
                    impact, smallest_fix, introduced_by_diff, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            status TEXT NOT NULL CHECK (status IN ('running', 'generated', 'failed')),
            findings_count INTEGER CHECK (findings_count IS NULL OR findings_count >= 0),
            posted_comment_id INTEGER,
            started_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_review_runs_repo_started
            ON review_runs(repository, started_at DESC);

        CREATE TABLE IF NOT EXISTS review_subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            policy_revision TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(repository, pr_number, head_sha, policy_revision)
        );

        CREATE INDEX IF NOT EXISTS idx_review_subjects_repo_pr
            ON review_subjects(repository, pr_number, id DESC);

        CREATE TABLE IF NOT EXISTS finding_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_subject_id INTEGER NOT NULL,
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
            UNIQUE(review_subject_id, fingerprint),
            FOREIGN KEY (review_subject_id) REFERENCES review_subjects(id),
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
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            head_sha TEXT NOT NULL,
            policy_revision TEXT NOT NULL,
            comment_id INTEGER,
            rendered_hash TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL,
            superseded_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_review_publications_current
            ON review_publications(repository, pr_number, superseded_at, id DESC);

        CREATE TABLE IF NOT EXISTS publication_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            publication_id INTEGER NOT NULL,
            local_reference TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            context_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'current' CHECK (
                status IN ('current', 'resolved')
            ),
            UNIQUE(publication_id, local_reference),
            UNIQUE(publication_id, fingerprint),
            FOREIGN KEY (publication_id) REFERENCES review_publications(id),
            FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint)
        );

        CREATE TABLE IF NOT EXISTS review_quality_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            local_reference TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            actor_user_id TEXT NOT NULL,
            actor_login TEXT NOT NULL DEFAULT '',
            author_association TEXT NOT NULL DEFAULT '',
            source_comment_id INTEGER,
            source_comment_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );

        -- Authority mapping: which finding a posted inline review comment belongs to.
        -- Stored at publish time; the sole source for routing a threaded reply back to a
        -- finding. The comment footer text is never trusted for this.
        CREATE TABLE IF NOT EXISTS review_comment_links (
            review_comment_id INTEGER PRIMARY KEY,
            repository TEXT NOT NULL,
            pr_number INTEGER NOT NULL,
            fingerprint TEXT NOT NULL,
            context_hash TEXT NOT NULL DEFAULT '',
            head_sha TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint)
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
    # Non-destructive migration from the first starter bundle.
    _ensure_column(
        connection, "findings", "category", "TEXT NOT NULL DEFAULT 'correctness'"
    )
    _ensure_column(
        connection, "findings", "publication_score", "INTEGER NOT NULL DEFAULT 8"
    )
    _ensure_column(connection, "findings", "context_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "decisions", "context_hash", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(connection, "decisions", "adr_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(
        connection, "finding_observations", "rule_id", "TEXT NOT NULL DEFAULT ''"
    )
    _ensure_column(
        connection,
        "processed_feedback_events",
        "outcome",
        "TEXT NOT NULL DEFAULT 'pending'",
    )
    connection.commit()
    existing_version = _user_version(connection)
    _migrate_findings_severity_check(connection)
    _migrate_decisions_check(connection)
    _migrate_review_runs_status(connection)
    if existing_version < OBSERVATION_BACKFILL_VERSION:
        _backfill_observations(connection)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.commit()
