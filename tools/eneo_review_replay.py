"""Typed structural validation for Eneo reviewer replay fixtures."""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast


REPLAY_SCHEMA_VERSION: Final = 1
SEVERITIES: Final = ("Critical", "High", "Medium", "Low")
SEVERITY_RANK: Final = {value: index for index, value in enumerate(SEVERITIES)}
SHA_RE: Final = re.compile(r"^[0-9a-f]{40,64}$")
REPOSITORY_RE: Final = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
IDENTIFIER_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")


@dataclass(frozen=True)
class ReplayValidationResult:
    path: str
    fixture_id: str


def validate_replay_path(path: Path) -> tuple[ReplayValidationResult, ...]:
    paths = _fixture_paths(path)
    seen_ids: set[str] = set()
    results: list[ReplayValidationResult] = []
    for fixture_path in paths:
        fixture = _load_fixture(fixture_path)
        fixture_id = _validate_fixture(fixture, fixture_path)
        if fixture_id in seen_ids:
            raise ValueError(f"duplicate replay fixture id: {fixture_id}")
        seen_ids.add(fixture_id)
        results.append(
            ReplayValidationResult(path=str(fixture_path), fixture_id=fixture_id)
        )
    return tuple(results)


def _fixture_paths(path: Path) -> tuple[Path, ...]:
    if path.is_dir():
        return tuple(sorted(path.glob("*.yaml")))
    return (path,)


def _load_fixture(path: Path) -> Mapping[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path} must be JSON-compatible YAML; {exc.msg} at line {exc.lineno}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ValueError(f"{path} must contain a JSON object")
    return cast(Mapping[str, object], raw)


def _validate_fixture(fixture: Mapping[str, object], path: Path) -> str:
    _expect_keys(
        fixture,
        {
            "schema_version",
            "id",
            "source",
            "model_expectations",
            "deterministic_invariants",
            "advisory_notes",
        },
        {
            "schema_version",
            "id",
            "source",
            "model_expectations",
            "deterministic_invariants",
        },
        "fixture",
    )
    if fixture.get("schema_version") != REPLAY_SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version must be {REPLAY_SCHEMA_VERSION}")
    fixture_id = _required_identifier(fixture, "id", "fixture")
    _validate_source(_mapping(fixture.get("source"), "source"))
    _validate_model_expectations(
        _mapping(fixture.get("model_expectations"), "model_expectations")
    )
    _validate_deterministic_invariants(
        _sequence(
            fixture.get("deterministic_invariants"),
            "deterministic_invariants",
        )
    )
    if "advisory_notes" in fixture:
        for note in _sequence(fixture.get("advisory_notes"), "advisory_notes"):
            _string(note, "advisory_notes[]")
    return fixture_id


def _validate_source(source: Mapping[str, object]) -> None:
    _expect_keys(
        source,
        {"repository", "pull_request", "base_sha", "head_sha", "trust"},
        {"repository", "pull_request", "base_sha", "head_sha", "trust"},
        "source",
    )
    repository = _string(source.get("repository"), "source.repository")
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError("source.repository must be owner/repository")
    pull_request = source.get("pull_request")
    if not isinstance(pull_request, int) or pull_request < 1:
        raise ValueError("source.pull_request must be a positive integer")
    for key in ("base_sha", "head_sha"):
        value = _string(source.get(key), f"source.{key}")
        if not SHA_RE.fullmatch(value):
            raise ValueError(f"source.{key} must be a 40-64 character hex SHA")
    if source.get("trust") != "human_confirmed":
        raise ValueError("source.trust must be human_confirmed")


def _validate_model_expectations(expectations: Mapping[str, object]) -> None:
    _expect_keys(
        expectations,
        {"mode", "required_root_causes", "forbidden_root_causes"},
        {"mode", "required_root_causes"},
        "model_expectations",
    )
    if expectations.get("mode") != "advisory":
        raise ValueError("model_expectations.mode must be advisory")
    for index, item in enumerate(
        _sequence(expectations.get("required_root_causes"), "required_root_causes")
    ):
        _validate_root_cause(_mapping(item, f"required_root_causes[{index}]"))
    for index, item in enumerate(
        _sequence(
            expectations.get("forbidden_root_causes", []),
            "forbidden_root_causes",
        )
    ):
        _validate_root_cause(_mapping(item, f"forbidden_root_causes[{index}]"))


def _validate_root_cause(item: Mapping[str, object]) -> None:
    _expect_keys(
        item,
        {"rule_id", "severity", "semantic_claim"},
        {"rule_id", "severity", "semantic_claim"},
        "root_cause",
    )
    _required_identifier(item, "rule_id", "root_cause")
    _string(item.get("semantic_claim"), "root_cause.semantic_claim")
    severity = _mapping(item.get("severity"), "root_cause.severity")
    _expect_keys(
        severity,
        {"minimum", "maximum"},
        {"minimum", "maximum"},
        "root_cause.severity",
    )
    minimum = _severity(severity.get("minimum"), "severity.minimum")
    maximum = _severity(severity.get("maximum"), "severity.maximum")
    if SEVERITY_RANK[minimum] > SEVERITY_RANK[maximum]:
        raise ValueError("severity.minimum cannot be lower priority than maximum")


def _validate_deterministic_invariants(items: Sequence[object]) -> None:
    seen_ids: set[str] = set()
    for index, item in enumerate(items):
        invariant = _mapping(item, f"deterministic_invariants[{index}]")
        _expect_keys(
            invariant,
            {"id", "covered_by"},
            {"id", "covered_by"},
            "deterministic_invariant",
        )
        invariant_id = _required_identifier(invariant, "id", "deterministic_invariant")
        if invariant_id in seen_ids:
            raise ValueError(f"duplicate deterministic invariant id: {invariant_id}")
        seen_ids.add(invariant_id)
        covered_by = _sequence(invariant.get("covered_by"), "covered_by")
        if not covered_by:
            raise ValueError("deterministic invariant covered_by cannot be empty")
        for reference in covered_by:
            _validate_test_reference(_string(reference, "covered_by[]"))


def _validate_test_reference(reference: str) -> None:
    parts = reference.split(".")
    for split_at in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split_at])
        try:
            target: object = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        for attribute in parts[split_at:]:
            if not hasattr(target, attribute):
                raise ValueError(f"test reference does not exist: {reference}")
            target = getattr(target, attribute)
        if not parts[-1].startswith("test_"):
            raise ValueError(f"test reference must end in a test method: {reference}")
        return
    raise ValueError(f"test reference module cannot be imported: {reference}")


def _expect_keys(
    value: Mapping[str, object],
    allowed: set[str],
    required: set[str],
    context: str,
) -> None:
    keys = set(value)
    extra = keys - allowed
    missing = required - keys
    if extra:
        raise ValueError(f"{context} has unknown keys: {', '.join(sorted(extra))}")
    if missing:
        raise ValueError(f"{context} is missing keys: {', '.join(sorted(missing))}")


def _mapping(value: object, context: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be an object")
    return cast(Mapping[str, object], value)


def _sequence(value: object, context: str) -> Sequence[object]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{context} must be a list")
    return cast(Sequence[object], value)


def _string(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context} must be a non-empty string")
    return " ".join(value.strip().split())


def _required_identifier(
    value: Mapping[str, object],
    key: str,
    context: str,
) -> str:
    identifier = _string(value.get(key), f"{context}.{key}")
    if not IDENTIFIER_RE.fullmatch(identifier):
        raise ValueError(f"{context}.{key} must be a lowercase identifier")
    return identifier


def _severity(value: object, context: str) -> str:
    severity = _string(value, context)
    if severity not in SEVERITY_RANK:
        raise ValueError(f"{context} must be one of: {', '.join(SEVERITIES)}")
    return severity
