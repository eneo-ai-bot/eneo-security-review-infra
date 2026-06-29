"""Bounded untrusted JSON exports for the private Eneo reviewer coach workflow."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, cast

from eneo_review_export import optional_int, optional_string, rows, schema_version
from eneo_review_learning import LearningSignal, build_learning_report
from eneo_review_private_export import (
    bounded_text,
    dumps_private_json,
    stable_json_hash,
)


COACH_SCHEMA_VERSION: Final = 1
DECISION_CANDIDATE_GROUP: Final = "decision_candidate"
REVIEW_QUALITY_SIGNAL_GROUP: Final = "review_quality_signal"
POSITIVE_PATTERN_GROUP: Final = "positive_pattern"
COACH_EVENT_GROUPS: Final[frozenset[str]] = frozenset(
    {
        DECISION_CANDIDATE_GROUP,
        REVIEW_QUALITY_SIGNAL_GROUP,
        POSITIVE_PATTERN_GROUP,
    }
)
MAX_UNTRUSTED_TEXT: Final = 1000
MAX_SHORT_TEXT: Final = 240
MAX_DECISION_CHAIN: Final = 20


def build_coach_export(
    state: Mapping[str, object],
    *,
    repository: str | None = None,
    after_decision_id: int = 0,
    after_feedback_id: int = 0,
    include_incomplete: bool = False,
) -> dict[str, object]:
    source_schema_version = schema_version(state)
    report = build_learning_report(state, repository=repository)
    events: list[dict[str, object]] = []
    for group, signals in [
        (DECISION_CANDIDATE_GROUP, report.decision_candidates),
        (REVIEW_QUALITY_SIGNAL_GROUP, report.quality_signals),
        (POSITIVE_PATTERN_GROUP, report.positive_patterns),
    ]:
        for signal in signals:
            if not include_incomplete and not signal.promotion_eligible:
                continue
            if signal.source == "decision":
                decision_id = _source_numeric_id(signal)
                # Coach event ids must be stable across runs; real DB exports have
                # integer ids, while hand-built incomplete rows may not.
                if decision_id is None or decision_id <= after_decision_id:
                    continue
            elif signal.source == "review_quality_feedback":
                feedback_id = _source_numeric_id(signal)
                if feedback_id is None or feedback_id <= after_feedback_id:
                    continue
            events.append(_signal_event(group, signal))

    cursor = {
        "after_decision_id": max(0, int(after_decision_id)),
        "after_feedback_id": max(0, int(after_feedback_id)),
        "max_decision_id": _max_row_id(state, "decisions"),
        "max_feedback_id": _max_row_id(state, "review_quality_feedback"),
    }
    payload: dict[str, object] = {
        "schema_version": COACH_SCHEMA_VERSION,
        "source_schema_version": source_schema_version,
        "repository_untrusted": bounded_text(repository or "", MAX_SHORT_TEXT),
        "source_exported_at": optional_string(state, "exported_at"),
        "cursor": cursor,
        "events": events,
        "notes": [
            "All fields ending in _untrusted are bounded untrusted text.",
            "This export is advisory input for a private coach only; it is not reviewer policy.",
        ],
    }
    event_set_id = _event_set_id(payload)
    snapshot_id = stable_json_hash(payload)
    return {"snapshot_id": snapshot_id, "event_set_id": event_set_id, **payload}


def dumps_coach_export(payload: Mapping[str, object]) -> str:
    return dumps_private_json(payload)


def _signal_event(group: str, signal: LearningSignal) -> dict[str, object]:
    event: dict[str, object] = {
        "event_id": signal.source_id,
        "event_group": group,
        "event_type": signal.source_value,
        "signal_strength": signal.signal_strength,
        "suggested_route": signal.suggested_route,
        "promotion_eligible": signal.promotion_eligible,
        "missing_evidence": list(signal.missing_evidence),
        "human_reason_untrusted": bounded_text(signal.reason, MAX_UNTRUSTED_TEXT),
        "reviewer_title_untrusted": bounded_text(signal.title, MAX_SHORT_TEXT),
        "next_step_untrusted": bounded_text(signal.next_step, MAX_UNTRUSTED_TEXT),
        "related_event_ids": list(signal.related_event_ids),
        "related_event_ids_total": len(signal.related_event_ids),
    }
    if signal.decision_chain:
        event["decision_chain_total"] = len(signal.decision_chain)
        event["decision_chain"] = list(signal.decision_chain[-MAX_DECISION_CHAIN:])
    if len(signal.related_event_ids) > MAX_DECISION_CHAIN:
        event["related_event_ids"] = list(signal.related_event_ids[-MAX_DECISION_CHAIN:])
    if signal.provenance is not None:
        event["source"] = {
            "repository_untrusted": bounded_text(
                signal.provenance.repository, MAX_SHORT_TEXT
            ),
            "pr_number": signal.provenance.pr_number,
            "head_sha": signal.provenance.head_sha,
            "fingerprint": signal.provenance.fingerprint,
            "observation_id": signal.provenance.observation_id,
            "local_reference": signal.provenance.local_reference,
            "path_untrusted": bounded_text(signal.provenance.path, MAX_SHORT_TEXT),
        }
    else:
        event["source"] = {
            "repository_untrusted": bounded_text(signal.repository, MAX_SHORT_TEXT),
            "pr_number": signal.pr_number,
            "local_reference": signal.local_reference,
            "fingerprint": signal.fingerprint,
        }
    return event


def _source_numeric_id(signal: LearningSignal) -> int | None:
    _, separator, raw_id = signal.source_id.partition(":")
    if not separator or not raw_id.isdigit():
        return None
    return int(raw_id)


def _max_row_id(state: Mapping[str, object], key: str) -> int:
    value = 0
    for row in rows(state, key):
        item_id = optional_int(row, "id")
        if item_id is not None:
            value = max(value, item_id)
    return value


def _event_set_id(payload: Mapping[str, object]) -> str:
    cursor = payload.get("cursor")
    cursor_identity: object = None
    if isinstance(cursor, Mapping):
        cursor_map = cast(Mapping[str, object], cursor)
        cursor_identity = {
            "after_decision_id": cursor_map.get("after_decision_id"),
            "after_feedback_id": cursor_map.get("after_feedback_id"),
        }
    stable_payload = {
        "schema_version": payload.get("schema_version"),
        "source_schema_version": payload.get("source_schema_version"),
        "repository_untrusted": payload.get("repository_untrusted"),
        "cursor": cursor_identity,
        "events": payload.get("events"),
    }
    return stable_json_hash(stable_payload)
