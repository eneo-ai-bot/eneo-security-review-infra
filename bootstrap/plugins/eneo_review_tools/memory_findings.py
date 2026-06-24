"""Finding identity rows, observations, PR-local references, and review context."""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from collections.abc import Mapping, Sequence
from typing import Any, Iterable, cast

try:
    from .memory_decisions import (
        active_suppression_from_decision,
        latest_decisions_for_fingerprints,
        active_suppression,
    )
    from .memory_identity import compute_fingerprint
    from .memory_validation import (
        HASH_RE,
        CATEGORIES,
        MAX_FINDINGS_PER_REVIEW,
        MIN_CONFIDENCE,
        SEVERITIES,
        SEVERITY_SCORE_GATES,
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        compact_text,
        current_policy_revision,
        isoformat,
        local_reference_number,
        normalize_context_hash,
        normalize_path,
        normalize_repository,
        normalize_rule_id,
        parse_time,
        utc_now,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_decisions import (
        active_suppression_from_decision,
        latest_decisions_for_fingerprints,
        active_suppression,
    )
    from memory_identity import compute_fingerprint
    from memory_validation import (
        HASH_RE,
        CATEGORIES,
        MAX_FINDINGS_PER_REVIEW,
        MIN_CONFIDENCE,
        SEVERITIES,
        SEVERITY_SCORE_GATES,
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        compact_text,
        current_policy_revision,
        isoformat,
        local_reference_number,
        normalize_context_hash,
        normalize_path,
        normalize_repository,
        normalize_rule_id,
        parse_time,
        utc_now,
    )


def _review_subject_id(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    head_sha: str,
    *,
    policy_revision: str | None = None,
    now: datetime | None = None,
) -> int:
    repository = normalize_repository(repository)
    if int(pr_number) < 1:
        raise ReviewMemoryError("pr_number must be positive")
    head_sha = clean_text(head_sha, field="head_sha", maximum=64).lower()
    if not HASH_RE.fullmatch(head_sha):
        raise ReviewMemoryError(
            "head_sha must be an exact 40 to 64 character hexadecimal commit SHA"
        )
    policy = current_policy_revision(policy_revision)
    connection.execute(
        """
        INSERT OR IGNORE INTO review_subjects (
            repository, pr_number, head_sha, policy_revision, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (repository, int(pr_number), head_sha, policy, isoformat(now)),
    )
    row = connection.execute(
        """
        SELECT id FROM review_subjects
        WHERE repository = ? AND pr_number = ? AND head_sha = ? AND policy_revision = ?
        """,
        (repository, int(pr_number), head_sha, policy),
    ).fetchone()
    if not row:
        raise ReviewMemoryError("failed to create review subject")
    return int(row["id"])


def _latest_publication_current_fingerprints(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
) -> set[str] | None:
    publication = connection.execute(
        """
        SELECT id FROM review_publications
        WHERE repository = ? AND pr_number = ? AND superseded_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (repository, pr_number),
    ).fetchone()
    if not publication:
        return None
    rows = connection.execute(
        """
        SELECT fingerprint FROM publication_findings
        WHERE publication_id = ? AND status = 'current'
        """,
        (int(publication["id"]),),
    ).fetchall()
    return {str(row["fingerprint"]) for row in rows}


def assign_local_reference(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    fingerprint: str,
    *,
    now: datetime | None = None,
) -> str:
    repository = normalize_repository(repository)
    pr_number = int(pr_number)
    row = connection.execute(
        """
        SELECT local_reference FROM pr_finding_references
        WHERE repository = ? AND pr_number = ? AND fingerprint = ?
        """,
        (repository, pr_number, fingerprint),
    ).fetchone()
    if row:
        return str(row["local_reference"])

    rows = connection.execute(
        """
        SELECT local_reference FROM pr_finding_references
        WHERE repository = ? AND pr_number = ?
        """,
        (repository, pr_number),
    ).fetchall()
    next_number = (
        max((local_reference_number(row["local_reference"]) for row in rows), default=0)
        + 1
    )
    local_reference = f"F{next_number}"
    connection.execute(
        """
        INSERT INTO pr_finding_references (
            repository, pr_number, fingerprint, local_reference, first_assigned_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (repository, pr_number, fingerprint, local_reference, isoformat(now)),
    )
    return local_reference


def local_reference_for_fingerprint(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    fingerprint: str,
) -> str | None:
    row = connection.execute(
        """
        SELECT local_reference FROM pr_finding_references
        WHERE repository = ? AND pr_number = ? AND fingerprint = ?
        """,
        (normalize_repository(repository), int(pr_number), fingerprint),
    ).fetchone()
    return str(row["local_reference"]) if row else None


def fingerprint_for_local_reference(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    local_reference: str,
) -> str:
    ref = str(local_reference or "").strip().upper()
    if not re.fullmatch(r"F[1-9][0-9]*", ref):
        raise ReviewMemoryError("local_reference must look like F1, F2, ...")
    row = connection.execute(
        """
        SELECT fingerprint FROM pr_finding_references
        WHERE repository = ? AND pr_number = ? AND local_reference = ?
        """,
        (normalize_repository(repository), int(pr_number), ref),
    ).fetchone()
    if not row:
        raise ReviewMemoryError("unknown local finding reference for this pull request")
    return str(row["fingerprint"])


def _validated_finding(repository: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    rule_id = normalize_rule_id(str(raw.get("rule_id", "")))
    path = normalize_path(str(raw.get("path", "")))
    symbol = clean_text(raw.get("symbol"), field="symbol", maximum=200, required=False)
    anchor = clean_text(raw.get("anchor"), field="anchor", maximum=240)
    title = clean_text(raw.get("title"), field="title", maximum=180)
    severity = str(raw.get("severity", "")).strip().title()
    if severity not in SEVERITIES:
        raise ReviewMemoryError(
            f"severity must be one of: {', '.join(sorted(SEVERITIES))}"
        )

    category = str(raw.get("category", "")).strip().lower()
    if category not in CATEGORIES:
        raise ReviewMemoryError(
            f"category must be one of: {', '.join(sorted(CATEGORIES))}"
        )

    score_raw = raw.get("publication_score")
    if score_raw is None:
        raise ReviewMemoryError("publication_score must be an integer")
    try:
        publication_score = int(score_raw)
    except (TypeError, ValueError) as exc:
        raise ReviewMemoryError("publication_score must be an integer") from exc
    minimum_score = SEVERITY_SCORE_GATES[severity]
    if publication_score < minimum_score or publication_score > 10:
        raise ReviewMemoryError(
            f"publication_score for {severity} must be between {minimum_score} and 10"
        )

    confidence_raw = raw.get("confidence")
    if confidence_raw is None:
        raise ReviewMemoryError("confidence must be a number")
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError) as exc:
        raise ReviewMemoryError("confidence must be a number") from exc
    if confidence < MIN_CONFIDENCE or confidence > 1.0:
        raise ReviewMemoryError("confidence must be between 0.85 and 1.00")

    introduced = raw.get("introduced_by_diff")
    if introduced is not True:
        raise ReviewMemoryError("introduced_by_diff must be true")

    line_raw = raw.get("line")
    if line_raw is None:
        raise ReviewMemoryError("line is required and must be an integer")
    try:
        line = int(line_raw)
    except (TypeError, ValueError) as exc:
        raise ReviewMemoryError("line is required and must be an integer") from exc
    if line < 1:
        raise ReviewMemoryError("line must be positive")

    evidence = clean_multiline(raw.get("evidence"), field="evidence", maximum=4000)
    disproof_checks = clean_multiline(
        raw.get("disproof_checks"), field="disproof_checks", maximum=2500
    )
    impact = clean_multiline(raw.get("impact"), field="impact", maximum=2000)
    smallest_fix = clean_multiline(
        raw.get("smallest_fix"), field="smallest_fix", maximum=2500
    )

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
    findings: object,
    *,
    context_hashes: dict[str, str] | None = None,
    policy_revision: str | None = None,
) -> list[dict[str, Any]]:
    repository = normalize_repository(repository)
    if pr_number < 1:
        raise ReviewMemoryError("pr_number must be positive")
    head_sha = clean_text(head_sha, field="head_sha", maximum=64).lower()
    if not HASH_RE.fullmatch(head_sha):
        raise ReviewMemoryError(
            "head_sha must be an exact 40 to 64 character hexadecimal commit SHA"
        )
    if not isinstance(findings, Sequence) or isinstance(findings, (str, bytes)):
        raise ReviewMemoryError("findings must be an array")
    finding_items: list[Mapping[str, Any]] = []
    for raw in cast(Sequence[object], findings):
        if not isinstance(raw, Mapping):
            raise ReviewMemoryError("each finding must be an object")
        finding_items.append(cast(Mapping[str, Any], raw))
    if len(finding_items) > MAX_FINDINGS_PER_REVIEW:
        raise ReviewMemoryError(
            f"findings exceeds operational safety limit of {MAX_FINDINGS_PER_REVIEW}"
        )
    normalized_hashes = {
        normalize_path(path): normalize_context_hash(value)
        for path, value in (context_hashes or {}).items()
    }
    policy = current_policy_revision(policy_revision)
    validated = [_validated_finding(repository, raw) for raw in finding_items]
    now = isoformat()
    results: list[dict[str, Any]] = []
    with connection:
        subject_id = _review_subject_id(
            connection,
            repository,
            pr_number,
            head_sha,
            policy_revision=policy,
            now=parse_time(now),
        )
        for item in validated:
            context_hash = normalized_hashes.get(item["path"], "")
            if not context_hash:
                raise ReviewMemoryError(
                    f"missing trusted context hash for {item['path']}"
                )
            existing = connection.execute(
                "SELECT occurrences FROM findings WHERE fingerprint = ?",
                (item["fingerprint"],),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO findings (
                        fingerprint, repository, rule_id, path, line, symbol, anchor,
                        title, severity, category, publication_score, confidence,
                        context_hash, pr_number, head_sha, evidence, disproof_checks,
                        impact, smallest_fix, introduced_by_diff, first_seen_at,
                        last_seen_at, occurrences
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        item["fingerprint"],
                        item["repository"],
                        item["rule_id"],
                        item["path"],
                        item["line"],
                        item["symbol"],
                        item["anchor"],
                        item["title"],
                        item["severity"],
                        item["category"],
                        item["publication_score"],
                        item["confidence"],
                        context_hash,
                        pr_number,
                        head_sha,
                        item["evidence"],
                        item["disproof_checks"],
                        item["impact"],
                        item["smallest_fix"],
                        item["introduced_by_diff"],
                        now,
                        now,
                    ),
                )
            cursor = connection.execute(
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
                    pr_number,
                    head_sha,
                    policy,
                    item["fingerprint"],
                    item["rule_id"],
                    item["path"],
                    item["line"],
                    item["symbol"],
                    item["anchor"],
                    item["title"],
                    item["severity"],
                    item["category"],
                    item["publication_score"],
                    item["confidence"],
                    context_hash,
                    item["evidence"],
                    item["disproof_checks"],
                    item["impact"],
                    item["smallest_fix"],
                    item["introduced_by_diff"],
                    now,
                ),
            )
            inserted_observation = cursor.rowcount == 1
            if not inserted_observation:
                connection.execute(
                    """
                    UPDATE finding_observations
                    SET line = ?, title = ?, severity = ?, category = ?,
                        publication_score = ?, confidence = ?, context_hash = ?,
                        evidence = ?, disproof_checks = ?, impact = ?, smallest_fix = ?,
                        introduced_by_diff = ?, observed_at = ?
                    WHERE review_subject_id = ? AND fingerprint = ?
                    """,
                    (
                        item["line"],
                        item["title"],
                        item["severity"],
                        item["category"],
                        item["publication_score"],
                        item["confidence"],
                        context_hash,
                        item["evidence"],
                        item["disproof_checks"],
                        item["impact"],
                        item["smallest_fix"],
                        item["introduced_by_diff"],
                        now,
                        subject_id,
                        item["fingerprint"],
                    ),
                )

            connection.execute(
                """
                UPDATE findings
                SET line = ?, title = ?, severity = ?, category = ?,
                    publication_score = ?, confidence = ?, context_hash = ?,
                    pr_number = ?, head_sha = ?, evidence = ?, disproof_checks = ?,
                    impact = ?, smallest_fix = ?, introduced_by_diff = ?,
                    last_seen_at = ?,
                    occurrences = occurrences + ?
                WHERE fingerprint = ?
                """,
                (
                    item["line"],
                    item["title"],
                    item["severity"],
                    item["category"],
                    item["publication_score"],
                    item["confidence"],
                    context_hash,
                    pr_number,
                    head_sha,
                    item["evidence"],
                    item["disproof_checks"],
                    item["impact"],
                    item["smallest_fix"],
                    item["introduced_by_diff"],
                    now,
                    1 if inserted_observation else 0,
                    item["fingerprint"],
                ),
            )
            local_reference = assign_local_reference(
                connection,
                repository,
                pr_number,
                item["fingerprint"],
                now=parse_time(now),
            )
            observation = connection.execute(
                """
                SELECT id FROM finding_observations
                WHERE review_subject_id = ? AND fingerprint = ?
                """,
                (subject_id, item["fingerprint"]),
            ).fetchone()
            suppression = active_suppression(
                connection, item["fingerprint"], context_hash=context_hash
            )
            results.append(
                {
                    "fingerprint": item["fingerprint"],
                    "fingerprint_short": item["fingerprint"][:12],
                    "local_reference": local_reference,
                    "review_subject_id": subject_id,
                    "observation_id": int(observation["id"]) if observation else None,
                    "policy_revision": policy,
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
    pr_number: int | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    repository = normalize_repository(repository)
    clean_paths = sorted(
        {normalize_path(path) for path in (paths or []) if str(path).strip()}
    )
    if pr_number is not None:
        pr_number = int(pr_number)
        if pr_number < 1:
            raise ReviewMemoryError("pr_number must be positive")
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
        SELECT fingerprint, rule_id, path, line, symbol, anchor, title, severity, category,
               publication_score, confidence, context_hash, pr_number, head_sha,
               policy_revision, observed_at AS last_seen_at, evidence, disproof_checks,
               impact, smallest_fix
        FROM finding_observations
        WHERE {where}
        ORDER BY observed_at DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    items = [dict(row) for row in rows]
    latest_decisions = latest_decisions_for_fingerprints(
        connection, (item["fingerprint"] for item in items)
    )
    moment = utc_now()

    def enrich(
        item: dict[str, Any], decisions: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        decision = decisions.get(item["fingerprint"])
        matches_last_seen = active_suppression_from_decision(
            decision, context_hash=item["context_hash"], now=moment
        )
        return {
            **item,
            "suppressed_for_last_seen_file_version": matches_last_seen is not None,
            "latest_decision": decision,
        }

    historical_suppressions: list[dict[str, Any]] = []
    recent: list[dict[str, Any]] = []
    for item in items:
        decision = latest_decisions.get(item["fingerprint"])
        matches_last_seen = active_suppression_from_decision(
            decision, context_hash=item["context_hash"], now=moment
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
                    "warning": (
                        "Historical hint only; the final record tool checks the "
                        "current file hash."
                    ),
                }
            )
        recent.append(enrich(item, latest_decisions))

    repeat_review_findings: list[dict[str, Any]] = []
    if pr_number is not None:
        active_publication_fingerprints = _latest_publication_current_fingerprints(
            connection,
            repository,
            pr_number,
        )
        repeat_items: list[dict[str, Any]] = []
        if active_publication_fingerprints is None or active_publication_fingerprints:
            repeat_params: list[Any] = [repository, pr_number]
            repeat_where = "repository = ? AND pr_number = ?"
            if active_publication_fingerprints is not None:
                fingerprints = sorted(active_publication_fingerprints)
                placeholders = ",".join("?" for _ in fingerprints)
                repeat_where += f" AND fingerprint IN ({placeholders})"
                repeat_params.extend(fingerprints)
            if clean_paths:
                placeholders = ",".join("?" for _ in clean_paths)
                repeat_where += f" AND path IN ({placeholders})"
                repeat_params.extend(clean_paths)
            repeat_rows = connection.execute(
                f"""
                SELECT fo.fingerprint, fo.rule_id, fo.path, fo.line, fo.symbol, fo.anchor,
                       fo.title, fo.severity, fo.category, fo.publication_score, fo.confidence,
                       fo.context_hash, fo.pr_number, fo.head_sha, fo.policy_revision,
                       fo.observed_at AS last_seen_at, fo.evidence, fo.disproof_checks,
                       fo.impact, fo.smallest_fix, refs.local_reference
                FROM finding_observations fo
                JOIN (
                    SELECT fingerprint, MAX(id) AS id
                    FROM finding_observations
                    WHERE {repeat_where}
                    GROUP BY fingerprint
                    ORDER BY MAX(id) DESC
                    LIMIT ?
                ) latest ON latest.id = fo.id
                LEFT JOIN pr_finding_references refs
                  ON refs.repository = fo.repository
                 AND refs.pr_number = fo.pr_number
                 AND refs.fingerprint = fo.fingerprint
                ORDER BY fo.observed_at DESC, fo.id DESC
                """,
                [*repeat_params, MAX_FINDINGS_PER_REVIEW],
            ).fetchall()
            repeat_items = [dict(row) for row in repeat_rows]
        repeat_decisions = latest_decisions_for_fingerprints(
            connection, (item["fingerprint"] for item in repeat_items)
        )
        for item in repeat_items:
            repeat_item = enrich(item, repeat_decisions)
            if not repeat_item["suppressed_for_last_seen_file_version"]:
                repeat_item["previous_head"] = str(item["head_sha"])
                repeat_item["prior_claim"] = compact_text(
                    item.get("evidence"), maximum=600
                )
                repeat_item["prior_disproof_checks"] = compact_text(
                    item.get("disproof_checks"), maximum=420
                )
                repeat_item["prior_impact"] = compact_text(
                    item.get("impact"), maximum=420
                )
                repeat_item["prior_smallest_fix"] = compact_text(
                    item.get("smallest_fix"), maximum=520
                )
                for old_field in (
                    "evidence",
                    "disproof_checks",
                    "impact",
                    "smallest_fix",
                ):
                    repeat_item.pop(old_field, None)
                repeat_review_findings.append(repeat_item)

    return {
        "repository": repository,
        "paths": clean_paths,
        "policy": (
            "Human decisions are historical hints during analysis. The record tool suppresses only "
            "when the current trusted file hash matches the hash reviewed by "
            "the human. A file change "
            "forces re-validation."
        ),
        "historical_suppressions": historical_suppressions,
        "recent_findings": recent,
        "repeat_review_findings": repeat_review_findings,
    }
