"""Verifier output and Codex reconciliation state for review runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Literal, cast, get_args

try:
    from .memory_validation import (
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        isoformat,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_validation import (
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        isoformat,
    )


VerificationMode = Literal["shadow", "advise", "gate"]
VerificationStatus = Literal[
    "skipped", "unavailable", "running", "completed", "failed"
]
CandidateVerdict = Literal["confirmed", "refuted", "needs_more_evidence"]
ReconciliationDecision = Literal["publish", "drop"]

VERIFICATION_MODES = frozenset(get_args(VerificationMode))
VERIFICATION_STATUSES = frozenset(get_args(VerificationStatus))
CANDIDATE_VERDICTS = frozenset(get_args(CandidateVerdict))
RECONCILIATION_DECISIONS = frozenset(get_args(ReconciliationDecision))


def _positive_id(value: int, *, field: str) -> int:
    if isinstance(value, bool) or int(value) < 1:
        raise ReviewMemoryError(f"{field} must be a positive integer")
    return int(value)


def _one_of(value: str, *, field: str, allowed: frozenset[str]) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise ReviewMemoryError(
            f"{field} must be one of: {', '.join(sorted(allowed))}"
        )
    return normalized


def _run_row(connection: sqlite3.Connection, review_run_id: int) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM review_runs WHERE id = ?",
        (review_run_id,),
    ).fetchone()
    if row is None:
        raise ReviewMemoryError("review_run_id does not match a recorded review run")
    return dict(row)


def _observation_row(
    connection: sqlite3.Connection, *, review_run_id: int, observation_id: int
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT id, review_run_id, fingerprint
        FROM finding_observations
        WHERE id = ?
        """,
        (observation_id,),
    ).fetchone()
    if row is None:
        raise ReviewMemoryError("observation_id does not match a recorded finding")
    item = dict(row)
    if int(item["review_run_id"] or 0) != review_run_id:
        raise ReviewMemoryError("observation_id belongs to a different review run")
    return item


def _verification_row(
    connection: sqlite3.Connection, verification_run_id: int
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM review_verification_runs WHERE id = ?",
        (verification_run_id,),
    ).fetchone()
    if row is None:
        raise ReviewMemoryError("verification_run_id does not match a verifier run")
    return dict(row)


def _ensure_no_publication(connection: sqlite3.Connection, review_run_id: int) -> None:
    row = connection.execute(
        "SELECT id FROM review_publications WHERE review_run_id = ? LIMIT 1",
        (review_run_id,),
    ).fetchone()
    if row is not None:
        raise ReviewMemoryError(
            "review run already has a publication; reconciliation is immutable"
        )


def record_verification_run(
    connection: sqlite3.Connection,
    *,
    review_run_id: int,
    provider: str = "",
    model: str = "",
    mode: str = "advise",
    status: str = "completed",
    bundle_hash: str = "",
    failure_code: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one external verifier attempt for a review run.

    This table is audit input only. Publication ignores these rows until Codex
    records an explicit candidate reconciliation.
    """
    review_run_id = _positive_id(review_run_id, field="review_run_id")
    _run_row(connection, review_run_id)
    verified_mode = cast(
        VerificationMode,
        _one_of(mode, field="mode", allowed=VERIFICATION_MODES),
    )
    verified_status = cast(
        VerificationStatus,
        _one_of(status, field="status", allowed=VERIFICATION_STATUSES),
    )
    provider = clean_text(provider, field="provider", maximum=80, required=False)
    model = clean_text(model, field="model", maximum=120, required=False)
    bundle_hash = clean_text(
        bundle_hash, field="bundle_hash", maximum=120, required=False
    )
    failure_code = clean_text(
        failure_code, field="failure_code", maximum=120, required=False
    )
    moment = isoformat(now)
    completed_at = None if verified_status == "running" else moment
    cursor = connection.execute(
        """
        INSERT INTO review_verification_runs (
            review_run_id, provider, model, mode, status, bundle_hash,
            failure_code, started_at, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review_run_id,
            provider,
            model,
            verified_mode,
            verified_status,
            bundle_hash,
            failure_code,
            moment,
            completed_at,
        ),
    )
    connection.commit()
    return _verification_row(connection, int(cursor.lastrowid or 0))


def record_candidate_verification(
    connection: sqlite3.Connection,
    *,
    verification_run_id: int,
    observation_id: int,
    verdict: str,
    confidence: float,
    counter_evidence: str = "",
    notes: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a verifier verdict for an existing Codex candidate observation."""
    verification_run_id = _positive_id(
        verification_run_id, field="verification_run_id"
    )
    observation_id = _positive_id(observation_id, field="observation_id")
    verification = _verification_row(connection, verification_run_id)
    review_run_id = int(verification["review_run_id"])
    observation = _observation_row(
        connection,
        review_run_id=review_run_id,
        observation_id=observation_id,
    )
    verified_verdict = cast(
        CandidateVerdict,
        _one_of(verdict, field="verdict", allowed=CANDIDATE_VERDICTS),
    )
    try:
        confidence_value = float(confidence)
    except (TypeError, ValueError) as exc:
        raise ReviewMemoryError("confidence must be a number") from exc
    if confidence_value < 0 or confidence_value > 1:
        raise ReviewMemoryError("confidence must be between 0 and 1")
    cleaned_counter = clean_multiline(
        counter_evidence,
        field="counter_evidence",
        maximum=4000,
        required=False,
    )
    if verified_verdict == "refuted" and not cleaned_counter:
        raise ReviewMemoryError("refuted verdicts require counter_evidence")
    cleaned_notes = clean_multiline(
        notes, field="notes", maximum=2000, required=False
    )
    cursor = connection.execute(
        """
        INSERT INTO candidate_verifications (
            verification_run_id, review_run_id, observation_id, fingerprint,
            verdict, confidence, counter_evidence, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            verification_run_id,
            review_run_id,
            observation_id,
            observation["fingerprint"],
            verified_verdict,
            confidence_value,
            cleaned_counter,
            cleaned_notes,
            isoformat(now),
        ),
    )
    connection.commit()
    row = connection.execute(
        "SELECT * FROM candidate_verifications WHERE id = ?",
        (int(cursor.lastrowid or 0),),
    ).fetchone()
    return dict(row) if row else {}


def record_candidate_reconciliation(
    connection: sqlite3.Connection,
    *,
    review_run_id: int,
    observation_id: int,
    final_decision: str,
    reason: str = "",
    verification_run_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record Codex's final decision for whether a candidate publishes."""
    review_run_id = _positive_id(review_run_id, field="review_run_id")
    observation_id = _positive_id(observation_id, field="observation_id")
    run = _run_row(connection, review_run_id)
    if str(run["status"]) != "running":
        raise ReviewMemoryError("review_run_id is not an active review run")
    _ensure_no_publication(connection, review_run_id)
    observation = _observation_row(
        connection,
        review_run_id=review_run_id,
        observation_id=observation_id,
    )
    decision = cast(
        ReconciliationDecision,
        _one_of(
            final_decision,
            field="final_decision",
            allowed=RECONCILIATION_DECISIONS,
        ),
    )
    cleaned_reason = clean_multiline(
        reason,
        field="reason",
        maximum=4000,
        required=decision == "drop",
    )
    verification_id: int | None = None
    if verification_run_id is not None:
        verification_id = _positive_id(
            verification_run_id, field="verification_run_id"
        )
        verification = _verification_row(connection, verification_id)
        if int(verification["review_run_id"]) != review_run_id:
            raise ReviewMemoryError(
                "verification_run_id belongs to a different review run"
            )
    connection.execute(
        """
        INSERT INTO candidate_reconciliations (
            review_run_id, observation_id, fingerprint, final_decision,
            reason, verification_run_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(review_run_id, fingerprint) DO UPDATE SET
            observation_id = excluded.observation_id,
            final_decision = excluded.final_decision,
            reason = excluded.reason,
            verification_run_id = excluded.verification_run_id,
            created_at = excluded.created_at
        """,
        (
            review_run_id,
            observation_id,
            observation["fingerprint"],
            decision,
            cleaned_reason,
            verification_id,
            isoformat(now),
        ),
    )
    connection.commit()
    row = connection.execute(
        """
        SELECT *
        FROM candidate_reconciliations
        WHERE review_run_id = ? AND fingerprint = ?
        """,
        (review_run_id, observation["fingerprint"]),
    ).fetchone()
    return dict(row) if row else {}


def candidate_reconciliations_for_run(
    connection: sqlite3.Connection, review_run_id: int
) -> dict[str, dict[str, Any]]:
    review_run_id = _positive_id(review_run_id, field="review_run_id")
    rows = connection.execute(
        """
        SELECT *
        FROM candidate_reconciliations
        WHERE review_run_id = ?
        """,
        (review_run_id,),
    ).fetchall()
    return {str(row["fingerprint"]): dict(row) for row in rows}


def latest_verification_status_by_run(
    connection: sqlite3.Connection, review_run_ids: list[int]
) -> dict[int, dict[str, Any]]:
    ids = sorted({_positive_id(run_id, field="review_run_id") for run_id in review_run_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT *
        FROM review_verification_runs
        WHERE id IN (
            SELECT MAX(id)
            FROM review_verification_runs
            WHERE review_run_id IN ({placeholders})
            GROUP BY review_run_id
        )
        """,
        ids,
    ).fetchall()
    return {int(row["review_run_id"]): dict(row) for row in rows}
