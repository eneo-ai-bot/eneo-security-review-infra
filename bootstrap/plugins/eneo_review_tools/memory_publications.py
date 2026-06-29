"""Review publication lifecycle and final comment assembly."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal, TypedDict, cast

try:
    from .memory_decisions import active_suppression
    from .memory_coverage import coverage_summary
    from .memory_findings import assign_local_reference
    from .memory_verification import (
        candidate_reconciliations_for_run,
        latest_verification_status_by_run,
    )
    from .memory_validation import (
        HASH_RE,
        MAX_FINDINGS_PER_REVIEW,
        PRIOR_FINDING_VERDICTS,
        PriorFindingVerdictValue,
        ReviewMemoryError,
        clean_text,
        current_policy_revision,
        isoformat,
        local_reference_number,
        normalize_repository,
        utc_now,
    )
    from .review_renderer import (
        ClosedFinding,
        PublishedFinding,
        ReviewBlock,
        ReviewCoverageSummary,
        render_review,
        review_blocks_from_json,
        review_blocks_to_json,
        review_markdown_from_blocks,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_decisions import active_suppression
    from memory_coverage import coverage_summary
    from memory_findings import assign_local_reference
    from memory_verification import (
        candidate_reconciliations_for_run,
        latest_verification_status_by_run,
    )
    from memory_validation import (
        HASH_RE,
        MAX_FINDINGS_PER_REVIEW,
        PRIOR_FINDING_VERDICTS,
        PriorFindingVerdictValue,
        ReviewMemoryError,
        clean_text,
        current_policy_revision,
        isoformat,
        local_reference_number,
        normalize_repository,
        utc_now,
    )
    from review_renderer import (
        ClosedFinding,
        PublishedFinding,
        ReviewBlock,
        ReviewCoverageSummary,
        render_review,
        review_blocks_from_json,
        review_blocks_to_json,
        review_markdown_from_blocks,
    )


ClosedFindingVerdict = Literal["resolved", "invalidated", "suppressed"]
PublicationStatus = Literal[
    "legacy_unverified",
    "generated",
    "posting",
    "posted",
    "publish_failed",
    "stale",
]
READY_TO_POST: frozenset[str] = frozenset({"generated", "publish_failed"})
PUBLICATION_MARKER_PREFIX = "eneo-review:canonical publication="


class PriorFindingVerdict(TypedDict):
    local_reference: str
    verdict: PriorFindingVerdictValue
    evidence: str


class PriorReconciliation(TypedDict):
    closed: list[ClosedFinding]
    carry_forward: list[str]
    still_present: list[str]
    partially_resolved: list[str]


class PublicationForPosting(TypedDict):
    publication_id: int
    review_run_id: int
    review_number: int | None
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    policy_revision: str
    publication_key: str
    rendered_markdown: str
    rendered_blocks_json: str
    rendered_hash: str
    comment_id: int | None
    supersedes_publication_id: int | None
    delivery_status: PublicationStatus


class PublicationForSupersession(TypedDict):
    publication_id: int
    review_number: int | None
    repository: str
    pr_number: int
    head_sha: str
    publication_key: str
    rendered_markdown: str
    rendered_blocks_json: str
    rendered_hash: str
    comment_ids: list[int]
    current_findings_count: int
    superseded_by_publication_id: int
    superseded_by_review_number: int | None
    superseded_by_head_sha: str
    superseded_by_comment_id: int


class VerificationExportSource(TypedDict):
    source_schema_version: int
    run: dict[str, Any]
    publication: dict[str, Any]
    current_findings: list[dict[str, Any]]


def _subject_row(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    head_sha: str,
    policy_revision: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM review_subjects
        WHERE repository = ? AND pr_number = ? AND head_sha = ? AND policy_revision = ?
        """,
        (repository, pr_number, head_sha, policy_revision),
    ).fetchone()
    return dict(row) if row else None


def _observations_for_run(
    connection: sqlite3.Connection, review_run_id: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM finding_observations
        WHERE review_run_id = ?
        ORDER BY id
        """,
        (review_run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _latest_publication(
    connection: sqlite3.Connection, repository: str, pr_number: int
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM review_publications
        WHERE repository = ? AND pr_number = ?
          AND delivery_status = 'posted'
          AND superseded_at IS NULL
          AND comment_id IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (repository, pr_number),
    ).fetchone()
    return dict(row) if row else None


def _publication_for_run(
    connection: sqlite3.Connection, review_run_id: int | None
) -> dict[str, Any] | None:
    if review_run_id is None:
        return None
    row = connection.execute(
        """
        SELECT * FROM review_publications
        WHERE review_run_id = ?
        """,
        (review_run_id,),
    ).fetchone()
    return dict(row) if row else None


def _publication_key(
    *,
    repository: str,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    policy_revision: str,
    review_run_id: int | None,
    markdown_hash: str,
) -> str:
    material = "\n".join(
        [
            repository,
            str(pr_number),
            base_sha,
            head_sha,
            policy_revision,
            str(review_run_id or ""),
            markdown_hash,
        ]
    )
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _with_publication_marker_blocks(
    blocks: Sequence[ReviewBlock], publication_key: str
) -> tuple[ReviewBlock, ...]:
    marker = publication_marker_html(publication_key)
    if any(marker in block.markdown for block in blocks):
        return tuple(blocks)
    return (*blocks, ReviewBlock(kind="metadata", markdown=marker))


def publication_marker(publication_key: str) -> str:
    return f"{PUBLICATION_MARKER_PREFIX}{publication_key}"


def publication_marker_html(publication_key: str) -> str:
    return f"<!-- {publication_marker(publication_key)} -->"


def extract_publication_key(body: str) -> str | None:
    token_index = body.find(PUBLICATION_MARKER_PREFIX)
    if token_index < 0:
        return None
    remainder = body[token_index + len(PUBLICATION_MARKER_PREFIX) :]
    parts = remainder.split()
    if not parts:
        return None
    key = parts[0].rstrip(" -\"'>")
    return key if key.startswith("sha256:") else None


def _publication_comment_ids(
    connection: sqlite3.Connection, publication_id: int
) -> list[int]:
    rows = connection.execute(
        """
        SELECT comment_id
        FROM review_publication_comments
        WHERE publication_id = ?
        ORDER BY part_number
        """,
        (publication_id,),
    ).fetchall()
    comment_ids = [int(row["comment_id"]) for row in rows]
    if comment_ids:
        return comment_ids
    fallback = connection.execute(
        """
        SELECT comment_id
        FROM review_publications
        WHERE id = ? AND comment_id IS NOT NULL
        """,
        (publication_id,),
    ).fetchone()
    return [int(fallback["comment_id"])] if fallback else []


def _next_review_number(
    connection: sqlite3.Connection, repository: str, pr_number: int
) -> int:
    row = connection.execute(
        """
        SELECT COALESCE(MAX(review_number), 0) + 1
        FROM review_publications
        WHERE repository = ? AND pr_number = ?
          AND delivery_status = 'posted'
          AND comment_id IS NOT NULL
        """,
        (repository, pr_number),
    ).fetchone()
    return int(row[0] if row else 1)


def publication_comment_ids(
    connection: sqlite3.Connection, publication_id: int
) -> list[int]:
    publication_id = int(publication_id)
    if publication_id < 1:
        raise ReviewMemoryError("publication_id must be positive")
    return _publication_comment_ids(connection, publication_id)


def list_publications(
    connection: sqlite3.Connection,
    *,
    repository: str | None = None,
    pr_number: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if pr_number is not None:
        pr_number = int(pr_number)
        if pr_number < 1:
            raise ReviewMemoryError("pr_number must be positive")
    repository = normalize_repository(repository) if repository else None
    limit = max(1, min(int(limit), 500))

    conditions: list[str] = []
    params: list[object] = []
    if repository:
        conditions.append("repository = ?")
        params.append(repository)
    if pr_number is not None:
        conditions.append("pr_number = ?")
        params.append(pr_number)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = connection.execute(
        f"""
        SELECT id, review_run_id, review_number, repository, pr_number, delivery_status,
               comment_id, failure_code, generated_at, posting_started_at,
               posted_at, publish_failed_at, superseded_at, base_sha, head_sha,
               supersedes_publication_id, superseded_by_publication_id,
               supersession_rendered_at, supersession_failure_code
        FROM review_publications
        {where}
        ORDER BY generated_at DESC, id DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    publications: list[dict[str, Any]] = []
    verification_statuses = latest_verification_status_by_run(
        connection,
        [
            int(row["review_run_id"])
            for row in rows
            if row["review_run_id"] is not None
        ],
    )
    for row in rows:
        item = dict(row)
        item["comment_ids"] = _publication_comment_ids(connection, int(item["id"]))
        verification = verification_statuses.get(int(item["review_run_id"] or 0))
        item["verification_status"] = str(verification["status"]) if verification else ""
        item["verification_mode"] = str(verification["mode"]) if verification else ""
        item["verification_provider"] = (
            str(verification["provider"]) if verification else ""
        )
        item["verification_failure_code"] = (
            str(verification["failure_code"]) if verification else ""
        )
        publications.append(item)
    return publications


def _publication_findings(
    connection: sqlite3.Connection, publication_id: int
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM publication_findings
        WHERE publication_id = ?
        ORDER BY id
        """,
        (publication_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def verification_export_source(
    connection: sqlite3.Connection, *, review_run_id: int
) -> VerificationExportSource:
    review_run_id = int(review_run_id)
    if review_run_id < 1:
        raise ReviewMemoryError("review_run_id must be positive")
    run = connection.execute(
        """
        SELECT id, repository, pr_number, base_sha, head_sha, status, phase,
               started_at, completed_at
        FROM review_runs
        WHERE id = ?
        """,
        (review_run_id,),
    ).fetchone()
    if run is None:
        raise ReviewMemoryError("review run was not found")

    publication = connection.execute(
        """
        SELECT id, review_run_id, review_number, repository, pr_number, base_sha,
               head_sha, delivery_status, rendered_hash, generated_at
        FROM review_publications
        WHERE review_run_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (review_run_id,),
    ).fetchone()
    if publication is None:
        raise ReviewMemoryError("review run has no recorded publication")

    publication_id = int(publication["id"])
    expected = _publication_current_finding_count(connection, publication_id)
    findings = connection.execute(
        """
        SELECT pf.local_reference, pf.status, pf.fingerprint, pf.context_hash,
               fo.id AS observation_id, fo.rule_id, fo.path, fo.line, fo.symbol,
               fo.anchor, fo.title, fo.severity, fo.category,
               fo.publication_score, fo.confidence, fo.evidence,
               fo.disproof_checks, fo.impact, fo.smallest_fix,
               fo.introduced_by_diff
        FROM publication_findings pf
        JOIN finding_observations fo ON fo.id = pf.observation_id
        WHERE pf.publication_id = ? AND pf.status = 'current'
        ORDER BY CAST(SUBSTR(pf.local_reference, 2) AS INTEGER), pf.id
        """,
        (publication_id,),
    ).fetchall()
    if expected != len(findings):
        raise ReviewMemoryError(
            "current publication has findings without observation evidence"
        )

    schema_row = connection.execute("PRAGMA user_version").fetchone()
    return {
        "source_schema_version": int(schema_row[0] if schema_row else 0),
        "run": dict(run),
        "publication": dict(publication),
        "current_findings": [dict(row) for row in findings],
    }


def _publication_result(
    connection: sqlite3.Connection, publication: Mapping[str, Any]
) -> dict[str, Any]:
    current = connection.execute(
        """
        SELECT COUNT(*) FROM publication_findings
        WHERE publication_id = ? AND status = 'current'
        """,
        (int(publication["id"]),),
    ).fetchone()
    closed = connection.execute(
        """
        SELECT COUNT(*) FROM publication_findings
        WHERE publication_id = ? AND status = 'resolved'
        """,
        (int(publication["id"]),),
    ).fetchone()
    markdown = str(publication.get("rendered_markdown") or "")
    if not markdown:
        raise ReviewMemoryError("stored publication is missing rendered_markdown")
    return {
        "publication_id": int(publication["id"]),
        "review_number": (
            int(publication["review_number"])
            if publication.get("review_number") is not None
            else None
        ),
        "repository": str(publication["repository"]),
        "pr_number": int(publication["pr_number"]),
        "base_sha": str(publication.get("base_sha") or ""),
        "head_sha": str(publication["head_sha"]),
        "policy_revision": str(publication["policy_revision"]),
        "review_run_id": (
            int(publication["review_run_id"])
            if publication.get("review_run_id") is not None
            else None
        ),
        "publication_key": str(publication.get("publication_key") or ""),
        "delivery_status": str(publication.get("delivery_status") or ""),
        "comment_id": (
            int(publication["comment_id"])
            if publication.get("comment_id") is not None
            else None
        ),
        "comment_ids": _publication_comment_ids(connection, int(publication["id"])),
        "supersedes_publication_id": (
            int(publication["supersedes_publication_id"])
            if publication.get("supersedes_publication_id") is not None
            else None
        ),
        "superseded_by_publication_id": (
            int(publication["superseded_by_publication_id"])
            if publication.get("superseded_by_publication_id") is not None
            else None
        ),
        "findings_count": int(current[0] if current else 0),
        "resolved_count": int(closed[0] if closed else 0),
        "closed_count": int(closed[0] if closed else 0),
        "rendered_blocks_json": str(publication.get("rendered_blocks_json") or ""),
        "rendered_hash": str(publication["rendered_hash"]),
        "markdown": markdown,
    }


def _finding_payload(
    item: dict[str, Any],
    *,
    local_reference: str,
    context_hash: str,
    observation_id: int | None = None,
    review_status: Literal["observed", "carried_forward"],
) -> PublishedFinding:
    if observation_id is None and item.get("id") is not None:
        observation_id = int(item["id"])
    return {
        "local_reference": local_reference,
        "fingerprint": str(item["fingerprint"]),
        "observation_id": observation_id,
        "context_hash": context_hash,
        "review_status": review_status,
        "rule_id": str(item["rule_id"]),
        "category": str(item["category"]),
        "path": str(item["path"]),
        "line": int(item["line"]),
        "title": str(item["title"]),
        "severity": str(item["severity"]),
        "publication_score": int(item["publication_score"]),
        "evidence": str(item["evidence"]),
        "disproof_checks": str(item["disproof_checks"]),
        "impact": str(item["impact"]),
        "smallest_fix": str(item["smallest_fix"]),
    }


def _feedback_enabled() -> bool:
    return os.environ.get("ENEO_REVIEW_FEEDBACK_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _closed_payload(
    item: dict[str, Any],
    *,
    verdict: ClosedFindingVerdict,
    evidence: str,
    title: str = "",
) -> ClosedFinding:
    return {
        "local_reference": str(item["local_reference"]),
        "fingerprint": str(item["fingerprint"]),
        "observation_id": (
            int(item["observation_id"])
            if item.get("observation_id") is not None
            else None
        ),
        "context_hash": str(item["context_hash"]),
        "verdict": verdict,
        "title": title,
        "evidence": evidence,
    }


def _prior_verdict_value(value: str, *, field: str) -> PriorFindingVerdictValue:
    if value not in PRIOR_FINDING_VERDICTS:
        raise ReviewMemoryError(f"{field} is not supported")
    return cast(PriorFindingVerdictValue, value)


def _normalize_previous_verdicts(
    previous_verdicts: object,
) -> dict[str, PriorFindingVerdict]:
    if previous_verdicts is None:
        return {}
    if not isinstance(previous_verdicts, Sequence) or isinstance(
        previous_verdicts, (str, bytes)
    ):
        raise ReviewMemoryError("previous_verdicts must be an array")
    verdict_items = cast(Sequence[object], previous_verdicts)
    if len(verdict_items) > MAX_FINDINGS_PER_REVIEW:
        raise ReviewMemoryError("previous_verdicts contains too many items")

    verdicts: dict[str, PriorFindingVerdict] = {}
    for index, raw_item in enumerate(verdict_items):
        if not isinstance(raw_item, Mapping):
            raise ReviewMemoryError(
                f"previous_verdicts[{index}] must be an object"
            )
        item = cast(Mapping[object, object], raw_item)
        local_reference = clean_text(
            item.get("local_reference"),
            field=f"previous_verdicts[{index}].local_reference",
            maximum=12,
        ).upper()
        if local_reference_number(local_reference) < 1:
            raise ReviewMemoryError(
                f"previous_verdicts[{index}].local_reference must be F1, F2, ..."
            )
        if local_reference in verdicts:
            raise ReviewMemoryError(f"duplicate previous verdict for {local_reference}")

        raw_verdict = clean_text(
            item.get("verdict"),
            field=f"previous_verdicts[{index}].verdict",
            maximum=40,
        ).lower()

        verdicts[local_reference] = {
            "local_reference": local_reference,
            "verdict": _prior_verdict_value(
                raw_verdict,
                field=f"previous_verdicts[{index}].verdict",
            ),
            "evidence": clean_text(
                item.get("evidence"),
                field=f"previous_verdicts[{index}].evidence",
                maximum=500,
                required=False,
            ),
        }
    return verdicts


def _reconcile_prior_findings(
    previous_current: Mapping[str, dict[str, Any]],
    current_by_fingerprint: Mapping[str, PublishedFinding],
    previous_verdicts: Mapping[str, PriorFindingVerdict],
    suppressed_fingerprints: set[str],
) -> PriorReconciliation:
    by_reference = {
        str(item["local_reference"]): fingerprint
        for fingerprint, item in previous_current.items()
    }
    for local_reference in previous_verdicts:
        if local_reference not in by_reference:
            raise ReviewMemoryError(
                f"previous verdict {local_reference} does not match a current prior finding"
            )

    closed: list[ClosedFinding] = []
    carry_forward: list[str] = []
    still_present: list[str] = []
    partially_resolved: list[str] = []
    for fingerprint, item in previous_current.items():
        local_reference = str(item["local_reference"])
        supplied = local_reference in previous_verdicts
        if supplied:
            verdict = previous_verdicts[local_reference]
        else:
            verdict: PriorFindingVerdict = {
                "local_reference": local_reference,
                "verdict": "not_checked",
                "evidence": "",
            }
        observed = fingerprint in current_by_fingerprint

        if observed:
            if not supplied or verdict["verdict"] == "still_present":
                still_present.append(local_reference)
                continue
            if verdict["verdict"] == "partially_resolved":
                partially_resolved.append(local_reference)
                continue
            raise ReviewMemoryError(
                f"previous verdict {local_reference}={verdict['verdict']} conflicts "
                "with a newly recorded finding"
            )

        if fingerprint in suppressed_fingerprints:
            closed.append(
                _closed_payload(
                    item,
                    verdict="suppressed",
                    evidence=(
                        verdict["evidence"]
                        or "A current human suppression matches this file version."
                    ),
                )
            )
            continue

        if verdict["verdict"] == "not_checked":
            carry_forward.append(fingerprint)
            continue
        if verdict["verdict"] == "resolved":
            closed.append(
                _closed_payload(
                    item,
                    verdict="resolved",
                    evidence=verdict["evidence"],
                )
            )
            continue
        if verdict["verdict"] == "invalidated":
            closed.append(
                _closed_payload(
                    item,
                    verdict="invalidated",
                    evidence=verdict["evidence"],
                )
            )
            continue
        if verdict["verdict"] == "suppressed":
            raise ReviewMemoryError(
                f"previous verdict {local_reference}=suppressed has no active human suppression"
            )
        raise ReviewMemoryError(
            f"previous verdict {local_reference}={verdict['verdict']} must also record "
            "the still-current finding"
        )

    return {
        "closed": closed,
        "carry_forward": carry_forward,
        "still_present": still_present,
        "partially_resolved": partially_resolved,
    }


def _latest_observation_for_pr(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    fingerprint: str,
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM finding_observations
        WHERE repository = ? AND pr_number = ? AND fingerprint = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (repository, pr_number, fingerprint),
    ).fetchone()
    if row:
        return dict(row)
    fallback = connection.execute(
        """
        SELECT fingerprint, rule_id, path, line, symbol, anchor, title, severity,
               category, publication_score, confidence, context_hash, evidence,
               disproof_checks, impact, smallest_fix
        FROM findings
        WHERE fingerprint = ?
        """,
        (fingerprint,),
    ).fetchone()
    return dict(fallback) if fallback else None


def finalize_review(
    connection: sqlite3.Connection,
    repository: str,
    pr_number: int,
    head_sha: str,
    *,
    review_run_id: int | None = None,
    previous_verdicts: object = None,
    policy_revision: str | None = None,
    comment_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    repository = normalize_repository(repository)
    pr_number = int(pr_number)
    if pr_number < 1:
        raise ReviewMemoryError("pr_number must be positive")
    head_sha = clean_text(head_sha, field="head_sha", maximum=64).lower()
    if not HASH_RE.fullmatch(head_sha):
        raise ReviewMemoryError(
            "head_sha must be an exact 40 to 64 character hexadecimal commit SHA"
        )
    if review_run_id is None or isinstance(review_run_id, bool):
        raise ReviewMemoryError("review_run_id must be a positive integer")
    review_run_id = int(review_run_id)
    if review_run_id < 1:
        raise ReviewMemoryError("review_run_id must be a positive integer")
    policy = current_policy_revision(policy_revision)
    moment = now or utc_now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        existing_publication = _publication_for_run(connection, review_run_id)
        if existing_publication:
            connection.commit()
            return _publication_result(connection, existing_publication)
        run = connection.execute(
            """
            SELECT repository, pr_number, head_sha
            FROM review_runs
            WHERE id = ?
            """,
            (review_run_id,),
        ).fetchone()
        if not run:
            raise ReviewMemoryError("review_run_id does not match a recorded review run")
        if (
            str(run["repository"]) != repository
            or int(run["pr_number"]) != pr_number
            or str(run["head_sha"]).lower() != head_sha
        ):
            raise ReviewMemoryError("review_run_id does not match this review subject")
        subject = _subject_row(connection, repository, pr_number, head_sha, policy)
        if not subject:
            raise ReviewMemoryError(
                "no review subject was recorded for this head and policy"
            )
        base_sha = str(subject.get("base_sha") or "")

        observed = _observations_for_run(connection, review_run_id)
        candidate_reconciliations = candidate_reconciliations_for_run(
            connection, review_run_id
        )
        # A drop is a final publication decision for this run, so it must exclude
        # both fresh observations and prior findings that would otherwise carry over.
        dropped_fingerprints = {
            fingerprint
            for fingerprint, item in candidate_reconciliations.items()
            if item.get("final_decision") == "drop"
        }
        current: list[PublishedFinding] = []
        for item in observed:
            fingerprint = str(item["fingerprint"])
            if fingerprint in dropped_fingerprints:
                continue
            context_hash = str(item["context_hash"])
            if active_suppression(connection, fingerprint, context_hash=context_hash):
                continue
            local_reference = assign_local_reference(
                connection, repository, pr_number, fingerprint, now=moment
            )
            current.append(
                _finding_payload(
                    item,
                    local_reference=local_reference,
                    context_hash=context_hash,
                    observation_id=int(item["id"]) if item.get("id") is not None else None,
                    review_status="observed",
                )
            )

        previous = _latest_publication(connection, repository, pr_number)
        review_number = _next_review_number(connection, repository, pr_number)
        previous_items = (
            _publication_findings(connection, previous["id"]) if previous else []
        )
        previous_current = {
            item["fingerprint"]: item
            for item in previous_items
            if item.get("status") == "current"
            and item["fingerprint"] not in dropped_fingerprints
        }
        current_by_fingerprint = {item["fingerprint"]: item for item in current}
        suppressed_previous = {
            fingerprint
            for fingerprint, item in previous_current.items()
            if active_suppression(
                connection,
                fingerprint,
                context_hash=str(item["context_hash"]),
            )
        }
        reconciliation = _reconcile_prior_findings(
            previous_current,
            current_by_fingerprint,
            _normalize_previous_verdicts(previous_verdicts),
            suppressed_previous,
        )
        closed = reconciliation["closed"]
        for item in closed:
            latest = _latest_observation_for_pr(
                connection, repository, pr_number, item["fingerprint"]
            )
            if latest:
                item["title"] = str(latest.get("title", ""))

        needs_recheck: list[str] = []
        for fingerprint in reconciliation["carry_forward"]:
            item = previous_current[fingerprint]
            # A carried finding has no fresh trusted file hash. Reuse the hash
            # from the previously published finding so suppressions apply only
            # to the exact file version a human or prior review saw.
            context_hash = str(item["context_hash"])
            carried = _latest_observation_for_pr(
                connection, repository, pr_number, fingerprint
            )
            if carried is None:
                continue
            local_reference = str(item["local_reference"])
            current.append(
                _finding_payload(
                    carried,
                    local_reference=local_reference,
                    context_hash=context_hash,
                    observation_id=(
                        int(item["observation_id"])
                        if item.get("observation_id") is not None
                        else None
                    ),
                    review_status="carried_forward",
                )
            )
            current_by_fingerprint[fingerprint] = current[-1]
            needs_recheck.append(local_reference)

        still_present = reconciliation["still_present"]
        partially_resolved = reconciliation["partially_resolved"]
        new_refs = [
            item["local_reference"]
            for fingerprint, item in current_by_fingerprint.items()
            if previous and fingerprint not in previous_current
        ]

        rendered = render_review(
            repository=repository,
            pr_number=pr_number,
            head_sha=head_sha,
            findings=current,
            closed=closed,
            still_present=still_present,
            partially_resolved=partially_resolved,
            new_refs=new_refs,
            needs_recheck=needs_recheck,
            feedback_enabled=_feedback_enabled(),
            coverage=cast(
                ReviewCoverageSummary | None,
                coverage_summary(connection, run_id=review_run_id),
            ),
            review_number=review_number,
            previous_review_number=(
                int(previous["review_number"])
                if previous and previous.get("review_number") is not None
                else None
            ),
            previous_head_sha=str(previous.get("head_sha") or "") if previous else "",
        )
        markdown = rendered.markdown
        markdown_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
        key = _publication_key(
            repository=repository,
            pr_number=pr_number,
            base_sha=base_sha,
            head_sha=head_sha,
            policy_revision=policy,
            review_run_id=review_run_id,
            markdown_hash=markdown_hash,
        )
        blocks = _with_publication_marker_blocks(rendered.blocks, key)
        markdown = review_markdown_from_blocks(blocks)
        rendered_blocks_json = review_blocks_to_json(blocks)
        rendered_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()

        cursor = connection.execute(
            """
            INSERT INTO review_publications (
                review_run_id, review_number, repository, pr_number, base_sha,
                head_sha, policy_revision, publication_key, comment_id,
                rendered_markdown, rendered_blocks_json, rendered_hash,
                delivery_status, published_at, generated_at,
                supersedes_publication_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'generated', ?, ?, ?)
            """,
            (
                review_run_id,
                review_number,
                repository,
                pr_number,
                base_sha,
                head_sha,
                policy,
                key,
                int(comment_id) if comment_id is not None else None,
                markdown,
                rendered_blocks_json,
                rendered_hash,
                isoformat(moment),
                isoformat(moment),
                int(previous["id"]) if previous else None,
            ),
        )
        if cursor.lastrowid is None:
            raise ReviewMemoryError("failed to record review publication")
        publication_id = cursor.lastrowid
        for item in current:
            connection.execute(
                """
                INSERT INTO publication_findings (
                    publication_id, local_reference, fingerprint, observation_id,
                    context_hash, status
                ) VALUES (?, ?, ?, ?, ?, 'current')
                """,
                (
                    publication_id,
                    item["local_reference"],
                    item["fingerprint"],
                    item["observation_id"],
                    item["context_hash"],
                ),
            )
        for item in closed:
            connection.execute(
                """
                INSERT INTO publication_findings (
                    publication_id, local_reference, fingerprint, observation_id,
                    context_hash, status
                ) VALUES (?, ?, ?, ?, ?, 'resolved')
                """,
                (
                    publication_id,
                    item["local_reference"],
                    item["fingerprint"],
                    item["observation_id"],
                    item["context_hash"],
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return {
        "publication_id": publication_id,
        "repository": repository,
        "pr_number": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "policy_revision": policy,
        "review_run_id": review_run_id,
        "review_number": review_number,
        "publication_key": key,
        "delivery_status": "generated",
        "findings_count": len(current),
        "resolved_count": sum(1 for item in closed if item["verdict"] == "resolved"),
        "closed_count": len(closed),
        "rendered_blocks_json": rendered_blocks_json,
        "rendered_hash": rendered_hash,
        "markdown": markdown,
    }


def _publication_for_posting(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    review_run_id: int | None,
) -> PublicationForPosting:
    if review_run_id is None:
        row = connection.execute(
            """
            SELECT * FROM review_publications
            WHERE id = ? AND review_run_id IS NULL
            """,
            (publication_id,),
        ).fetchone()
    else:
        row = connection.execute(
            """
            SELECT * FROM review_publications
            WHERE id = ? AND review_run_id = ?
            """,
            (publication_id, review_run_id),
        ).fetchone()
    if not row:
        raise ReviewMemoryError("publication_id and review_run_id do not match")
    status = str(row["delivery_status"])
    if status not in {
        "legacy_unverified",
        "generated",
        "posting",
        "posted",
        "publish_failed",
        "stale",
    }:
        raise ReviewMemoryError("publication has an unknown delivery_status")
    body = str(row["rendered_markdown"] or "")
    blocks_json = str(row["rendered_blocks_json"] or "")
    rendered_hash = str(row["rendered_hash"] or "")
    if not body:
        raise ReviewMemoryError("publication is missing rendered_markdown")
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != rendered_hash:
        raise ReviewMemoryError("publication rendered_markdown hash mismatch")
    if blocks_json:
        try:
            blocks = review_blocks_from_json(blocks_json, fallback_markdown=body)
        except ValueError as exc:
            raise ReviewMemoryError("publication rendered_blocks_json is invalid") from exc
        if review_markdown_from_blocks(blocks) != body:
            raise ReviewMemoryError("publication rendered_blocks_json does not match markdown")
    return {
        "publication_id": publication_id,
        "review_run_id": int(row["review_run_id"] or 0),
        "review_number": (
            int(row["review_number"]) if row["review_number"] is not None else None
        ),
        "repository": str(row["repository"]),
        "pr_number": int(row["pr_number"]),
        "base_sha": str(row["base_sha"] or ""),
        "head_sha": str(row["head_sha"]),
        "policy_revision": str(row["policy_revision"]),
        "publication_key": str(row["publication_key"]),
        "rendered_markdown": body,
        "rendered_blocks_json": blocks_json,
        "rendered_hash": rendered_hash,
        "comment_id": int(row["comment_id"]) if row["comment_id"] is not None else None,
        "supersedes_publication_id": (
            int(row["supersedes_publication_id"])
            if row["supersedes_publication_id"] is not None
            else None
        ),
        "delivery_status": cast(PublicationStatus, status),
    }


def _publication_current_finding_count(
    connection: sqlite3.Connection, publication_id: int
) -> int:
    current = connection.execute(
        """
        SELECT COUNT(*)
        FROM publication_findings
        WHERE publication_id = ?
          AND status = 'current'
        """,
        (publication_id,),
    ).fetchone()
    return int(current[0] if current else 0)


def claim_publication_for_posting(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    review_run_id: int,
    now: datetime | None = None,
) -> PublicationForPosting:
    publication_id = int(publication_id)
    review_run_id = int(review_run_id)
    if publication_id < 1 or review_run_id < 1:
        raise ReviewMemoryError("publication_id and review_run_id must be positive")
    moment = isoformat(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        publication = _publication_for_posting(
            connection, publication_id=publication_id, review_run_id=review_run_id
        )
        status = publication["delivery_status"]
        if status == "posted":
            connection.commit()
            return publication
        if status not in READY_TO_POST:
            raise ReviewMemoryError(f"publication is not ready to publish: {status}")
        cursor = connection.execute(
            """
            UPDATE review_publications
            SET delivery_status = 'posting',
                posting_started_at = ?,
                publish_failed_at = NULL,
                failure_code = ''
            WHERE id = ? AND review_run_id = ?
              AND delivery_status IN ('generated', 'publish_failed')
            """,
            (moment, publication_id, review_run_id),
        )
        if cursor.rowcount != 1:
            raise ReviewMemoryError("publication was claimed by another publisher")
        claimed = _publication_for_posting(
            connection, publication_id=publication_id, review_run_id=review_run_id
        )
        connection.commit()
        return claimed
    except Exception:
        connection.rollback()
        raise


def mark_publication_posted(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    review_run_id: int | None = None,
    comment_id: int,
    comment_ids: Sequence[int] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if isinstance(comment_id, bool) or publication_id < 1 or comment_id < 1:
        raise ReviewMemoryError("publication_id and comment_id must be positive")
    if review_run_id is not None and review_run_id < 1:
        raise ReviewMemoryError("review_run_id must be positive")
    recorded_comment_ids: list[int] = [int(comment_id)]
    if comment_ids is not None:
        recorded_comment_ids = []
        for value in comment_ids:
            if isinstance(value, bool):
                raise ReviewMemoryError("comment_ids must contain positive integers")
            comment_part_id = int(value)
            if comment_part_id < 1:
                raise ReviewMemoryError("comment_ids must contain positive integers")
            recorded_comment_ids.append(comment_part_id)
        if not recorded_comment_ids:
            raise ReviewMemoryError("comment_ids must not be empty")
        if recorded_comment_ids[0] != int(comment_id):
            raise ReviewMemoryError("comment_id must match the first comment_ids item")
    moment = isoformat(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        publication = _publication_for_posting(
            connection, publication_id=publication_id, review_run_id=review_run_id
        )
        if publication["delivery_status"] not in {"generated", "posting", "posted"}:
            raise ReviewMemoryError(
                "publication must be generated or posting before it can be marked posted"
            )
        connection.execute(
            """
            UPDATE review_publications
            SET superseded_at = ?,
                superseded_by_publication_id = ?,
                supersession_failure_code = ''
            WHERE repository = ? AND pr_number = ?
              AND delivery_status = 'posted'
              AND superseded_at IS NULL
              AND id != ?
            """,
            (
                moment,
                publication_id,
                publication["repository"],
                publication["pr_number"],
                publication_id,
            ),
        )
        connection.execute(
            """
            UPDATE review_publications
            SET delivery_status = 'posted',
                comment_id = ?,
                posted_at = ?,
                failure_code = '',
                superseded_at = NULL,
                superseded_by_publication_id = NULL,
                supersession_failure_code = ''
            WHERE id = ?
              AND (review_run_id = ? OR (? IS NULL AND review_run_id IS NULL))
            """,
            (comment_id, moment, publication_id, review_run_id, review_run_id),
        )
        if review_run_id is not None:
            current_findings = _publication_current_finding_count(
                connection, publication_id
            )
            connection.execute(
                """
                UPDATE review_runs
                SET status = 'generated',
                    phase = 'posted',
                    findings_count = ?,
                    posted_comment_id = ?,
                    completed_at = ?,
                    last_heartbeat_at = ?,
                    failure_code = ''
                WHERE id = ?
                  AND status = 'running'
                """,
                (current_findings, comment_id, moment, moment, review_run_id),
            )
        connection.execute(
            "DELETE FROM review_publication_comments WHERE publication_id = ?",
            (publication_id,),
        )
        for part_number, part_comment_id in enumerate(recorded_comment_ids, start=1):
            connection.execute(
                """
                INSERT INTO review_publication_comments (
                    publication_id, part_number, comment_id, posted_at
                ) VALUES (?, ?, ?, ?)
                """,
                (publication_id, part_number, part_comment_id, moment),
            )
        updated = _publication_for_posting(
            connection, publication_id=publication_id, review_run_id=review_run_id
        )
        connection.commit()
        return dict(updated)
    except Exception:
        connection.rollback()
        raise


def publication_for_supersession(
    connection: sqlite3.Connection, superseding_publication_id: int
) -> PublicationForSupersession | None:
    if isinstance(superseding_publication_id, bool) or superseding_publication_id < 1:
        raise ReviewMemoryError("superseding_publication_id must be positive")
    row = connection.execute(
        """
        SELECT old.*, new.review_number AS superseded_by_review_number,
               new.head_sha AS superseded_by_head_sha,
               new.comment_id AS superseded_by_comment_id
        FROM review_publications AS old
        JOIN review_publications AS new
          ON new.id = old.superseded_by_publication_id
        WHERE old.superseded_by_publication_id = ?
          AND old.comment_id IS NOT NULL
          AND new.comment_id IS NOT NULL
        ORDER BY old.id DESC
        LIMIT 1
        """,
        (int(superseding_publication_id),),
    ).fetchone()
    if row is None:
        return None
    body = str(row["rendered_markdown"] or "")
    rendered_hash = str(row["rendered_hash"] or "")
    if not body or hashlib.sha256(body.encode("utf-8")).hexdigest() != rendered_hash:
        raise ReviewMemoryError("superseded publication markdown is not trusted")
    return {
        "publication_id": int(row["id"]),
        "review_number": (
            int(row["review_number"]) if row["review_number"] is not None else None
        ),
        "repository": str(row["repository"]),
        "pr_number": int(row["pr_number"]),
        "head_sha": str(row["head_sha"]),
        "publication_key": str(row["publication_key"]),
        "rendered_markdown": body,
        "rendered_blocks_json": str(row["rendered_blocks_json"] or ""),
        "rendered_hash": rendered_hash,
        "comment_ids": _publication_comment_ids(connection, int(row["id"])),
        "current_findings_count": _publication_current_finding_count(
            connection, int(row["id"])
        ),
        "superseded_by_publication_id": int(row["superseded_by_publication_id"]),
        "superseded_by_review_number": (
            int(row["superseded_by_review_number"])
            if row["superseded_by_review_number"] is not None
            else None
        ),
        "superseded_by_head_sha": str(row["superseded_by_head_sha"] or ""),
        "superseded_by_comment_id": int(row["superseded_by_comment_id"]),
    }


def mark_supersession_rendered(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    failure_code: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    if isinstance(publication_id, bool) or publication_id < 1:
        raise ReviewMemoryError("publication_id must be positive")
    failure_code = clean_text(
        failure_code, field="failure_code", maximum=80, required=False
    )
    moment = isoformat(now)
    with connection:
        connection.execute(
            """
            UPDATE review_publications
            SET supersession_rendered_at = ?,
                supersession_failure_code = ?
            WHERE id = ?
            """,
            (moment, failure_code, int(publication_id)),
        )
    return {
        "publication_id": int(publication_id),
        "supersession_rendered_at": moment,
        "supersession_failure_code": failure_code,
    }


def mark_publication_failed(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    review_run_id: int | None,
    failure_code: str,
    status: PublicationStatus = "publish_failed",
    now: datetime | None = None,
) -> dict[str, Any]:
    if status not in {"publish_failed", "stale"}:
        raise ReviewMemoryError("failed publication status must be publish_failed or stale")
    failure_code = clean_text(
        failure_code, field="failure_code", maximum=80, required=False
    ) or status
    moment = isoformat(now)
    connection.execute("BEGIN IMMEDIATE")
    try:
        _publication_for_posting(
            connection, publication_id=publication_id, review_run_id=review_run_id
        )
        current_findings = _publication_current_finding_count(
            connection, publication_id
        )
        connection.execute(
            """
            UPDATE review_publications
            SET delivery_status = ?,
                publish_failed_at = ?,
                failure_code = ?
            WHERE id = ?
              AND (review_run_id = ? OR (? IS NULL AND review_run_id IS NULL))
            """,
            (status, moment, failure_code, publication_id, review_run_id, review_run_id),
        )
        if review_run_id is not None:
            connection.execute(
                """
                UPDATE review_runs
                SET status = 'failed',
                    phase = 'failed',
                    findings_count = ?,
                    completed_at = ?,
                    last_heartbeat_at = ?,
                    failure_code = ?
                WHERE id = ?
                  AND status = 'running'
                """,
                (current_findings, moment, moment, failure_code, review_run_id),
            )
        updated = _publication_for_posting(
            connection, publication_id=publication_id, review_run_id=review_run_id
        )
        connection.commit()
        return dict(updated)
    except Exception:
        connection.rollback()
        raise
