"""Shared typed reader for exported Eneo review-memory snapshots."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast


SUPPORTED_SCHEMA_VERSIONS: Final = {4, 5, 6, 7, 8}


@dataclass(frozen=True)
class DecisionProvenance:
    observation_id: int
    repository: str
    pr_number: int | None
    head_sha: str
    fingerprint: str
    title: str
    path: str
    local_reference: str


def load_export(path: Path) -> Mapping[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("review-memory export must be a JSON object")
    return cast(Mapping[str, object], raw)


def schema_version(state: Mapping[str, object]) -> int:
    value = state.get("schema_version")
    if not isinstance(value, int):
        raise ValueError("review-memory export is missing integer schema_version")
    if value not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(str(item) for item in sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise ValueError(
            f"unsupported review-memory schema_version {value}; supported: {supported}"
        )
    return value


def rows(state: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    value = state.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list in the review-memory export")
    raw_rows = cast(list[object], value)
    output: list[Mapping[str, object]] = []
    for index, item in enumerate(raw_rows):
        if not isinstance(item, Mapping):
            raise ValueError(f"{key}[{index}] must be an object")
        output.append(cast(Mapping[str, object], item))
    return tuple(output)


def decision_provenances(
    state: Mapping[str, object]
) -> dict[int, DecisionProvenance]:
    local_refs: dict[tuple[str, int, str], str] = {}
    for row in rows(state, "pr_finding_references"):
        repository = optional_string(row, "repository")
        pr_number = optional_int(row, "pr_number")
        fingerprint = optional_string(row, "fingerprint")
        if repository and pr_number is not None and fingerprint:
            local_refs[(repository, pr_number, fingerprint)] = optional_string(
                row, "local_reference"
            )

    provenances: dict[int, DecisionProvenance] = {}
    for row in rows(state, "finding_observations"):
        observation_id = optional_int(row, "id")
        if observation_id is None:
            raise ValueError("finding_observations row is missing id")
        repository = optional_string(row, "repository")
        pr_number = optional_int(row, "pr_number")
        fingerprint = required_string(row, "fingerprint")
        local_reference = (
            local_refs.get((repository, pr_number, fingerprint), "")
            if pr_number is not None
            else ""
        )
        provenances[observation_id] = DecisionProvenance(
            observation_id=observation_id,
            repository=repository,
            pr_number=pr_number,
            head_sha=optional_string(row, "head_sha"),
            fingerprint=fingerprint,
            title=optional_string(row, "title"),
            path=optional_string(row, "path"),
            local_reference=local_reference,
        )
    return provenances


def provenance_for_decision(
    row: Mapping[str, object],
    provenances: Mapping[int, DecisionProvenance],
) -> DecisionProvenance | None:
    observation_id = optional_int(row, "observation_id")
    if observation_id is None:
        return None
    provenance = provenances.get(observation_id)
    if provenance is None:
        raise ValueError(
            f"decision observation_id {observation_id} is missing from finding_observations"
        )
    fingerprint = required_string(row, "fingerprint")
    if provenance.fingerprint != fingerprint:
        raise ValueError(
            f"decision fingerprint {fingerprint} does not match observation {observation_id}"
        )
    return provenance


def matches_repository(
    repository: str | None,
    provenance: DecisionProvenance | None,
    row: Mapping[str, object],
) -> bool:
    if repository is None:
        return True
    if provenance is not None:
        return provenance.repository == repository
    row_repository = optional_string(row, "repository")
    return row_repository == repository


def row_id(row: Mapping[str, object]) -> int | None:
    return optional_int(row, "id")


def required_string(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return " ".join(value.strip().split())


def optional_string(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    return " ".join(value.strip().split())


def optional_int(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
