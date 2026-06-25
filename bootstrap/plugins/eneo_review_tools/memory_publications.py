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
    from .memory_findings import assign_local_reference
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
        render_review_markdown,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_decisions import active_suppression
    from memory_findings import assign_local_reference
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
    from review_renderer import ClosedFinding, PublishedFinding, render_review_markdown


ClosedFindingVerdict = Literal["resolved", "invalidated", "suppressed"]


class PriorFindingVerdict(TypedDict):
    local_reference: str
    verdict: PriorFindingVerdictValue
    evidence: str


class PriorReconciliation(TypedDict):
    closed: list[ClosedFinding]
    carry_forward: list[str]
    still_present: list[str]
    partially_resolved: list[str]


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


def _observations_for_subject(
    connection: sqlite3.Connection,
    subject_id: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT * FROM finding_observations
        WHERE review_subject_id = ?
        """,
        (subject_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _latest_publication(
    connection: sqlite3.Connection, repository: str, pr_number: int
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT * FROM review_publications
        WHERE repository = ? AND pr_number = ? AND superseded_at IS NULL
        ORDER BY id DESC
        LIMIT 1
        """,
        (repository, pr_number),
    ).fetchone()
    return dict(row) if row else None


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
    policy = current_policy_revision(policy_revision)
    moment = now or utc_now()
    connection.execute("BEGIN IMMEDIATE")
    try:
        subject = _subject_row(connection, repository, pr_number, head_sha, policy)
        if not subject:
            raise ReviewMemoryError(
                "no review subject was recorded for this head and policy"
            )

        observed = _observations_for_subject(connection, int(subject["id"]))
        current: list[PublishedFinding] = []
        for item in observed:
            fingerprint = str(item["fingerprint"])
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
        previous_items = (
            _publication_findings(connection, previous["id"]) if previous else []
        )
        previous_current = {
            item["fingerprint"]: item
            for item in previous_items
            if item.get("status") == "current"
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

        markdown = render_review_markdown(
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
        )
        rendered_hash = hashlib.sha256(markdown.encode("utf-8")).hexdigest()

        if previous:
            connection.execute(
                "UPDATE review_publications SET superseded_at = ? WHERE id = ?",
                (isoformat(moment), previous["id"]),
            )
        cursor = connection.execute(
            """
            INSERT INTO review_publications (
                repository, pr_number, head_sha, policy_revision, comment_id,
                rendered_hash, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repository,
                pr_number,
                head_sha,
                policy,
                int(comment_id) if comment_id is not None else None,
                rendered_hash,
                isoformat(moment),
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
        "head_sha": head_sha,
        "policy_revision": policy,
        "findings_count": len(current),
        "resolved_count": sum(1 for item in closed if item["verdict"] == "resolved"),
        "closed_count": len(closed),
        "rendered_hash": rendered_hash,
        "markdown": markdown,
    }
