"""Human decisions and active suppression evaluation."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Iterable

try:
    from .memory_identity import resolve_fingerprint
    from .memory_validation import (
        DECISIONS,
        SUPPRESSIVE_DECISIONS,
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        isoformat,
        normalize_repository,
        parse_time,
        utc_now,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_identity import resolve_fingerprint
    from memory_validation import (
        DECISIONS,
        SUPPRESSIVE_DECISIONS,
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        isoformat,
        normalize_repository,
        parse_time,
        utc_now,
    )


def latest_decision(
    connection: sqlite3.Connection, fingerprint: str
) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT id, fingerprint, decision, reason, actor, context_hash,
               observation_id, adr_id, created_at, expires_at
        FROM decisions
        WHERE fingerprint = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()
    return dict(row) if row else None


def latest_decisions_for_fingerprints(
    connection: sqlite3.Connection, fingerprints: Iterable[str]
) -> dict[str, dict[str, Any]]:
    unique = sorted({str(value) for value in fingerprints if value})
    if not unique:
        return {}

    output: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(unique), 500):
        chunk = unique[offset : offset + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"""
            SELECT d.id, d.fingerprint, d.decision, d.reason, d.actor,
                   d.context_hash, d.observation_id, d.adr_id,
                   d.created_at, d.expires_at
            FROM decisions d
            JOIN (
                SELECT fingerprint, MAX(id) AS id
                FROM decisions
                WHERE fingerprint IN ({placeholders})
                GROUP BY fingerprint
            ) latest ON latest.id = d.id
            """,
            chunk,
        ).fetchall()
        for row in rows:
            item = dict(row)
            output[item["fingerprint"]] = item
    return output


def current_context_hash_for_fingerprint(
    connection: sqlite3.Connection, fingerprint: str
) -> str:
    row = connection.execute(
        "SELECT context_hash FROM findings WHERE fingerprint = ?", (fingerprint,)
    ).fetchone()
    return str(row["context_hash"] or "") if row else ""


def active_suppression_from_decision(
    decision: dict[str, Any] | None,
    *,
    context_hash: str | None,
    now: datetime,
) -> dict[str, Any] | None:
    if not decision or decision["decision"] not in SUPPRESSIVE_DECISIONS:
        return None
    expires = parse_time(decision.get("expires_at"))
    if expires is not None and expires <= now:
        return None

    current_hash = context_hash or ""
    decision_hash = str(decision.get("context_hash") or "")
    # A suppression is deliberately narrow: it applies only to the exact file
    # version that a human reviewed. Any later file change forces re-validation.
    if not current_hash or not decision_hash or current_hash != decision_hash:
        return None
    return decision


def active_suppression(
    connection: sqlite3.Connection,
    fingerprint: str,
    *,
    context_hash: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    decision = latest_decision(connection, fingerprint)
    if not decision or decision["decision"] not in SUPPRESSIVE_DECISIONS:
        return None

    current_hash = context_hash or current_context_hash_for_fingerprint(
        connection, fingerprint
    )
    return active_suppression_from_decision(
        decision, context_hash=current_hash, now=now or utc_now()
    )


def latest_observation_id_for_fingerprint(
    connection: sqlite3.Connection, fingerprint: str
) -> int | None:
    row = connection.execute(
        """
        SELECT id
        FROM finding_observations
        WHERE fingerprint = ?
        ORDER BY observed_at DESC, id DESC
        LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()
    return int(row["id"]) if row else None


def _observation_for_id(
    connection: sqlite3.Connection, observation_id: int
) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM finding_observations WHERE id = ?",
        (int(observation_id),),
    ).fetchone()
    if not row:
        raise ReviewMemoryError("decision observation_id does not exist")
    return dict(row)


def _latest_observation_for_fingerprint(
    connection: sqlite3.Connection, fingerprint: str
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT *
        FROM finding_observations
        WHERE fingerprint = ?
        ORDER BY observed_at DESC, id DESC
        LIMIT 1
        """,
        (fingerprint,),
    ).fetchone()
    if not row:
        raise ReviewMemoryError(
            "finding has no recorded observation; re-run the review before deciding it"
        )
    return dict(row)


def _latest_observation_for_local_reference(
    connection: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
    local_reference: str,
) -> dict[str, Any]:
    repository = normalize_repository(repository)
    reference = clean_text(
        local_reference, field="local_reference", maximum=12
    ).upper()
    row = connection.execute(
        """
        SELECT fo.*
        FROM pr_finding_references refs
        JOIN finding_observations fo
          ON fo.repository = refs.repository
         AND fo.pr_number = refs.pr_number
         AND fo.fingerprint = refs.fingerprint
        WHERE refs.repository = ?
          AND refs.pr_number = ?
          AND refs.local_reference = ?
        ORDER BY fo.observed_at DESC, fo.id DESC
        LIMIT 1
        """,
        (repository, int(pr_number), reference),
    ).fetchone()
    if not row:
        raise ReviewMemoryError("unknown local finding reference for this pull request")
    return dict(row)


def observation_id_for_context(
    connection: sqlite3.Connection,
    *,
    repository: str,
    pr_number: int,
    fingerprint: str,
    head_sha: str = "",
    context_hash: str = "",
) -> int | None:
    repository = normalize_repository(repository)
    params: list[Any] = [repository, int(pr_number), fingerprint]
    clauses = [
        "repository = ?",
        "pr_number = ?",
        "fingerprint = ?",
    ]
    if head_sha:
        clauses.append("head_sha = ?")
        params.append(head_sha)
    if context_hash:
        clauses.append("context_hash = ?")
        params.append(context_hash)
    # Empty head/hash is allowed for non-suppressive legacy feedback; the lookup
    # then degrades only within the same repository, PR, and fingerprint.
    row = connection.execute(
        f"""
        SELECT id
        FROM finding_observations
        WHERE {" AND ".join(clauses)}
        ORDER BY observed_at DESC, id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    return int(row["id"]) if row else None


def insert_decision(
    connection: sqlite3.Connection,
    *,
    fingerprint: str,
    decision: str,
    reason: str,
    actor: str,
    context_hash: str,
    observation_id: int | None = None,
    adr_id: str = "",
    expires_days: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if decision in SUPPRESSIVE_DECISIONS and not context_hash:
        raise ReviewMemoryError(
            "finding has no trusted file hash; re-run the review before suppressing"
        )
    adr_id = clean_text(adr_id, field="adr_id", maximum=80, required=False)
    if decision == "intentional_by_design" and not adr_id:
        raise ReviewMemoryError("intentional_by_design requires an ADR id")

    expires_at: str | None = None
    moment = now or utc_now()
    if decision in SUPPRESSIVE_DECISIONS:
        days = 180 if expires_days is None else int(expires_days)
        if days < 1 or days > 3650:
            raise ReviewMemoryError("expires_days must be between 1 and 3650")
        expires_at = isoformat(moment + timedelta(days=days))
    elif expires_days is not None:
        raise ReviewMemoryError("expires_days only applies to suppressive decisions")

    if observation_id is not None:
        observation = connection.execute(
            "SELECT fingerprint FROM finding_observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
        if not observation:
            raise ReviewMemoryError("decision observation_id does not exist")
        if str(observation["fingerprint"]) != fingerprint:
            raise ReviewMemoryError(
                "decision observation_id belongs to a different finding"
            )

    created_at = isoformat(moment)
    cursor = connection.execute(
        """
        INSERT INTO decisions (
            fingerprint, decision, reason, actor, context_hash, observation_id,
            adr_id, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fingerprint,
            decision,
            reason,
            actor,
            context_hash,
            observation_id,
            adr_id,
            created_at,
            expires_at,
        ),
    )
    return {
        "id": cursor.lastrowid,
        "fingerprint": fingerprint,
        "decision": decision,
        "reason": reason,
        "actor": actor,
        "context_hash": context_hash,
        "observation_id": observation_id,
        "adr_id": adr_id,
        "created_at": created_at,
        "expires_at": expires_at,
    }


def add_decision(
    connection: sqlite3.Connection,
    fingerprint: str,
    decision: str,
    reason: str,
    actor: str,
    *,
    expires_days: int | None = None,
    adr_id: str = "",
    observation_id: int | None = None,
    repository: str | None = None,
    pr_number: int | None = None,
    local_reference: str = "",
    latest: bool = False,
) -> dict[str, Any]:
    raw_fingerprint = fingerprint.strip()
    decision = decision.strip().lower()
    if decision not in DECISIONS:
        raise ReviewMemoryError(
            f"decision must be one of: {', '.join(sorted(DECISIONS))}"
        )
    reason = clean_multiline(reason, field="reason", maximum=2000)
    actor = clean_text(actor, field="actor", maximum=200)

    target_count = sum(
        [
            observation_id is not None,
            bool(local_reference),
            bool(latest),
        ]
    )
    if target_count != 1:
        raise ReviewMemoryError(
            "decide requires exactly one target: observation_id, local_reference, or latest"
        )

    if observation_id is not None:
        observation = _observation_for_id(connection, observation_id)
    elif local_reference:
        if repository is None or pr_number is None:
            raise ReviewMemoryError(
                "local_reference decisions require repository and pr_number"
            )
        observation = _latest_observation_for_local_reference(
            connection,
            repository=repository,
            pr_number=pr_number,
            local_reference=local_reference,
        )
    else:
        if not raw_fingerprint:
            raise ReviewMemoryError("latest decisions require a fingerprint")
        observation = _latest_observation_for_fingerprint(
            connection, resolve_fingerprint(connection, raw_fingerprint)
        )

    fingerprint = str(observation["fingerprint"])
    if raw_fingerprint:
        resolved = resolve_fingerprint(connection, raw_fingerprint)
        if resolved != fingerprint:
            raise ReviewMemoryError(
                "decision target observation belongs to a different finding"
            )
    context_hash = str(observation["context_hash"] or "")

    with connection:
        return insert_decision(
            connection,
            fingerprint=fingerprint,
            decision=decision,
            reason=reason,
            actor=actor,
            context_hash=context_hash,
            observation_id=int(observation["id"]),
            adr_id=adr_id,
            expires_days=expires_days,
        )
