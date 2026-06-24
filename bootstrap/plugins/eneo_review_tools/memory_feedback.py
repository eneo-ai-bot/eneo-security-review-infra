"""In-PR feedback routing, idempotency, and decision audit."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

try:
    from .memory_decisions import (
        current_context_hash_for_fingerprint,
        insert_decision,
        observation_id_for_context,
    )
    from .memory_identity import resolve_fingerprint
    from .memory_validation import (
        FEEDBACK_DECISIONS,
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        isoformat,
        normalize_context_hash,
        normalize_repository,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_decisions import (
        current_context_hash_for_fingerprint,
        insert_decision,
        observation_id_for_context,
    )
    from memory_identity import resolve_fingerprint
    from memory_validation import (
        FEEDBACK_DECISIONS,
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        isoformat,
        normalize_context_hash,
        normalize_repository,
    )


def link_review_comment(
    connection: sqlite3.Connection,
    *,
    review_comment_id: int,
    repository: str,
    pr_number: int,
    fingerprint: str,
    context_hash: str,
    head_sha: str = "",
    now: datetime | None = None,
) -> None:
    """Record, at publish time, that a posted inline review comment belongs to a finding.
    This mapping — not the comment's footer text — is the sole authority for routing a
    later threaded reply back to the exact finding it concerns."""
    repository = normalize_repository(repository)
    fingerprint = resolve_fingerprint(connection, fingerprint)
    review_comment_id = int(review_comment_id)
    pr_number = int(pr_number)
    context_hash = normalize_context_hash(context_hash) if context_hash else ""
    head_sha = clean_text(head_sha, field="head_sha", maximum=64, required=False)
    with connection:
        existing = connection.execute(
            """
            SELECT repository, pr_number, fingerprint, context_hash, head_sha
            FROM review_comment_links
            WHERE review_comment_id = ?
            """,
            (review_comment_id,),
        ).fetchone()
        if existing:
            current = dict(existing)
            if current == {
                "repository": repository,
                "pr_number": pr_number,
                "fingerprint": fingerprint,
                "context_hash": context_hash,
                "head_sha": head_sha,
            }:
                return
            raise ReviewMemoryError(
                "review_comment_id is already linked to a different finding"
            )
        connection.execute(
            """
            INSERT INTO review_comment_links (
                review_comment_id, repository, pr_number, fingerprint, context_hash,
                head_sha, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review_comment_id,
                repository,
                pr_number,
                fingerprint,
                context_hash,
                head_sha,
                isoformat(now),
            ),
        )


def finding_for_review_comment(
    connection: sqlite3.Connection, review_comment_id: int
) -> dict[str, Any] | None:
    """Look up the finding a posted inline review comment maps to. Returns None when the
    comment is not a recognized finding thread (so feedback on it is a clean no-op)."""
    row = connection.execute(
        "SELECT * FROM review_comment_links WHERE review_comment_id = ?",
        (int(review_comment_id),),
    ).fetchone()
    return dict(row) if row else None


def feedback_event(
    connection: sqlite3.Connection, event_id: str
) -> dict[str, Any] | None:
    event_id = clean_text(event_id, field="event_id", maximum=200)
    row = connection.execute(
        """
        SELECT event_id, outcome, processed_at
        FROM processed_feedback_events
        WHERE event_id = ?
        """,
        (event_id,),
    ).fetchone()
    return dict(row) if row else None


def record_feedback_decision(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    review_comment_id: int,
    decision: str,
    reason: str,
    actor_user_id: str,
    actor_login: str = "",
    author_association: str = "",
    allowlist_version: str = "",
    source_comment_id: int | None = None,
    source_comment_url: str = "",
    classifier_version: str = "",
    classifier_output: str = "",
    hmac_key_version: str = "",
    adr_id: str = "",
    expires_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Deterministically record a human feedback decision from a reply in an inline
    review-comment thread. The thread (review_comment_id), via the stored mapping,
    identifies the finding — the caller never picks it. Returns:
      - None: the event was already processed (replay) or the comment is not a known
        finding thread (clean no-op);
      - {"status": "stale", ...}: a suppressive decision whose finding's file changed
        since the comment was posted (do not suppress a version the human did not review);
      - {"status": "recorded", ...}: the decision + finding details for the bot confirmation.
    Raises for an out-of-scope decision (e.g. accepted_risk — a governance decision).
    Call with no open transaction; this function owns the BEGIN IMMEDIATE boundary."""
    decision = decision.strip().lower()
    if decision not in FEEDBACK_DECISIONS:
        raise ReviewMemoryError(
            f"feedback decision must be one of: {', '.join(sorted(FEEDBACK_DECISIONS))}"
        )
    event_id = clean_text(event_id, field="event_id", maximum=200)
    actor_user_id = clean_text(actor_user_id, field="actor_user_id", maximum=64)
    reason = clean_multiline(reason, field="reason", maximum=2000)
    actor_login = clean_text(
        actor_login, field="actor_login", maximum=200, required=False
    )
    author_association = clean_text(
        author_association, field="author_association", maximum=80, required=False
    )
    allowlist_version = clean_text(
        allowlist_version, field="allowlist_version", maximum=200, required=False
    )
    source_comment_url = clean_text(
        source_comment_url, field="source_comment_url", maximum=500, required=False
    )
    classifier_version = clean_text(
        classifier_version, field="classifier_version", maximum=100, required=False
    )
    classifier_output = clean_text(
        classifier_output, field="classifier_output", maximum=500, required=False
    )
    hmac_key_version = clean_text(
        hmac_key_version, field="hmac_key_version", maximum=100, required=False
    )
    adr_id = clean_text(adr_id, field="adr_id", maximum=80, required=False)
    review_comment_id = int(review_comment_id)
    source_comment_id = (
        int(source_comment_id) if source_comment_id is not None else None
    )

    # Event claim, context check, decision, and audit commit or roll back together.
    # Otherwise a crash can permanently consume a maintainer's feedback.
    connection.execute("BEGIN IMMEDIATE")
    try:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO processed_feedback_events (event_id, outcome, processed_at)
            VALUES (?, 'pending', ?)
            """,
            (event_id, isoformat(now)),
        )
        if cursor.rowcount == 0:
            connection.rollback()
            return None

        link = finding_for_review_comment(connection, review_comment_id)
        if link is None:
            # Consuming unmapped events is deliberate: webhook delivery should be
            # idempotent even when a reply is not attached to a known finding thread.
            connection.execute(
                "UPDATE processed_feedback_events SET outcome = 'no_mapping' WHERE event_id = ?",
                (event_id,),
            )
            connection.commit()
            return None

        fingerprint = link["fingerprint"]
        linked_hash = str(link["context_hash"] or "")
        current_hash = current_context_hash_for_fingerprint(connection, fingerprint)

        # A suppression may only apply to the exact file version the human reviewed.
        if decision in {"false_positive", "intentional_by_design"} and (
            not linked_hash or linked_hash != current_hash
        ):
            connection.execute(
                "UPDATE processed_feedback_events SET outcome = 'stale' WHERE event_id = ?",
                (event_id,),
            )
            connection.commit()
            return {"status": "stale", "fingerprint": fingerprint}

        actor = f"github-id:{actor_user_id}"
        observation_id = observation_id_for_context(
            connection,
            repository=link["repository"],
            pr_number=int(link["pr_number"]),
            fingerprint=fingerprint,
            head_sha=str(link["head_sha"] or ""),
            context_hash=linked_hash,
        )
        if observation_id is None:
            raise ReviewMemoryError(
                "review comment link does not resolve to a recorded finding observation"
            )
        result = insert_decision(
            connection,
            fingerprint=fingerprint,
            decision=decision,
            reason=reason,
            actor=actor,
            context_hash=current_hash,
            observation_id=observation_id,
            adr_id=adr_id,
            expires_days=expires_days,
            now=now,
        )
        connection.execute(
            """
            INSERT INTO decision_audit (
                decision_id, actor_user_id, actor_login, author_association, allowlist_version,
                review_comment_id, source_comment_id, source_comment_url, classifier_version,
                classifier_output, hmac_key_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result["id"],
                actor_user_id,
                actor_login,
                author_association,
                allowlist_version,
                review_comment_id,
                source_comment_id,
                source_comment_url,
                classifier_version,
                classifier_output,
                hmac_key_version,
                isoformat(now),
            ),
        )
        finding = connection.execute(
            "SELECT title FROM findings WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        connection.execute(
            "UPDATE processed_feedback_events SET outcome = 'recorded' WHERE event_id = ?",
            (event_id,),
        )
        connection.commit()
        return {
            "status": "recorded",
            "decision": result["decision"],
            "fingerprint": fingerprint,
            "title": finding["title"] if finding else "",
            "context_hash": result["context_hash"],
            "adr_id": result["adr_id"],
            "expires_at": result["expires_at"],
        }
    except Exception:
        connection.rollback()
        raise
