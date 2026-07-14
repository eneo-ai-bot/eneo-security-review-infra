"""Review run lifecycle state."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Literal, cast

try:
    from .memory_validation import (
        ReviewMemoryError,
        clean_text,
        isoformat,
        normalize_repository,
        parse_time,
        utc_now,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_validation import (
        ReviewMemoryError,
        clean_text,
        isoformat,
        normalize_repository,
        parse_time,
        utc_now,
    )


RunPhase = Literal[
    "accepted",
    "fetching_pr",
    "collecting_diff",
    "reviewing",
    "rendering",
    "publishing",
    "posted",
    "failed",
]

RUNNING_PHASES = frozenset(
    {
        "accepted",
        "fetching_pr",
        "collecting_diff",
        "reviewing",
        "rendering",
        "publishing",
    }
)
TERMINAL_PHASE_BY_STATUS = {"generated": "posted", "failed": "failed"}


def _running_phase(value: str) -> RunPhase:
    if value not in RUNNING_PHASES:
        raise ReviewMemoryError("phase must be an active review phase")
    return cast(RunPhase, value)


def _active_run(
    connection: sqlite3.Connection, repository: str, pr_number: int
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT *
        FROM review_runs
        WHERE repository = ?
          AND pr_number = ?
          AND status = 'running'
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (repository, int(pr_number)),
    ).fetchone()
    return dict(row) if row else None


def _duplicate_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "repository": row["repository"],
        "pr_number": int(row["pr_number"]),
        "status": "duplicate",
        "phase": row.get("phase") or "accepted",
        "started_at": row["started_at"],
        "last_heartbeat_at": row.get("last_heartbeat_at") or row["started_at"],
        "existing_run_id": int(row["id"]),
        "message": (
            f"A review is already running for {row['repository']}#{row['pr_number']} "
            f"as run #{row['id']}."
        ),
    }


def start_run(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    *,
    trigger_comment_id: int | None = None,
    trigger_user: str = "",
    base_sha: str = "",
    head_sha: str = "",
    force: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the start of one run-owned review lifecycle for a PR snapshot."""
    repository = normalize_repository(repository)
    if pr_number < 1:
        raise ReviewMemoryError("pr_number must be positive")
    base_sha = clean_text(base_sha, field="base_sha", maximum=64, required=False).lower()
    head_sha = clean_text(head_sha, field="head_sha", maximum=64, required=False).lower()
    moment = now or utc_now()
    mark_stale_runs_failed(
        connection,
        repository=repository,
        pr_number=pr_number,
        now=moment,
    )
    started = isoformat(moment)
    connection.execute("BEGIN IMMEDIATE")
    try:
        if force:
            connection.execute(
                """
                UPDATE review_runs
                SET status = 'failed',
                    phase = 'failed',
                    completed_at = ?,
                    last_heartbeat_at = ?,
                    failure_code = 'superseded_by_force'
                WHERE repository = ?
                  AND pr_number = ?
                  AND status = 'running'
                """,
                (started, started, repository, int(pr_number)),
            )
        else:
            existing = _active_run(connection, repository, pr_number)
            if existing is not None:
                connection.commit()
                return _duplicate_response(existing)
        cursor = connection.execute(
            """
            INSERT INTO review_runs (
                repository, pr_number, trigger_comment_id, trigger_user, base_sha, head_sha,
                status, phase, started_at, last_heartbeat_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', 'accepted', ?, ?)
            """,
            (
                repository,
                pr_number,
                # Audit only; webhook redelivery idempotency belongs to the gateway.
                int(trigger_comment_id) if trigger_comment_id is not None else None,
                clean_text(
                    trigger_user, field="trigger_user", maximum=200, required=False
                ),
                base_sha,
                head_sha,
                started,
                started,
            ),
        )
        connection.commit()
    except sqlite3.IntegrityError:
        connection.rollback()
        existing = _active_run(connection, repository, pr_number)
        if existing is not None:
            return _duplicate_response(existing)
        raise
    except Exception:
        connection.rollback()
        raise
    if cursor.lastrowid is None:
        raise ReviewMemoryError("failed to start review run")
    run_id = cursor.lastrowid
    return {
        "id": run_id,
        "repository": repository,
        "pr_number": pr_number,
        "trigger_comment_id": trigger_comment_id,
        "base_sha": base_sha,
        "status": "running",
        "phase": "accepted",
        "started_at": started,
        "last_heartbeat_at": started,
    }


def mark_stale_runs_failed(
    connection: sqlite3.Connection,
    *,
    older_than_minutes: int = 30,
    repository: str | None = None,
    pr_number: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Move abandoned running runs to failed.

    Review execution itself is not resumable. This cleanup keeps operator state honest
    after a container restart or crashed model call without introducing a queue.
    """
    if isinstance(older_than_minutes, bool) or int(older_than_minutes) < 1:
        raise ReviewMemoryError("older_than_minutes must be a positive integer")
    older_than = int(older_than_minutes)
    moment = now or utc_now()
    cutoff = isoformat(moment - timedelta(minutes=older_than))
    completed = isoformat(moment)

    conditions = [
        "status = 'running'",
        "COALESCE(NULLIF(last_heartbeat_at, ''), started_at) < ?",
    ]
    params: list[Any] = [cutoff]
    repo = normalize_repository(repository) if repository else None
    if repo is not None:
        conditions.append("repository = ?")
        params.append(repo)
    if pr_number is not None:
        if isinstance(pr_number, bool) or int(pr_number) < 1:
            raise ReviewMemoryError("pr_number must be positive")
        conditions.append("pr_number = ?")
        params.append(int(pr_number))
    where = " AND ".join(conditions)

    with connection:
        rows = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT id, repository, pr_number, status, phase, findings_count,
                       posted_comment_id, failure_code, started_at,
                       last_heartbeat_at, completed_at
                FROM review_runs
                WHERE {where}
                ORDER BY started_at ASC, id ASC
                """,
                params,
            ).fetchall()
        ]
        if rows:
            connection.execute(
                f"""
                UPDATE review_runs
                SET status = 'failed',
                    phase = 'failed',
                    completed_at = ?,
                    last_heartbeat_at = ?,
                    failure_code = 'stale_timeout'
                WHERE {where}
                """,
                [completed, completed, *params],
            )
            run_ids = [int(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in run_ids)
            connection.execute(
                f"""
                UPDATE review_publications
                SET delivery_status = 'publish_failed',
                    publish_failed_at = ?,
                    failure_code = 'stale_timeout',
                    suggestion_delivery_status = CASE
                        WHEN suggestion_delivery_status = 'posting'
                        THEN 'publish_failed'
                        ELSE suggestion_delivery_status
                    END,
                    suggestion_failure_code = CASE
                        WHEN suggestion_delivery_status = 'posting'
                        THEN 'stale_timeout'
                        ELSE suggestion_failure_code
                    END
                WHERE review_run_id IN ({placeholders})
                  AND delivery_status = 'posting'
                """,
                (completed, *run_ids),
            )

    for row in rows:
        row["status"] = "failed"
        row["phase"] = "failed"
        row["failure_code"] = "stale_timeout"  # canonical: failure_codes.STALE_TIMEOUT
        row["last_heartbeat_at"] = completed
        row["completed_at"] = completed
    return {
        "failed_count": len(rows),
        "older_than_minutes": older_than,
        "cutoff": cutoff,
        "repository": repo,
        "pr_number": pr_number,
        "completed_at": completed,
        "runs": rows,
    }


def get_run(connection: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    """Read a review run row by id at any status. Pure storage; no GitHub awareness."""
    if isinstance(run_id, bool) or int(run_id) < 1:
        raise ReviewMemoryError("run_id must be a positive integer")
    row = connection.execute(
        "SELECT * FROM review_runs WHERE id = ?", (int(run_id),)
    ).fetchone()
    return dict(row) if row else None


def record_failure_status_comment(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    comment_id: int,
    posted_at: str,
) -> bool:
    """Persist the durable failure-status comment id.

    Intentionally NOT gated to running runs — the failure-status comment is posted for
    terminal status='failed' rows by the reaper / delivery-failure paths.
    """
    if isinstance(run_id, bool) or int(run_id) < 1:
        raise ReviewMemoryError("run_id must be a positive integer")
    if isinstance(comment_id, bool) or int(comment_id) < 1:
        raise ReviewMemoryError("comment_id must be a positive integer")
    with connection:
        cursor = connection.execute(
            """
            UPDATE review_runs
            SET failure_status_comment_id = ?,
                failure_status_posted_at = ?
            WHERE id = ?
            """,
            (int(comment_id), str(posted_at), int(run_id)),
        )
    return cursor.rowcount > 0


def clear_failure_status_comment(connection: sqlite3.Connection, run_id: int) -> None:
    """Clear the failure-status comment id once a real review supersedes it."""
    with connection:
        connection.execute(
            """
            UPDATE review_runs
            SET failure_status_comment_id = NULL,
                failure_status_posted_at = ''
            WHERE id = ?
            """,
            (int(run_id),),
        )


def failure_status_comments_for_pr(
    connection: sqlite3.Connection, repository: str, pr_number: int
) -> list[dict[str, int]]:
    """Stored failure-status comments for a PR, so a successful review can clean them up."""
    rows = connection.execute(
        """
        SELECT id, failure_status_comment_id
        FROM review_runs
        WHERE repository = ?
          AND pr_number = ?
          AND failure_status_comment_id IS NOT NULL
        """,
        (normalize_repository(repository), int(pr_number)),
    ).fetchall()
    return [
        {"run_id": int(row["id"]), "comment_id": int(row["failure_status_comment_id"])}
        for row in rows
    ]


def failed_runs_needing_status(
    connection: sqlite3.Connection,
    *,
    repository: str | None = None,
    pr_number: int | None = None,
) -> list[dict[str, Any]]:
    """Failed runs that have not yet had a failure-status comment posted.

    The reaper posts a deterministic status for these so an aborted review is never
    silent on the PR. Pure read; the CLI orchestrates the publishing.
    """
    conditions = ["status = 'failed'", "failure_status_comment_id IS NULL"]
    params: list[Any] = []
    if repository is not None:
        conditions.append("repository = ?")
        params.append(normalize_repository(repository))
    if pr_number is not None:
        if isinstance(pr_number, bool) or int(pr_number) < 1:
            raise ReviewMemoryError("pr_number must be positive")
        conditions.append("pr_number = ?")
        params.append(int(pr_number))
    rows = connection.execute(
        f"""
        SELECT id, repository, pr_number, head_sha, failure_code
        FROM review_runs
        WHERE {" AND ".join(conditions)}
        ORDER BY id ASC
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def update_run_phase(
    connection: sqlite3.Connection,
    run_id: int,
    phase: str,
    *,
    repository: str | None = None,
    pr_number: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Heartbeat an active review run at a known phase."""
    phase = _running_phase(phase)
    if isinstance(run_id, bool) or int(run_id) < 1:
        raise ReviewMemoryError("run_id must be a positive integer")
    conditions = ["id = ?", "status = 'running'"]
    params: list[Any] = [int(run_id)]
    if repository is not None:
        conditions.append("repository = ?")
        params.append(normalize_repository(repository))
    if pr_number is not None:
        if isinstance(pr_number, bool) or int(pr_number) < 1:
            raise ReviewMemoryError("pr_number must be positive")
        conditions.append("pr_number = ?")
        params.append(int(pr_number))
    heartbeat = isoformat(now)
    with connection:
        cursor = connection.execute(
            f"""
            UPDATE review_runs
            SET phase = ?,
                last_heartbeat_at = ?
            WHERE {" AND ".join(conditions)}
            """,
            (phase, heartbeat, *params),
        )
    if cursor.rowcount == 0:
        return None
    return {"id": int(run_id), "phase": phase, "last_heartbeat_at": heartbeat}


def validate_run_snapshot(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    repository: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
) -> dict[str, Any]:
    """Validate that a tool call still belongs to the active review snapshot."""
    if isinstance(run_id, bool) or int(run_id) < 1:
        raise ReviewMemoryError("run_id must be a positive integer")
    if isinstance(pr_number, bool) or int(pr_number) < 1:
        raise ReviewMemoryError("pr_number must be positive")
    repository = normalize_repository(repository)
    base_sha = clean_text(base_sha, field="base_sha", maximum=64, required=False).lower()
    head_sha = clean_text(head_sha, field="head_sha", maximum=64).lower()
    row = connection.execute(
        """
        SELECT id, repository, pr_number, base_sha, head_sha, status, phase
        FROM review_runs
        WHERE id = ?
        """,
        (int(run_id),),
    ).fetchone()
    if row is None:
        raise ReviewMemoryError("run_id does not match a recorded review run")
    if (
        str(row["repository"]) != repository
        or int(row["pr_number"]) != int(pr_number)
    ):
        raise ReviewMemoryError("run_id does not match this pull request")
    if str(row["status"]) != "running":
        raise ReviewMemoryError("run_id is not an active review run")
    recorded_base = str(row["base_sha"] or "").lower()
    recorded_head = str(row["head_sha"] or "").lower()
    if recorded_base and base_sha and recorded_base != base_sha:
        raise ReviewMemoryError("pull request base SHA changed during review")
    if recorded_head and recorded_head != head_sha:
        raise ReviewMemoryError("pull request head SHA changed during review")
    return dict(row)


def complete_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    repository: str | None = None,
    pr_number: int | None = None,
    status: str = "generated",
    findings_count: int | None = None,
    posted_comment_id: int | None = None,
    failure_code: str = "",
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Mark one specific running run (by id) as generated or failed, atomically. Completing
    by id — not by latest-running — prevents one review from completing another when
    reviews of the same pull request overlap. The optional repository/pr_number guard
    further scopes the update. Returns None when no running run with that id (and guard)
    exists, so a duplicate or losing completer is a clean no-op rather than a corruption."""
    status = "generated" if status == "done" else status
    if status not in {"generated", "failed"}:
        raise ReviewMemoryError("status must be generated or failed")
    if isinstance(run_id, bool) or run_id < 1:
        raise ReviewMemoryError("run_id must be a positive integer")
    # Validated here so the >= 0 invariant holds for every database, including a
    # review_runs table created before the column CHECK existed. This function is the
    # authoritative guard; the table-level CHECK is incremental defense-in-depth for
    # freshly created databases only.
    if findings_count is not None and int(findings_count) < 0:
        raise ReviewMemoryError("findings_count must be zero or greater")
    phase = TERMINAL_PHASE_BY_STATUS[status]
    failure_code = clean_text(
        failure_code, field="failure_code", maximum=80, required=False
    )
    if status == "generated":
        failure_code = ""
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
            SET status = ?,
                phase = ?,
                findings_count = ?,
                posted_comment_id = ?,
                completed_at = ?,
                last_heartbeat_at = ?,
                failure_code = ?
            WHERE {" AND ".join(conditions)}
            """,
            (
                status,
                phase,
                int(findings_count) if findings_count is not None else None,
                int(posted_comment_id) if posted_comment_id is not None else None,
                completed,
                completed,
                failure_code,
                *params,
            ),
        )
    if cursor.rowcount == 0:
        return None
    return {
        "id": run_id,
        "status": status,
        "phase": phase,
        "findings_count": findings_count,
        "failure_code": failure_code,
        "completed_at": completed,
    }


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
        f"SELECT * FROM review_runs {where} ORDER BY started_at DESC, id DESC LIMIT ?",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def run_is_stale(
    run: dict[str, Any], *, now: datetime | None = None, stale_after_minutes: int = 30
) -> bool:
    """A run still marked 'running' well past a normal review duration is almost
    certainly crashed or abandoned. This is a read-only interpretation for display
    and metrics; it never mutates the row."""
    if run.get("status") != "running":
        return False
    heartbeat = parse_time(run.get("last_heartbeat_at")) or parse_time(run.get("started_at"))
    if heartbeat is None:
        return False
    moment = now or utc_now()
    return (moment - heartbeat) > timedelta(minutes=max(1, int(stale_after_minutes)))


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
        f"""
        SELECT status, phase, started_at, last_heartbeat_at, completed_at, findings_count
        FROM review_runs {where}
        """,
        params,
    ).fetchall()

    by_status = {"running": 0, "generated": 0, "failed": 0}
    durations: list[float] = []
    findings_total = 0
    completed_with_count = 0
    stalled_running = 0
    for row in rows:
        item = dict(row)
        if item.get("status") in by_status:
            by_status[item["status"]] += 1
        if run_is_stale(item, now=moment):
            stalled_running += 1
        started = parse_time(item.get("started_at"))
        completed = parse_time(item.get("completed_at"))
        if (
            item.get("status") == "generated"
            and started is not None
            and completed is not None
        ):
            durations.append((completed - started).total_seconds())
        if item.get("status") == "generated" and item.get("findings_count") is not None:
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
        "stalled_running": stalled_running,
        "time_to_answer_seconds": {"p50": _pct(50), "p95": _pct(95)},
        "avg_findings_per_completed_run": (
            round(findings_total / completed_with_count, 2)
            if completed_with_count
            else None
        ),
    }
