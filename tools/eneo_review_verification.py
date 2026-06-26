"""Private shadow-mode verification bundles for generated PR reviews."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, cast

from eneo_review_private_export import (
    bounded_text,
    dumps_private_json,
    stable_json_hash,
)


VERIFICATION_SCHEMA_VERSION: Final = 1
MAX_SHORT_TEXT: Final = 240
MAX_UNTRUSTED_TEXT: Final = 1000
MAX_PATHS: Final = 10


def build_verification_export(
    source: Mapping[str, object],
    *,
    coverage: Mapping[str, object] | None,
) -> dict[str, object]:
    run = _mapping(source, "run")
    publication = _mapping(source, "publication")
    run_id = _positive_int(_int(run, "id"), "run.id")
    if _text(run, "status") != "generated":
        raise ValueError("review run must be generated before verification export")

    publication_status = _text(publication, "delivery_status")
    if publication_status not in {"generated", "posted"}:
        raise ValueError(
            "review publication must be generated or posted before verification export"
        )

    findings = [
        _finding_payload(item)
        for item in _mapping_list(source, "current_findings")
    ]
    payload: dict[str, object] = {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "source": "review-verification-shadow",
        "source_schema_version": _int(source, "source_schema_version"),
        "repository_untrusted": bounded_text(_text(run, "repository"), MAX_SHORT_TEXT),
        "pr_number": _int(run, "pr_number"),
        "review_run_id": run_id,
        "run_phase": _text(run, "phase"),
        "base_sha": _text(run, "base_sha"),
        "head_sha": _text(run, "head_sha"),
        "started_at": _text(run, "started_at"),
        "completed_at": _text(run, "completed_at"),
        "publication_id": _int(publication, "id"),
        "publication_status": publication_status,
        "review_number": _optional_int(publication, "review_number"),
        "rendered_hash": _text(publication, "rendered_hash"),
        "generated_at": _text(publication, "generated_at"),
        "coverage": _coverage_payload(coverage),
        "findings": findings,
        "verification_mode": {
            "kind": "shadow_non_gating",
        },
        "notes": [
            "All fields ending in _untrusted are bounded untrusted text.",
            "This private artifact is advisory input only; it does not publish comments, suppress findings, change reviewer policy, or gate pull requests.",
            "A missing finding in this artifact is not proof that the pull request is clean.",
        ],
    }
    return {
        "snapshot_id": stable_json_hash(payload),
        "event_set_id": _event_set_id(payload),
        **payload,
    }


def dumps_verification_export(payload: Mapping[str, object]) -> str:
    return dumps_private_json(payload)


def _positive_int(value: int, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    integer = int(value)
    if integer < 1:
        raise ValueError(f"{field} must be a positive integer")
    return integer


def _finding_payload(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "local_reference": _text(row, "local_reference"),
        "observation_id": _int(row, "observation_id"),
        "fingerprint": _text(row, "fingerprint"),
        "rule_id": _text(row, "rule_id"),
        "severity": _text(row, "severity"),
        "category": _text(row, "category"),
        "publication_score": _int(row, "publication_score"),
        "confidence": _float(row, "confidence"),
        "path_untrusted": bounded_text(_text(row, "path"), MAX_SHORT_TEXT),
        "line": _int(row, "line"),
        "symbol_untrusted": bounded_text(_text(row, "symbol"), MAX_SHORT_TEXT),
        "anchor_untrusted": bounded_text(_text(row, "anchor"), MAX_SHORT_TEXT),
        "title_untrusted": bounded_text(_text(row, "title"), MAX_SHORT_TEXT),
        "evidence_untrusted": bounded_text(_text(row, "evidence"), MAX_UNTRUSTED_TEXT),
        "impact_untrusted": bounded_text(_text(row, "impact"), MAX_UNTRUSTED_TEXT),
        "suggested_change_untrusted": bounded_text(
            _text(row, "smallest_fix"), MAX_UNTRUSTED_TEXT
        ),
        "disproof_checks_untrusted": bounded_text(
            _text(row, "disproof_checks"), MAX_UNTRUSTED_TEXT
        ),
        "introduced_by_diff": bool(_int(row, "introduced_by_diff")),
        "context_hash": _text(row, "context_hash"),
    }


def _coverage_payload(summary: Mapping[str, object] | None) -> dict[str, object]:
    if summary is None:
        return {
            "state": "unknown",
            "coverage_hash": "",
            "changed_paths": 0,
            "diff_exposed": 0,
            "context_paths_read": 0,
            "context_ranges_read": 0,
            "changed_files_reported": None,
            "changed_files_registered": 0,
            "changed_file_registration_complete": False,
            "unavailable": 0,
            "diff_truncated": 0,
            "unavailable_paths_untrusted": [],
            "truncated_paths_untrusted": [],
        }
    return {
        "state": str(summary.get("state") or "unknown"),
        "coverage_hash": str(summary.get("coverage_hash") or ""),
        "changed_paths": _mapping_int(summary, "changed_paths"),
        "diff_exposed": _mapping_int(summary, "diff_exposed"),
        "context_paths_read": _mapping_int(summary, "context_paths_read"),
        "context_ranges_read": _mapping_int(summary, "context_ranges_read"),
        "changed_files_reported": _mapping_optional_int(
            summary, "changed_files_reported"
        ),
        "changed_files_registered": _mapping_int(summary, "changed_files_registered"),
        "changed_file_registration_complete": bool(
            summary.get("changed_file_registration_complete")
        ),
        "unavailable": _mapping_int(summary, "unavailable"),
        "diff_truncated": _mapping_int(summary, "diff_truncated"),
        "unavailable_paths_untrusted": _bounded_list(
            summary.get("unavailable_paths")
        ),
        "truncated_paths_untrusted": _bounded_list(summary.get("truncated_paths")),
    }


def _mapping_int(row: Mapping[str, object], key: str) -> int:
    return _as_int(row.get(key))


def _mapping_optional_int(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    return _as_int(value) if value is not None else None


def _mapping(row: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = row.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return cast(Mapping[str, object], value)


def _mapping_list(row: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    value = row.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    items: list[Mapping[str, object]] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, Mapping):
            raise ValueError(f"{key}[{index}] must be an object")
        items.append(cast(Mapping[str, object], item))
    return items


def _bounded_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    items = cast(list[object] | tuple[object, ...], value)
    return [bounded_text(item, MAX_SHORT_TEXT) for item in items[:MAX_PATHS]]


def _event_set_id(payload: Mapping[str, object]) -> str:
    findings = payload.get("findings")
    stable_findings: list[dict[str, object]] = []
    if isinstance(findings, list):
        for item in cast(list[object], findings):
            if not isinstance(item, Mapping):
                continue
            finding = cast(Mapping[str, object], item)
            stable_findings.append(
                {
                    "local_reference": finding.get("local_reference"),
                    "observation_id": finding.get("observation_id"),
                    "fingerprint": finding.get("fingerprint"),
                    "context_hash": finding.get("context_hash"),
                }
            )
    coverage = payload.get("coverage")
    coverage_hash = ""
    if isinstance(coverage, Mapping):
        coverage_hash = str(
            cast(Mapping[str, object], coverage).get("coverage_hash") or ""
        )
    return stable_json_hash(
        {
            "schema_version": payload.get("schema_version"),
            "source": payload.get("source"),
            "repository_untrusted": payload.get("repository_untrusted"),
            "pr_number": payload.get("pr_number"),
            "review_run_id": payload.get("review_run_id"),
            "publication_id": payload.get("publication_id"),
            "base_sha": payload.get("base_sha"),
            "head_sha": payload.get("head_sha"),
            "coverage_hash": coverage_hash,
            "findings": stable_findings,
        }
    )


def _text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value)


def _int(row: Mapping[str, object], key: str) -> int:
    return _as_int(row.get(key))


def _optional_int(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    return _as_int(value) if value is not None else None


def _float(row: Mapping[str, object], key: str) -> float:
    return _as_float(row.get(key))


def _as_int(value: object | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (str, int, float, bytes)):
        return int(value)
    raise ValueError(f"expected integer-compatible value, got {type(value).__name__}")


def _as_float(value: object | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (str, int, float, bytes)):
        return float(value)
    raise ValueError(f"expected float-compatible value, got {type(value).__name__}")
