"""Objective review-context coverage facts for a review run."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal, TypedDict, cast

try:
    from .memory_validation import (
        ReviewMemoryError,
        isoformat,
        normalize_path,
        normalize_repository,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_validation import (
        ReviewMemoryError,
        isoformat,
        normalize_path,
        normalize_repository,
    )

CoverageState = Literal["complete", "incomplete", "unknown"]


class CoverageSummary(TypedDict):
    state: CoverageState
    changed_paths: int
    diff_exposed: int
    context_reads: int
    unavailable: int
    diff_truncated: int
    coverage_hash: str
    unavailable_paths: list[str]
    truncated_paths: list[str]


def _positive_run_id(value: int) -> int:
    if isinstance(value, bool):
        raise ReviewMemoryError("run_id must be a positive integer")
    run_id = int(value)
    if run_id < 1:
        raise ReviewMemoryError("run_id must be a positive integer")
    return run_id


def _validate_run(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    repository: str,
    pr_number: int,
) -> None:
    row = connection.execute(
        """
        SELECT repository, pr_number
        FROM review_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise ReviewMemoryError("run_id does not match a recorded review run")
    if str(row["repository"]) != repository or int(row["pr_number"]) != int(pr_number):
        raise ReviewMemoryError("run_id does not match this repository and PR")


def _path_domain(path: str) -> str:
    if path.startswith("backend/"):
        return "backend"
    if path.startswith("frontend/"):
        return "frontend"
    if path.startswith(".github/") or path in {"compose.yaml", "Dockerfile"}:
        return "infrastructure"
    return "general"


def _review_mode(path: str, change_status: str) -> str:
    if change_status == "removed":
        return "normal"
    if "alembic" in path or "migration" in path.lower():
        return "migration"
    if path.endswith((".yaml", ".yml", ".toml", ".json")) or path.startswith(".github/"):
        return "configuration"
    if "generated" in path or path.endswith(".d.ts"):
        return "generated-contract"
    return "normal"


def _load_ranges(raw: str) -> list[dict[str, int]]:
    try:
        value: object = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    ranges: list[dict[str, int]] = []
    for item in cast(list[object], value):
        if not isinstance(item, dict):
            continue
        item_map = cast(dict[object, object], item)
        start = item_map.get("start")
        end = item_map.get("end")
        if type(start) is int and type(end) is int and start >= 1 and end >= start:
            ranges.append({"start": start, "end": end})
    return ranges


def _dump_ranges(ranges: Sequence[Mapping[str, int]]) -> str:
    return json.dumps(list(ranges), separators=(",", ":"), sort_keys=True)


def register_changed_files(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    repository: str,
    pr_number: int,
    files: Sequence[Mapping[str, object]],
    now: datetime | None = None,
) -> None:
    run_id = _positive_run_id(run_id)
    repository = normalize_repository(repository)
    pr_number = int(pr_number)
    if pr_number < 1:
        raise ReviewMemoryError("pr_number must be positive")
    _validate_run(connection, run_id=run_id, repository=repository, pr_number=pr_number)
    moment = isoformat(now)
    with connection:
        for item in files:
            path = normalize_path(str(item.get("path", "")))
            change_status = str(item.get("status", ""))[:40]
            connection.execute(
                """
                INSERT INTO review_run_files (
                    run_id, repository, pr_number, path, change_status, domain,
                    review_mode, first_accessed_at, last_accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, path) DO UPDATE SET
                    change_status = excluded.change_status,
                    domain = excluded.domain,
                    review_mode = excluded.review_mode,
                    last_accessed_at = excluded.last_accessed_at
                """,
                (
                    run_id,
                    repository,
                    pr_number,
                    path,
                    change_status,
                    _path_domain(path),
                    _review_mode(path, change_status),
                    moment,
                    moment,
                ),
            )


def record_diff_exposure(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    repository: str,
    pr_number: int,
    paths: Sequence[str],
    truncated: bool,
    unavailable_reason: str = "",
    now: datetime | None = None,
) -> None:
    run_id = _positive_run_id(run_id)
    repository = normalize_repository(repository)
    pr_number = int(pr_number)
    _validate_run(connection, run_id=run_id, repository=repository, pr_number=pr_number)
    moment = isoformat(now)
    reason = str(unavailable_reason or "")[:80]
    with connection:
        for raw_path in paths:
            path = normalize_path(raw_path)
            connection.execute(
                """
                INSERT INTO review_run_files (
                    run_id, repository, pr_number, path, change_status, domain,
                    review_mode, diff_requested, diff_returned, diff_truncated,
                    unavailable_reason, first_accessed_at, last_accessed_at
                ) VALUES (?, ?, ?, ?, '', ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, path) DO UPDATE SET
                    diff_requested = 1,
                    diff_returned = CASE
                        WHEN excluded.diff_returned = 1 THEN 1
                        ELSE review_run_files.diff_returned
                    END,
                    diff_truncated = CASE
                        WHEN excluded.diff_truncated = 1 THEN 1
                        ELSE review_run_files.diff_truncated
                    END,
                    unavailable_reason = CASE
                        WHEN excluded.unavailable_reason != ''
                        THEN excluded.unavailable_reason
                        ELSE review_run_files.unavailable_reason
                    END,
                    last_accessed_at = excluded.last_accessed_at
                """,
                (
                    run_id,
                    repository,
                    pr_number,
                    path,
                    _path_domain(path),
                    _review_mode(path, ""),
                    0 if reason else 1,
                    1 if truncated else 0,
                    reason,
                    moment,
                    moment,
                ),
            )


def record_file_range(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    repository: str,
    pr_number: int,
    path: str,
    side: Literal["head", "base"],
    start_line: int,
    end_line: int,
    now: datetime | None = None,
) -> None:
    run_id = _positive_run_id(run_id)
    repository = normalize_repository(repository)
    pr_number = int(pr_number)
    path = normalize_path(path)
    if side not in {"head", "base"}:
        raise ReviewMemoryError("side must be head or base")
    if start_line < 1 or end_line < start_line:
        raise ReviewMemoryError("line range must be positive")
    _validate_run(connection, run_id=run_id, repository=repository, pr_number=pr_number)
    moment = isoformat(now)
    column = "head_ranges_read_json" if side == "head" else "base_ranges_read_json"
    row = connection.execute(
        f"""
        SELECT {column}
        FROM review_run_files
        WHERE run_id = ? AND path = ?
        """,
        (run_id, path),
    ).fetchone()
    ranges = _load_ranges(str(row[column])) if row else []
    item = {"start": int(start_line), "end": int(end_line)}
    if item not in ranges:
        ranges.append(item)
    ranges.sort(key=lambda value: (value["start"], value["end"]))
    with connection:
        connection.execute(
            f"""
            INSERT INTO review_run_files (
                run_id, repository, pr_number, path, change_status, domain,
                review_mode, {column}, first_accessed_at, last_accessed_at
            ) VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, path) DO UPDATE SET
                {column} = excluded.{column},
                last_accessed_at = excluded.last_accessed_at
            """,
            (
                run_id,
                repository,
                pr_number,
                path,
                _path_domain(path),
                _review_mode(path, ""),
                _dump_ranges(ranges),
                moment,
                moment,
            ),
        )


def coverage_summary(
    connection: sqlite3.Connection,
    *,
    run_id: int | None,
) -> CoverageSummary | None:
    if run_id is None:
        return None
    run_id = _positive_run_id(run_id)
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT path, change_status, domain, review_mode, diff_requested,
                   diff_returned, diff_truncated, head_ranges_read_json,
                   base_ranges_read_json, unavailable_reason
            FROM review_run_files
            WHERE run_id = ?
            ORDER BY path
            """,
            (run_id,),
        )
    ]
    if not rows:
        return {
            "state": "unknown",
            "changed_paths": 0,
            "diff_exposed": 0,
            "context_reads": 0,
            "unavailable": 0,
            "diff_truncated": 0,
            "coverage_hash": "sha256:" + hashlib.sha256(b"[]").hexdigest(),
            "unavailable_paths": [],
            "truncated_paths": [],
        }

    diff_exposed = sum(1 for row in rows if int(row["diff_returned"]) == 1)
    context_reads = sum(
        1
        for row in rows
        if _load_ranges(str(row["head_ranges_read_json"]))
        or _load_ranges(str(row["base_ranges_read_json"]))
    )
    unavailable_paths = [
        str(row["path"]) for row in rows if str(row["unavailable_reason"] or "")
    ]
    truncated_paths = [
        str(row["path"]) for row in rows if int(row["diff_truncated"]) == 1
    ]
    state: CoverageState = (
        "complete"
        if diff_exposed == len(rows) and not unavailable_paths and not truncated_paths
        else "incomplete"
    )
    hash_payload = [
        {
            "path": str(row["path"]),
            "change_status": str(row["change_status"]),
            "domain": str(row["domain"]),
            "review_mode": str(row["review_mode"]),
            "diff_returned": int(row["diff_returned"]),
            "diff_truncated": int(row["diff_truncated"]),
            "head_ranges_read": _load_ranges(str(row["head_ranges_read_json"])),
            "base_ranges_read": _load_ranges(str(row["base_ranges_read_json"])),
            "unavailable_reason": str(row["unavailable_reason"] or ""),
        }
        for row in rows
    ]
    material = json.dumps(hash_payload, separators=(",", ":"), sort_keys=True)
    return {
        "state": state,
        "changed_paths": len(rows),
        "diff_exposed": diff_exposed,
        "context_reads": context_reads,
        "unavailable": len(unavailable_paths),
        "diff_truncated": len(truncated_paths),
        "coverage_hash": "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
        "unavailable_paths": unavailable_paths[:5],
        "truncated_paths": truncated_paths[:5],
    }
