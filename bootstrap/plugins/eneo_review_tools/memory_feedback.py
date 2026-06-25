"""In-PR feedback routing, idempotency, and decision audit."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

try:
    from .feedback_authorization import (
        AuthorizedFeedbackActor,
        authorize_feedback_actor,
    )
    from .feedback_commands import (
        ReviewQualityFeedbackCommand,
        parse_review_feedback_command,
    )
    from .memory_decisions import insert_decision
    from .memory_validation import (
        REVIEW_FEEDBACK_CATEGORIES,
        ReviewMemoryError,
        clean_text,
        isoformat,
        local_reference_number,
        normalize_repository,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from feedback_authorization import AuthorizedFeedbackActor, authorize_feedback_actor
    from feedback_commands import (
        ReviewQualityFeedbackCommand,
        parse_review_feedback_command,
    )
    from memory_decisions import insert_decision
    from memory_validation import (
        REVIEW_FEEDBACK_CATEGORIES,
        ReviewMemoryError,
        clean_text,
        isoformat,
        local_reference_number,
        normalize_repository,
    )

FeedbackStatus = Literal[
    "recorded",
    "replay",
    "no_mapping",
    "not_current",
    "stale",
    "unauthorized",
    "ignored",
    "unsupported",
]

__all__ = (
    "FeedbackResult",
    "FeedbackStatus",
    "FindingTarget",
    "PublicationTarget",
    "feedback_event",
    "record_review_feedback_comment",
    "resolve_current_review_state",
)


@dataclass(frozen=True)
class PublicationTarget:
    publication_id: int
    repository: str
    pr_number: int
    head_sha: str


@dataclass(frozen=True)
class FindingTarget:
    local_reference: str
    fingerprint: str
    observation_id: int
    trusted_context_hash: str


@dataclass(frozen=True)
class FeedbackResult:
    status: FeedbackStatus
    event_id: str
    decision_id: int | None = None
    feedback_id: int | None = None
    fingerprint: str = ""
    local_reference: str = ""
    title: str = ""
    context_hash: str = ""
    adr_id: str = ""
    expires_at: str | None = None


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


def _claim_feedback_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    now: datetime | None,
) -> bool:
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO processed_feedback_events (event_id, outcome, processed_at)
        VALUES (?, 'pending', ?)
        """,
        (event_id, isoformat(now)),
    )
    return cursor.rowcount == 1


def _set_feedback_outcome(
    connection: sqlite3.Connection, *, event_id: str, outcome: str
) -> None:
    connection.execute(
        "UPDATE processed_feedback_events SET outcome = ? WHERE event_id = ?",
        (outcome, event_id),
    )


def _insert_decision_audit(
    connection: sqlite3.Connection,
    *,
    decision_id: int,
    actor_user_id: str,
    actor_login: str,
    author_association: str,
    allowlist_version: str,
    source_comment_id: int | None,
    source_comment_url: str,
    classifier_version: str,
    classifier_output: str,
    hmac_key_version: str,
    now: datetime | None,
) -> None:
    connection.execute(
        """
        INSERT INTO decision_audit (
            decision_id, actor_user_id, actor_login, author_association, allowlist_version,
            review_comment_id, source_comment_id, source_comment_url, classifier_version,
            classifier_output, hmac_key_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            decision_id,
            actor_user_id,
            actor_login,
            author_association,
            allowlist_version,
            None,
            source_comment_id,
            source_comment_url,
            classifier_version,
            classifier_output,
            hmac_key_version,
            isoformat(now),
        ),
    )


def _positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or value < 1:
        raise ReviewMemoryError(f"{field} must be a positive integer")
    return value


def resolve_current_review_state(
    connection: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
) -> PublicationTarget | None:
    repository = normalize_repository(repository)
    pr_number = int(pr_number)
    if pr_number < 1:
        raise ReviewMemoryError("pr_number must be positive")
    rows = connection.execute(
        """
        SELECT id, repository, pr_number, head_sha
        FROM review_publications
        WHERE repository = ? AND pr_number = ? AND superseded_at IS NULL
        ORDER BY id DESC
        LIMIT 2
        """,
        (repository, pr_number),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise ReviewMemoryError("multiple current review publications exist")
    row = rows[0]
    return PublicationTarget(
        publication_id=int(row["id"]),
        repository=str(row["repository"]),
        pr_number=int(row["pr_number"]),
        head_sha=str(row["head_sha"]),
    )


def _normalize_local_reference(local_reference: str) -> str:
    reference = clean_text(
        local_reference, field="local_reference", maximum=12
    ).upper()
    if local_reference_number(reference) < 1:
        raise ReviewMemoryError("local_reference must look like F1, F2, ...")
    return reference


def _resolve_current_finding_target(
    connection: sqlite3.Connection,
    *,
    publication: PublicationTarget,
    local_reference: str,
) -> tuple[FindingTarget | None, FeedbackStatus]:
    reference = _normalize_local_reference(local_reference)
    row = connection.execute(
        """
        SELECT pf.local_reference, pf.fingerprint, pf.observation_id,
               pf.context_hash, pf.status,
               fo.id AS found_observation_id, fo.fingerprint AS observation_fingerprint
        FROM publication_findings pf
        LEFT JOIN finding_observations fo ON fo.id = pf.observation_id
        WHERE pf.publication_id = ? AND pf.local_reference = ?
        """,
        (publication.publication_id, reference),
    ).fetchone()
    if row is None or str(row["status"]) != "current":
        return None, "not_current"
    if row["observation_id"] is None or row["found_observation_id"] is None:
        return None, "no_mapping"
    fingerprint = str(row["fingerprint"])
    if str(row["observation_fingerprint"]) != fingerprint:
        return None, "no_mapping"
    context_hash = str(row["context_hash"] or "")
    if not context_hash:
        return None, "stale"
    return (
        FindingTarget(
            local_reference=reference,
            fingerprint=fingerprint,
            observation_id=int(row["observation_id"]),
            trusted_context_hash=context_hash,
        ),
        "recorded",
    )


def _finding_title(connection: sqlite3.Connection, fingerprint: str) -> str:
    row = connection.execute(
        "SELECT title FROM findings WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    return str(row["title"]) if row else ""


def _record_decision_for_target(
    connection: sqlite3.Connection,
    *,
    target: FindingTarget,
    decision: str,
    reason: str,
    adr_id: str,
    actor: AuthorizedFeedbackActor,
    actor_login: str,
    author_association: str,
    source_comment_id: int | None,
    source_comment_url: str,
    classifier_version: str,
    classifier_output: str,
    hmac_key_version: str,
    expires_days: int | None,
    now: datetime | None,
) -> dict[str, Any]:
    result = insert_decision(
        connection,
        fingerprint=target.fingerprint,
        decision=decision,
        reason=reason,
        actor=f"github-id:{actor.actor_user_id}",
        context_hash=target.trusted_context_hash,
        observation_id=target.observation_id,
        adr_id=adr_id,
        expires_days=expires_days,
        now=now,
    )
    _insert_decision_audit(
        connection,
        decision_id=int(result["id"]),
        actor_user_id=actor.actor_user_id,
        actor_login=actor_login,
        author_association=author_association,
        allowlist_version=actor.allowlist_version,
        source_comment_id=source_comment_id,
        source_comment_url=source_comment_url,
        classifier_version=classifier_version,
        classifier_output=classifier_output,
        hmac_key_version=hmac_key_version,
        now=now,
    )
    return result


def _record_quality_feedback(
    connection: sqlite3.Connection,
    *,
    publication: PublicationTarget,
    command: ReviewQualityFeedbackCommand,
    actor: AuthorizedFeedbackActor,
    actor_login: str,
    author_association: str,
    source_comment_id: int,
    source_comment_url: str,
    now: datetime | None,
) -> int:
    if command.category not in REVIEW_FEEDBACK_CATEGORIES:
        raise ReviewMemoryError("unknown review feedback category")
    cursor = connection.execute(
        """
        INSERT INTO review_quality_feedback (
            repository, pr_number, publication_id, head_sha, local_reference,
            category, reason, actor_user_id, actor_login, author_association,
            source_comment_id, source_comment_url, created_at
        ) VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            publication.repository,
            publication.pr_number,
            publication.publication_id,
            publication.head_sha,
            command.category,
            command.reason,
            actor.actor_user_id,
            actor_login,
            author_association,
            source_comment_id,
            source_comment_url,
            isoformat(now),
        ),
    )
    if cursor.lastrowid is None:
        raise ReviewMemoryError("failed to record review-quality feedback")
    return int(cursor.lastrowid)


def record_review_feedback_comment(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    repository: str,
    pr_number: int,
    body: str,
    actor_user_id: object,
    actor_login: str = "",
    author_association: str = "",
    source_comment_id: object,
    source_comment_url: str = "",
    allowed_actor_ids: str | frozenset[str] | None = None,
    expires_days: int | None = None,
    now: datetime | None = None,
) -> FeedbackResult:
    event_id = clean_text(event_id, field="event_id", maximum=200)
    if str(body or "").strip().lower() in {"@review", "/review"}:
        return FeedbackResult(status="ignored", event_id=event_id)

    actor = authorize_feedback_actor(
        actor_user_id, allowed_actor_ids=allowed_actor_ids
    )
    if actor is None:
        return FeedbackResult(status="unauthorized", event_id=event_id)

    command = parse_review_feedback_command(body)
    if command is None:
        return FeedbackResult(status="ignored", event_id=event_id)
    if (
        not isinstance(command, ReviewQualityFeedbackCommand)
        and command.decision == "intentional_by_design"
    ):
        return FeedbackResult(
            status="unsupported",
            event_id=event_id,
            local_reference=command.local_reference,
            adr_id=command.adr_id,
        )

    repository = normalize_repository(repository)
    pr_number = int(pr_number)
    if pr_number < 1:
        raise ReviewMemoryError("pr_number must be positive")
    source_comment = _positive_int(source_comment_id, field="source_comment_id")
    actor_login = clean_text(
        actor_login, field="actor_login", maximum=200, required=False
    )
    author_association = clean_text(
        author_association, field="author_association", maximum=80, required=False
    )
    source_comment_url = clean_text(
        source_comment_url, field="source_comment_url", maximum=500, required=False
    )

    connection.execute("BEGIN IMMEDIATE")
    try:
        if not _claim_feedback_event(connection, event_id=event_id, now=now):
            connection.rollback()
            return FeedbackResult(status="replay", event_id=event_id)

        publication = resolve_current_review_state(
            connection, repository=repository, pr_number=pr_number
        )
        if publication is None:
            _set_feedback_outcome(
                connection, event_id=event_id, outcome="no_mapping"
            )
            connection.commit()
            return FeedbackResult(status="no_mapping", event_id=event_id)

        if isinstance(command, ReviewQualityFeedbackCommand):
            feedback_id = _record_quality_feedback(
                connection,
                publication=publication,
                command=command,
                actor=actor,
                actor_login=actor_login,
                author_association=author_association,
                source_comment_id=source_comment,
                source_comment_url=source_comment_url,
                now=now,
            )
            _set_feedback_outcome(connection, event_id=event_id, outcome="recorded")
            connection.commit()
            return FeedbackResult(
                status="recorded", event_id=event_id, feedback_id=feedback_id
            )

        target, target_status = _resolve_current_finding_target(
            connection,
            publication=publication,
            local_reference=command.local_reference,
        )
        if target is None:
            _set_feedback_outcome(
                connection, event_id=event_id, outcome=target_status
            )
            connection.commit()
            return FeedbackResult(
                status=target_status,
                event_id=event_id,
                local_reference=command.local_reference,
            )

        result = _record_decision_for_target(
            connection,
            target=target,
            decision=command.decision,
            reason=command.reason,
            adr_id=command.adr_id,
            actor=actor,
            actor_login=actor_login,
            author_association=author_association,
            source_comment_id=source_comment,
            source_comment_url=source_comment_url,
            classifier_version="",
            classifier_output="",
            hmac_key_version="",
            expires_days=expires_days,
            now=now,
        )
        _set_feedback_outcome(connection, event_id=event_id, outcome="recorded")
        connection.commit()
        return FeedbackResult(
            status="recorded",
            event_id=event_id,
            decision_id=int(result["id"]),
            fingerprint=target.fingerprint,
            local_reference=target.local_reference,
            title=_finding_title(connection, target.fingerprint),
            context_hash=str(result["context_hash"]),
            adr_id=str(result["adr_id"]),
            expires_at=result["expires_at"],
        )
    except Exception:
        connection.rollback()
        raise
