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
DiffState = Literal["unseen", "complete", "truncated", "unavailable"]


class CoverageSummary(TypedDict):
    state: CoverageState
    changed_paths: int
    diff_exposed: int
    context_reads: int
    changed_paths_with_diff: int
    changed_paths_with_source_reads: int
    supporting_context_paths_read: int
    changed_files_reported: int | None
    changed_files_registered: int
    changed_file_registration_complete: bool
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
        SELECT repository, pr_number, status
        FROM review_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        raise ReviewMemoryError("run_id does not match a recorded review run")
    if str(row["repository"]) != repository or int(row["pr_number"]) != int(pr_number):
        raise ReviewMemoryError("run_id does not match this repository and PR")
    if str(row["status"]) != "running":
        raise ReviewMemoryError("run_id is not an active review run")


def _run_registration(
    connection: sqlite3.Connection, run_id: int
) -> dict[str, int | bool | None]:
    row = connection.execute(
        """
        SELECT changed_files_reported, changed_files_registered,
               changed_file_registration_complete
        FROM review_runs
        WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return {
            "changed_files_reported": None,
            "changed_files_registered": 0,
            "changed_file_registration_complete": False,
        }
    return {
        "changed_files_reported": (
            int(row["changed_files_reported"])
            if row["changed_files_reported"] is not None
            else None
        ),
        "changed_files_registered": int(row["changed_files_registered"] or 0),
        "changed_file_registration_complete": bool(
            row["changed_file_registration_complete"]
        ),
    }


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
    changed_files_reported: int | None = None,
    registration_complete: bool | None = None,
    now: datetime | None = None,
) -> None:
    run_id = _positive_run_id(run_id)
    repository = normalize_repository(repository)
    pr_number = int(pr_number)
    if pr_number < 1:
        raise ReviewMemoryError("pr_number must be positive")
    _validate_run(connection, run_id=run_id, repository=repository, pr_number=pr_number)
    if changed_files_reported is None:
        reported = len(files)
    elif isinstance(changed_files_reported, bool) or int(changed_files_reported) < 0:
        raise ReviewMemoryError("changed_files_reported must be non-negative")
    else:
        reported = int(changed_files_reported)
    complete = (
        len(files) >= reported
        if registration_complete is None
        else bool(registration_complete)
    )
    moment = isoformat(now)
    with connection:
        for item in files:
            path = normalize_path(str(item.get("path", "")))
            change_status = str(item.get("status", ""))[:40]
            connection.execute(
                """
                INSERT INTO review_run_files (
                    run_id, repository, pr_number, path, change_status, domain,
                    is_changed_path, review_mode, first_accessed_at, last_accessed_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(run_id, path) DO UPDATE SET
                    change_status = excluded.change_status,
                    is_changed_path = 1,
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
        registered = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM review_run_files
            WHERE run_id = ? AND is_changed_path = 1
            """,
            (run_id,),
        ).fetchone()
        registered_count = int(registered["count"] if registered else 0)
        connection.execute(
            """
            UPDATE review_runs
            SET changed_files_reported = ?,
                changed_files_registered = ?,
                changed_file_registration_complete = ?
            WHERE id = ?
            """,
            (
                reported,
                registered_count,
                1 if complete and registered_count == reported else 0,
                run_id,
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
    diff_state: DiffState
    if reason:
        diff_state = "unavailable"
    elif truncated:
        diff_state = "truncated"
    else:
        diff_state = "complete"
    with connection:
        for raw_path in paths:
            path = normalize_path(raw_path)
            connection.execute(
                """
                INSERT INTO review_run_files (
                    run_id, repository, pr_number, path, change_status, domain,
                    review_mode, diff_requested, diff_returned, diff_truncated,
                    diff_state, unavailable_reason, first_accessed_at, last_accessed_at
                ) VALUES (?, ?, ?, ?, '', ?, ?, 1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, path) DO UPDATE SET
                    diff_requested = 1,
                    diff_returned = excluded.diff_returned,
                    diff_truncated = excluded.diff_truncated,
                    diff_state = excluded.diff_state,
                    unavailable_reason = excluded.unavailable_reason,
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
                    diff_state,
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
            SELECT path, change_status, is_changed_path, domain, review_mode,
                   diff_requested, diff_returned, diff_truncated, diff_state,
                   head_ranges_read_json, base_ranges_read_json,
                   unavailable_reason
            FROM review_run_files
            WHERE run_id = ?
            ORDER BY path
            """,
            (run_id,),
        )
    ]
    if not rows:
        registration = _run_registration(connection, run_id)
        return {
            "state": "unknown",
            "changed_paths": 0,
            "diff_exposed": 0,
            "context_reads": 0,
            "changed_paths_with_diff": 0,
            "changed_paths_with_source_reads": 0,
            "supporting_context_paths_read": 0,
            "changed_files_reported": cast(
                int | None, registration["changed_files_reported"]
            ),
            "changed_files_registered": int(
                registration["changed_files_registered"] or 0
            ),
            "changed_file_registration_complete": bool(
                registration["changed_file_registration_complete"]
            ),
            "unavailable": 0,
            "diff_truncated": 0,
            "coverage_hash": "sha256:" + hashlib.sha256(b"[]").hexdigest(),
            "unavailable_paths": [],
            "truncated_paths": [],
        }

    registration = _run_registration(connection, run_id)
    changed_rows = [row for row in rows if int(row.get("is_changed_path", 0)) == 1]
    supporting_rows = [row for row in rows if int(row.get("is_changed_path", 0)) == 0]
    changed_paths_with_diff = sum(
        1 for row in changed_rows if str(row.get("diff_state") or "") == "complete"
    )
    context_reads = sum(
        1
        for row in rows
        if _load_ranges(str(row["head_ranges_read_json"]))
        or _load_ranges(str(row["base_ranges_read_json"]))
    )
    changed_paths_with_source_reads = sum(
        1
        for row in changed_rows
        if _load_ranges(str(row["head_ranges_read_json"]))
        or _load_ranges(str(row["base_ranges_read_json"]))
    )
    supporting_context_paths_read = sum(
        1
        for row in supporting_rows
        if _load_ranges(str(row["head_ranges_read_json"]))
        or _load_ranges(str(row["base_ranges_read_json"]))
    )
    unavailable_paths = [
        str(row["path"])
        for row in changed_rows
        if str(row.get("diff_state") or "") == "unavailable"
    ]
    truncated_paths = [
        str(row["path"])
        for row in changed_rows
        if str(row.get("diff_state") or "") == "truncated"
    ]
    registered = int(registration["changed_files_registered"] or 0)
    reported = cast(int | None, registration["changed_files_reported"])
    registration_complete = bool(registration["changed_file_registration_complete"])
    expected_changed_paths = registered
    state: CoverageState = (
        "complete"
        if (
            expected_changed_paths > 0
            and registration_complete
            and changed_paths_with_diff == expected_changed_paths
            and not unavailable_paths
            and not truncated_paths
        )
        else "incomplete"
    )
    hash_payload = [
        {
            "path": str(row["path"]),
            "change_status": str(row["change_status"]),
            "domain": str(row["domain"]),
            "review_mode": str(row["review_mode"]),
            "is_changed_path": int(row.get("is_changed_path", 0)),
            "diff_state": str(row.get("diff_state") or "unseen"),
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
        "changed_paths": expected_changed_paths,
        "diff_exposed": changed_paths_with_diff,
        "context_reads": context_reads,
        "changed_paths_with_diff": changed_paths_with_diff,
        "changed_paths_with_source_reads": changed_paths_with_source_reads,
        "supporting_context_paths_read": supporting_context_paths_read,
        "changed_files_reported": reported,
        "changed_files_registered": registered,
        "changed_file_registration_complete": registration_complete,
        "unavailable": len(unavailable_paths),
        "diff_truncated": len(truncated_paths),
        "coverage_hash": "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest(),
        "unavailable_paths": unavailable_paths[:5],
        "truncated_paths": truncated_paths[:5],
    }
