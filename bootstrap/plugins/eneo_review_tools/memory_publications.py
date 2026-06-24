"""Review publication lifecycle and final comment assembly."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from typing import Any, Literal

try:
    from .memory_decisions import active_suppression
    from .memory_findings import assign_local_reference, local_reference_number
    from .memory_validation import (
        HASH_RE,
        ReviewMemoryError,
        clean_text,
        current_policy_revision,
        isoformat,
        normalize_repository,
        utc_now,
    )
    from .review_renderer import (
        PublishedFinding,
        ResolvedFinding,
        render_review_markdown,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_decisions import active_suppression
    from memory_findings import assign_local_reference, local_reference_number
    from memory_validation import (
        HASH_RE,
        ReviewMemoryError,
        clean_text,
        current_policy_revision,
        isoformat,
        normalize_repository,
        utc_now,
    )
    from review_renderer import PublishedFinding, ResolvedFinding, render_review_markdown


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
    review_status: Literal["observed", "carried_forward"],
) -> PublishedFinding:
    return {
        "local_reference": local_reference,
        "fingerprint": str(item["fingerprint"]),
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
        # Explicit verdicts are intentionally deferred. Until that path exists,
        # absence from the latest observation set carries a finding forward instead
        # of treating it as resolved. This hook is kept so the future verdict slice
        # can render resolved history without changing the renderer contract again.
        resolved: list[ResolvedFinding] = []
        needs_recheck: list[str] = []
        for fingerprint, item in previous_current.items():
            if fingerprint not in current_by_fingerprint:
                # A carried finding has no fresh trusted file hash. Reuse the hash
                # from the previously published finding so suppressions apply only
                # to the exact file version a human or prior review saw.
                context_hash = str(item["context_hash"])
                if active_suppression(connection, fingerprint, context_hash=context_hash):
                    continue
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
                        review_status="carried_forward",
                    )
                )
                current_by_fingerprint[fingerprint] = current[-1]
                needs_recheck.append(local_reference)
        still_present = [
            item["local_reference"]
            for fingerprint, item in current_by_fingerprint.items()
            if fingerprint in previous_current
            and item["review_status"] == "observed"
        ]
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
            resolved=resolved,
            still_present=sorted(still_present, key=local_reference_number),
            new_refs=sorted(new_refs, key=local_reference_number),
            needs_recheck=sorted(needs_recheck, key=local_reference_number),
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
                    publication_id, local_reference, fingerprint, context_hash, status
                ) VALUES (?, ?, ?, ?, 'current')
                """,
                (
                    publication_id,
                    item["local_reference"],
                    item["fingerprint"],
                    item["context_hash"],
                ),
            )
        for item in resolved:
            connection.execute(
                """
                INSERT INTO publication_findings (
                    publication_id, local_reference, fingerprint, context_hash, status
                ) VALUES (?, ?, ?, ?, 'resolved')
                """,
                (
                    publication_id,
                    item["local_reference"],
                    item["fingerprint"],
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
        "resolved_count": len(resolved),
        "rendered_hash": rendered_hash,
        "markdown": markdown,
    }
