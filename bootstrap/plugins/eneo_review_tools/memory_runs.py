"""Review run telemetry lifecycle."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

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


def start_run(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    *,
    trigger_comment_id: int | None = None,
    trigger_user: str = "",
    base_sha: str = "",
    head_sha: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record the start of a review run. Operational telemetry only — this is a
    separate table from findings/decisions and never affects suppression."""
    repository = normalize_repository(repository)
    if pr_number < 1:
        raise ReviewMemoryError("pr_number must be positive")
    moment = now or utc_now()
    mark_stale_runs_failed(
        connection,
        repository=repository,
        pr_number=pr_number,
        now=moment,
    )
    started = isoformat(moment)
    with connection:
        cursor = connection.execute(
            """
            INSERT INTO review_runs (
                repository, pr_number, trigger_comment_id, trigger_user, base_sha, head_sha,
                status, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                repository,
                pr_number,
                int(trigger_comment_id) if trigger_comment_id is not None else None,
                clean_text(
                    trigger_user, field="trigger_user", maximum=200, required=False
                ),
                clean_text(base_sha, field="base_sha", maximum=64, required=False),
                clean_text(head_sha, field="head_sha", maximum=64, required=False),
                started,
            ),
        )
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
        "started_at": started,
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

    conditions = ["status = 'running'", "started_at < ?"]
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
                SELECT id, repository, pr_number, status, findings_count,
                       posted_comment_id, started_at, completed_at
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
                SET status = 'failed', completed_at = ?
                WHERE {where}
                """,
                [completed, *params],
            )

    for row in rows:
        row["status"] = "failed"
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


def complete_run(
    connection: sqlite3.Connection,
    run_id: int,
    *,
    repository: str | None = None,
    pr_number: int | None = None,
    status: str = "generated",
    findings_count: int | None = None,
    posted_comment_id: int | None = None,
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
    return {
        "id": run_id,
        "status": status,
        "findings_count": findings_count,
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
    certainly crashed or abandoned — its run_complete was never recorded (these tools
    are best-effort telemetry the model calls). This is a read-only interpretation for
    display and metrics; it never mutates the row."""
    if run.get("status") != "running":
        return False
    started = parse_time(run.get("started_at"))
    if started is None:
        return False
    moment = now or utc_now()
    return (moment - started) > timedelta(minutes=max(1, int(stale_after_minutes)))


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
        f"SELECT status, started_at, completed_at, findings_count FROM review_runs {where}",
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
