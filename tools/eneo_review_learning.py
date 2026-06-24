"""Offline learning-candidate reports for the Eneo PR reviewer.

The public webhook reviewer must not import this module. It is operator tooling
that reads an exported review-memory JSON snapshot and turns explicit human
signals into a report for a private review-coach workflow.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast


SUPPORTED_SCHEMA_VERSIONS: Final = {4, 5}


@dataclass(frozen=True)
class DecisionProvenance:
    observation_id: int | None
    repository: str
    pr_number: int | None
    head_sha: str
    fingerprint: str
    title: str
    path: str
    local_reference: str


@dataclass(frozen=True)
class LearningSignal:
    source: str
    source_value: str
    signal_strength: str
    suggested_route: str
    title: str
    reason: str
    next_step: str
    promotion_eligible: bool
    missing_evidence: tuple[str, ...]
    repository: str
    pr_number: int | None
    fingerprint: str
    local_reference: str
    provenance: DecisionProvenance | None


@dataclass(frozen=True)
class LearningReport:
    schema_version: int
    repository: str | None
    decision_candidates: tuple[LearningSignal, ...]
    quality_signals: tuple[LearningSignal, ...]
    positive_patterns: tuple[LearningSignal, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class SignalPolicy:
    signal_strength: str
    suggested_route: str
    next_step: str


DECISION_POLICIES: Final[dict[str, SignalPolicy]] = {
    "false_positive": SignalPolicy(
        "strong",
        "judgment_or_procedure",
        "Add or extend a false-positive replay case, then tighten the evidence "
        "rule only if the same reasoning failure recurs.",
    ),
    "intentional_by_design": SignalPolicy(
        "strong",
        "architecture_context",
        "Check that the ADR or accepted design context is canonical, then add a "
        "narrow ADR/replay fixture instead of a broad suppression rule.",
    ),
    "accepted_risk": SignalPolicy(
        "strong",
        "exact_decision",
        "Keep this as scoped governance unless repeated accepted risks show a "
        "missing reviewer policy or migration checklist.",
    ),
    "duplicate": SignalPolicy(
        "medium",
        "root_cause_deduplication",
        "Use this to improve grouping only when repeated duplicates share the "
        "same root-cause split.",
    ),
    "reopen": SignalPolicy(
        "strong",
        "stability_regression",
        "Create a replay case showing why the prior suppression no longer held, "
        "then adjust policy only after human review.",
    ),
}


QUALITY_POLICIES: Final[dict[str, SignalPolicy]] = {
    "missed_issue": SignalPolicy(
        "strong",
        "procedure_or_mechanical_gap",
        "Add a replay case for the missed issue before changing prompts, tools, "
        "or retrieval.",
    ),
    "severity_too_high": SignalPolicy(
        "strong",
        "severity_calibration",
        "Add a labelled severity example and update calibration only if the "
        "current rubric mis-scores it.",
    ),
    "severity_too_low": SignalPolicy(
        "strong",
        "severity_calibration",
        "Add a labelled severity example and update calibration only if the "
        "current rubric mis-scores it.",
    ),
    "too_speculative": SignalPolicy(
        "strong",
        "evidence_gate_calibration",
        "Add a negative replay case and tighten the falsification gate if the "
        "finding still survives.",
    ),
    "remediation_impractical": SignalPolicy(
        "medium",
        "remediation_quality",
        "Capture the safer fix pattern and update remediation guidance only "
        "after a replay proves the old suggestion would recur.",
    ),
    "unclear": SignalPolicy(
        "medium",
        "developer_experience",
        "Improve renderer or wording only if the same readability problem "
        "appears across multiple reviews.",
    ),
    "too_verbose": SignalPolicy(
        "medium",
        "developer_experience",
        "Prefer renderer/comment-contract changes over adding more prompt prose.",
    ),
}


POSITIVE_DECISIONS: Final = {"resolved"}
POSITIVE_FEEDBACK: Final = {"useful"}


def load_export(path: Path) -> Mapping[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("review-memory export must be a JSON object")
    return cast(Mapping[str, object], raw)


def build_learning_report(
    state: Mapping[str, object], *, repository: str | None = None
) -> LearningReport:
    schema_version = _schema_version(state)
    provenance_by_observation_id = _decision_provenances(state)
    decision_candidates: list[LearningSignal] = []
    quality_signals: list[LearningSignal] = []
    positive_patterns: list[LearningSignal] = []
    unclassified_decisions: set[str] = set()
    unclassified_feedback: set[str] = set()
    notes: list[str] = [
        "Weak signals such as silence, merge-without-fix, thumbs-up, or a later "
        "code change are not treated as learning candidates.",
    ]

    for row in _rows(state, "decisions"):
        decision = _required_string(row, "decision")
        provenance = _provenance_for_decision(row, provenance_by_observation_id)
        if not _matches_repository(repository, provenance, row):
            continue
        if decision in POSITIVE_DECISIONS:
            positive_patterns.append(
                _decision_signal(
                    row,
                    provenance,
                    SignalPolicy(
                        "medium",
                        "positive_pattern",
                        "Treat as a fixed-finding example only when the root "
                        "cause is independently confirmed by a regression test.",
                    ),
                )
            )
        elif decision in DECISION_POLICIES:
            decision_candidates.append(
                _decision_signal(row, provenance, DECISION_POLICIES[decision])
            )
        else:
            unclassified_decisions.add(decision)

    for row in _rows(state, "review_quality_feedback"):
        category = _required_string(row, "category")
        if repository is not None and _required_string(row, "repository") != repository:
            continue
        if category in POSITIVE_FEEDBACK:
            positive_patterns.append(
                _quality_signal(
                    row,
                    SignalPolicy(
                        "medium",
                        "positive_pattern",
                        "Protect the behavior only if a later change risks "
                        "regressing the useful output shape.",
                    ),
                )
            )
        elif category in QUALITY_POLICIES:
            quality_signals.append(_quality_signal(row, QUALITY_POLICIES[category]))
        else:
            unclassified_feedback.add(category)

    if unclassified_decisions:
        values = ", ".join(f"`{value}`" for value in sorted(unclassified_decisions))
        notes.append(f"Unclassified decision values were present: {values}.")
    if unclassified_feedback:
        values = ", ".join(f"`{value}`" for value in sorted(unclassified_feedback))
        notes.append(f"Unclassified review-quality feedback values were present: {values}.")

    if not quality_signals and not unclassified_feedback:
        notes.append(
            "No review-quality feedback signals were present. In the current "
            "bundle this is expected until a public feedback writer is added."
        )
    elif not quality_signals:
        notes.append(
            "Review-quality feedback rows were present, but none matched a known "
            "learning signal category."
        )

    return LearningReport(
        schema_version=schema_version,
        repository=repository,
        decision_candidates=tuple(decision_candidates),
        quality_signals=tuple(quality_signals),
        positive_patterns=tuple(positive_patterns),
        notes=tuple(notes),
    )


def render_markdown(report: LearningReport) -> str:
    lines = [
        "# Eneo reviewer learning candidates",
        "",
        "Generated from an exported review-memory snapshot. This report is "
        "advisory; the public webhook reviewer must not read it as policy.",
        "",
        f"- Schema version: {report.schema_version}",
        f"- Repository: {report.repository or '(all repositories)'}",
        f"- Decision candidates: {len(report.decision_candidates)}",
        f"- Review-quality signals: {len(report.quality_signals)}",
        f"- Positive patterns: {len(report.positive_patterns)}",
        "",
        "## Decision candidates",
        "",
    ]
    if report.decision_candidates:
        lines.extend(_render_signals("D", report.decision_candidates))
    else:
        lines.append(
            "No decision-derived learning candidates were found. Do not create "
            "policy from silence or absence of decisions."
        )
        lines.append("")

    lines.extend(["## Review-quality signals", ""])
    if report.quality_signals:
        lines.extend(_render_signals("Q", report.quality_signals))
    else:
        lines.append(
            "No review-quality signals were found in the export. This is normal "
            "until feedback ingestion writes this table."
        )
        lines.append("")

    lines.extend(["## Positive patterns", ""])
    if report.positive_patterns:
        lines.extend(_render_signals("P", report.positive_patterns))
    else:
        lines.append("No positive patterns were found.")
        lines.append("")

    lines.extend(["## Notes", ""])
    for note in report.notes:
        lines.append(f"- {note}")
    lines.append(
        "- Before committing or sharing a generated report, remove sensitive "
        "human-entered reasons, private URLs, and customer-specific detail."
    )
    lines.append(
        "- A candidate becomes reviewer policy only through a normal human-reviewed "
        "change to AGENTS.md, the review skill, an ADR, plugin code, or a replay case."
    )
    lines.append("")
    return "\n".join(lines)


def _schema_version(state: Mapping[str, object]) -> int:
    value = state.get("schema_version")
    if not isinstance(value, int):
        raise ValueError("review-memory export is missing integer schema_version")
    if value not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(str(item) for item in sorted(SUPPORTED_SCHEMA_VERSIONS))
        raise ValueError(
            f"unsupported review-memory schema_version {value}; supported: {supported}"
        )
    return value


def _rows(state: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    value = state.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list in the review-memory export")
    raw_rows = cast(list[object], value)
    rows: list[Mapping[str, object]] = []
    for index, item in enumerate(raw_rows):
        if not isinstance(item, Mapping):
            raise ValueError(f"{key}[{index}] must be an object")
        rows.append(cast(Mapping[str, object], item))
    return tuple(rows)

def _decision_provenances(
    state: Mapping[str, object]
) -> dict[int, DecisionProvenance]:
    local_refs: dict[tuple[str, int, str], str] = {}
    for row in _rows(state, "pr_finding_references"):
        repository = _optional_string(row, "repository")
        pr_number = _optional_int(row, "pr_number")
        fingerprint = _optional_string(row, "fingerprint")
        if repository and pr_number is not None and fingerprint:
            local_refs[(repository, pr_number, fingerprint)] = _optional_string(
                row, "local_reference"
            )

    provenances: dict[int, DecisionProvenance] = {}
    for row in _rows(state, "finding_observations"):
        observation_id = _optional_int(row, "id")
        if observation_id is None:
            raise ValueError("finding_observations row is missing id")
        repository = _optional_string(row, "repository")
        pr_number = _optional_int(row, "pr_number")
        fingerprint = _required_string(row, "fingerprint")
        local_reference = (
            local_refs.get((repository, pr_number, fingerprint), "")
            if pr_number is not None
            else ""
        )
        provenances[observation_id] = DecisionProvenance(
            observation_id=observation_id,
            repository=repository,
            pr_number=pr_number,
            head_sha=_optional_string(row, "head_sha"),
            fingerprint=fingerprint,
            title=_optional_string(row, "title"),
            path=_optional_string(row, "path"),
            local_reference=local_reference,
        )
    return provenances


def _provenance_for_decision(
    row: Mapping[str, object],
    provenances: Mapping[int, DecisionProvenance],
) -> DecisionProvenance | None:
    observation_id = _optional_int(row, "observation_id")
    if observation_id is None:
        return None
    provenance = provenances.get(observation_id)
    if provenance is None:
        raise ValueError(
            f"decision observation_id {observation_id} is missing from finding_observations"
        )
    fingerprint = _required_string(row, "fingerprint")
    if provenance.fingerprint != fingerprint:
        raise ValueError(
            f"decision fingerprint {fingerprint} does not match observation {observation_id}"
        )
    return provenance


def _decision_signal(
    row: Mapping[str, object],
    provenance: DecisionProvenance | None,
    policy: SignalPolicy,
) -> LearningSignal:
    decision = _required_string(row, "decision")
    reason = _optional_string(row, "reason")
    missing_evidence: list[str] = []
    if not reason:
        missing_evidence.append("human reason")
    if provenance is None:
        missing_evidence.append("exact observation provenance")
    promotion_eligible = not missing_evidence
    signal_strength = policy.signal_strength if promotion_eligible else "incomplete"
    return LearningSignal(
        source="decision",
        source_value=decision,
        signal_strength=signal_strength,
        suggested_route=policy.suggested_route,
        title=_signal_title(decision, provenance),
        reason=reason,
        next_step=policy.next_step,
        promotion_eligible=promotion_eligible,
        missing_evidence=tuple(missing_evidence),
        repository=provenance.repository if provenance else "",
        pr_number=provenance.pr_number if provenance else None,
        fingerprint=_required_string(row, "fingerprint"),
        local_reference=provenance.local_reference if provenance else "",
        provenance=provenance,
    )


def _quality_signal(
    row: Mapping[str, object],
    policy: SignalPolicy,
) -> LearningSignal:
    category = _required_string(row, "category")
    reason = _optional_string(row, "reason")
    promotion_eligible = bool(reason)
    return LearningSignal(
        source="review_quality_feedback",
        source_value=category,
        signal_strength=policy.signal_strength if promotion_eligible else "incomplete",
        suggested_route=policy.suggested_route,
        title=f"Review-quality feedback: {category.replace('_', ' ')}",
        reason=reason,
        next_step=policy.next_step,
        promotion_eligible=promotion_eligible,
        missing_evidence=() if promotion_eligible else ("human reason",),
        repository=_required_string(row, "repository"),
        pr_number=_optional_int(row, "pr_number"),
        fingerprint="",
        local_reference=_optional_string(row, "local_reference"),
        provenance=None,
    )


def _matches_repository(
    repository: str | None,
    provenance: DecisionProvenance | None,
    row: Mapping[str, object],
) -> bool:
    if repository is None:
        return True
    if provenance is not None:
        return provenance.repository == repository
    row_repository = _optional_string(row, "repository")
    return row_repository == repository


def _signal_title(decision: str, provenance: DecisionProvenance | None) -> str:
    if provenance is None or not provenance.title:
        return f"Human decision: {decision}"
    return provenance.title


def _render_signals(prefix: str, signals: tuple[LearningSignal, ...]) -> list[str]:
    lines: list[str] = []
    for index, signal in enumerate(signals, start=1):
        label = f"{prefix}{index}"
        location = _location(signal)
        lines.extend(
            [
                f"### {label}: {signal.title}",
                "",
                f"- Source: `{signal.source}` / `{signal.source_value}`",
                f"- Signal: {signal.signal_strength}",
                f"- Suggested route: {signal.suggested_route}",
                f"- Scope: {location}",
                f"- Promotion eligible: {'yes' if signal.promotion_eligible else 'no'}",
            ]
        )
        if signal.fingerprint:
            lines.append(f"- Fingerprint: `{signal.fingerprint[:12]}`")
        if signal.local_reference:
            lines.append(f"- Local reference: `{signal.local_reference}`")
        if signal.reason:
            lines.append(f"- Human reason: {signal.reason}")
        if signal.missing_evidence:
            missing = ", ".join(signal.missing_evidence)
            lines.append(f"- Missing evidence: {missing}")
        lines.extend([f"- Next step: {signal.next_step}", ""])
    return lines


def _location(signal: LearningSignal) -> str:
    if signal.provenance is not None:
        parts = [signal.provenance.repository]
        if signal.provenance.pr_number is not None:
            parts.append(f"#{signal.provenance.pr_number}")
        if signal.provenance.path:
            parts.append(signal.provenance.path)
        return " ".join(part for part in parts if part)

    parts = [signal.repository]
    if signal.pr_number is not None:
        parts.append(f"#{signal.pr_number}")
    return " ".join(part for part in parts if part) or "(unknown)"


def _required_string(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return " ".join(value.strip().split())


def _optional_string(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    return " ".join(value.strip().split())


def _optional_int(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
