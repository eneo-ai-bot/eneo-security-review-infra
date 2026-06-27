"""Read-only review-memory reporting and export helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

try:
    from .memory_decisions import (
        active_suppression_from_decision,
        latest_decisions_for_fingerprints,
        active_suppression,
        latest_decision,
    )
    from .memory_schema import SCHEMA_VERSION
    from .memory_validation import (
        CATEGORIES,
        DECISIONS,
        SEVERITIES,
        isoformat,
        normalize_repository,
        parse_time,
        utc_now,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_decisions import (
        active_suppression_from_decision,
        latest_decisions_for_fingerprints,
        active_suppression,
        latest_decision,
    )
    from memory_schema import SCHEMA_VERSION
    from memory_validation import (
        CATEGORIES,
        DECISIONS,
        SEVERITIES,
        isoformat,
        normalize_repository,
        parse_time,
        utc_now,
    )


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
    findings = [
        dict(row)
        for row in connection.execute("SELECT * FROM findings ORDER BY first_seen_at")
    ]
    subjects = [
        dict(row)
        for row in connection.execute("SELECT * FROM review_subjects ORDER BY id")
    ]
    observations = [
        dict(row)
        for row in connection.execute("SELECT * FROM finding_observations ORDER BY id")
    ]
    decisions = [
        dict(row) for row in connection.execute("SELECT * FROM decisions ORDER BY id")
    ]
    references = [
        dict(row)
        for row in connection.execute("SELECT * FROM pr_finding_references ORDER BY id")
    ]
    publications = [
        dict(row)
        for row in connection.execute("SELECT * FROM review_publications ORDER BY id")
    ]
    run_files = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM review_run_files ORDER BY run_id, path"
        )
    ]
    publication_findings = [
        dict(row)
        for row in connection.execute("SELECT * FROM publication_findings ORDER BY id")
    ]
    verification_runs = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM review_verification_runs ORDER BY id"
        )
    ]
    candidate_verifications = [
        dict(row)
        for row in connection.execute("SELECT * FROM candidate_verifications ORDER BY id")
    ]
    candidate_reconciliations = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM candidate_reconciliations ORDER BY id"
        )
    ]
    quality_feedback = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM review_quality_feedback ORDER BY id"
        )
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": isoformat(),
        "findings": findings,
        "review_subjects": subjects,
        "finding_observations": observations,
        "decisions": decisions,
        "pr_finding_references": references,
        "review_publications": publications,
        "review_run_files": run_files,
        "publication_findings": publication_findings,
        "review_verification_runs": verification_runs,
        "candidate_verifications": candidate_verifications,
        "candidate_reconciliations": candidate_reconciliations,
        "review_quality_feedback": quality_feedback,
    }


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
    items = [dict(row) for row in rows]
    latest_decisions = latest_decisions_for_fingerprints(
        connection, (item["fingerprint"] for item in items)
    )

    by_severity = {severity: 0 for severity in sorted(SEVERITIES)}
    by_category = {category: 0 for category in sorted(CATEGORIES)}
    latest_decision_by_type = {decision: 0 for decision in sorted(DECISIONS)}
    findings_without_decision = 0
    active_suppressions = 0
    nearing_expiry = 0
    repeats_after_decision = 0

    for item in items:
        fingerprint = item["fingerprint"]
        if item.get("severity") in by_severity:
            by_severity[item["severity"]] += 1
        if item.get("category") in by_category:
            by_category[item["category"]] += 1

        decision = latest_decisions.get(fingerprint)
        if decision is None:
            findings_without_decision += 1
        elif decision["decision"] in latest_decision_by_type:
            latest_decision_by_type[decision["decision"]] += 1

        suppression = active_suppression_from_decision(
            decision, context_hash=item.get("context_hash"), now=moment
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
            if (
                last_seen is not None
                and decided_at is not None
                and decided_at <= last_seen
            ):
                repeats_after_decision += 1

    return {
        "repository": repo,
        "generated_at": isoformat(moment),
        "findings_total": len(items),
        "findings_without_decision": findings_without_decision,
        "findings_by_severity": by_severity,
        "findings_by_category": by_category,
        "latest_decision_by_type": latest_decision_by_type,
        "active_suppressions": active_suppressions,
        "active_suppressions_expiring_within_days": expiring_within_days,
        "active_suppressions_nearing_expiry": nearing_expiry,
        "repeats_after_decision_approx": repeats_after_decision,
    }
