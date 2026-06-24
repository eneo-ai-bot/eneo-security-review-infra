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


SUPPORTED_SCHEMA_VERSIONS: Final = {4}


@dataclass(frozen=True)
class FindingRef:
    fingerprint: str
    repository: str
    pr_number: int | None
    rule_id: str
    title: str
    path: str
    severity: str
    category: str


@dataclass(frozen=True)
class LearningSignal:
    source: str
    source_value: str
    signal_strength: str
    classification: str
    title: str
    reason: str
    next_step: str
    repository: str
    pr_number: int | None
    fingerprint: str
    local_reference: str
    finding: FindingRef | None


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
    classification: str
    next_step: str


DECISION_POLICIES: Final[dict[str, SignalPolicy]] = {
    "false_positive": SignalPolicy(
        "strong",
        "judgment_calibration",
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
    finding_by_fingerprint = {
        finding.fingerprint: finding for finding in _finding_refs(state)
    }
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
        fingerprint = _required_string(row, "fingerprint")
        finding = finding_by_fingerprint.get(fingerprint)
        if not _matches_repository(repository, finding, row):
            continue
        if decision in POSITIVE_DECISIONS:
            positive_patterns.append(
                _decision_signal(
                    row,
                    finding,
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
                _decision_signal(row, finding, DECISION_POLICIES[decision])
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


def _finding_refs(state: Mapping[str, object]) -> tuple[FindingRef, ...]:
    refs: list[FindingRef] = []
    for row in _rows(state, "findings"):
        refs.append(
            FindingRef(
                fingerprint=_required_string(row, "fingerprint"),
                repository=_optional_string(row, "repository"),
                pr_number=_optional_int(row, "pr_number"),
                rule_id=_optional_string(row, "rule_id"),
                title=_optional_string(row, "title"),
                path=_optional_string(row, "path"),
                severity=_optional_string(row, "severity"),
                category=_optional_string(row, "category"),
            )
        )
    return tuple(refs)


def _decision_signal(
    row: Mapping[str, object],
    finding: FindingRef | None,
    policy: SignalPolicy,
) -> LearningSignal:
    decision = _required_string(row, "decision")
    fallback_repository = finding.repository if finding else ""
    fallback_pr = finding.pr_number if finding else None
    return LearningSignal(
        source="decision",
        source_value=decision,
        signal_strength=policy.signal_strength,
        classification=policy.classification,
        title=_signal_title(decision, finding),
        reason=_optional_string(row, "reason"),
        next_step=policy.next_step,
        repository=fallback_repository,
        pr_number=fallback_pr,
        fingerprint=_required_string(row, "fingerprint"),
        local_reference="",
        finding=finding,
    )


def _quality_signal(
    row: Mapping[str, object],
    policy: SignalPolicy,
) -> LearningSignal:
    category = _required_string(row, "category")
    return LearningSignal(
        source="review_quality_feedback",
        source_value=category,
        signal_strength=policy.signal_strength,
        classification=policy.classification,
        title=f"Review-quality feedback: {category.replace('_', ' ')}",
        reason=_optional_string(row, "reason"),
        next_step=policy.next_step,
        repository=_required_string(row, "repository"),
        pr_number=_optional_int(row, "pr_number"),
        fingerprint="",
        local_reference=_optional_string(row, "local_reference"),
        finding=None,
    )


def _matches_repository(
    repository: str | None,
    finding: FindingRef | None,
    row: Mapping[str, object],
) -> bool:
    if repository is None:
        return True
    if finding is not None:
        return finding.repository == repository
    row_repository = _optional_string(row, "repository")
    return row_repository == repository


def _signal_title(decision: str, finding: FindingRef | None) -> str:
    if finding is None or not finding.title:
        return f"Human decision: {decision}"
    return finding.title


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
                f"- Classification: {signal.classification}",
                f"- Scope: {location}",
            ]
        )
        if signal.fingerprint:
            lines.append(f"- Fingerprint: `{signal.fingerprint[:12]}`")
        if signal.local_reference:
            lines.append(f"- Local reference: `{signal.local_reference}`")
        if signal.reason:
            lines.append(f"- Human reason: {signal.reason}")
        lines.extend([f"- Next step: {signal.next_step}", ""])
    return lines


def _location(signal: LearningSignal) -> str:
    if signal.finding is not None:
        parts = [signal.finding.repository]
        if signal.finding.pr_number is not None:
            parts.append(f"#{signal.finding.pr_number}")
        if signal.finding.path:
            parts.append(signal.finding.path)
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
