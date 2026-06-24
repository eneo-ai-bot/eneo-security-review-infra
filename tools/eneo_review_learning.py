"""Offline learning-candidate reports for the Eneo PR reviewer.

The public webhook reviewer must not import this module. It is operator tooling
that reads an exported review-memory JSON snapshot and turns explicit human
signals into a report for a private review-coach workflow.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from eneo_review_export import (
    DecisionProvenance,
    decision_provenances,
    load_export as _load_export,
    matches_repository,
    optional_int,
    optional_string,
    provenance_for_decision,
    required_string,
    row_id,
    rows,
    schema_version,
)


def load_export(path: Path) -> Mapping[str, object]:
    return _load_export(path)


@dataclass(frozen=True)
class LearningSignal:
    source: str
    source_id: str
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
    related_event_ids: tuple[str, ...]
    decision_chain: tuple[str, ...]


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


@dataclass(frozen=True)
class DecisionEpisode:
    fingerprint: str
    rows: tuple[Mapping[str, object], ...]
    latest: Mapping[str, object]
    provenance: DecisionProvenance | None
    source_id: str
    related_event_ids: tuple[str, ...]
    decision_chain: tuple[str, ...]


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
CONTRADICTORY_DECISIONS: Final = {
    "false_positive",
    "intentional_by_design",
    "accepted_risk",
    "duplicate",
}
CONTRADICTORY_OUTCOME_ROUTE: Final = "contradictory_outcome"
POSITIVE_PATTERN_ROUTE: Final = "positive_pattern"
CONTRADICTORY_OUTCOME_POLICY: Final = SignalPolicy(
    "medium",
    CONTRADICTORY_OUTCOME_ROUTE,
    "Investigate why the same observation moved through a human decision and "
    "later resolved before changing reviewer policy.",
)
POSITIVE_DECISION_POLICY: Final = SignalPolicy(
    "medium",
    POSITIVE_PATTERN_ROUTE,
    "Treat as a fixed-finding example only when the root cause is independently "
    "confirmed by a regression test.",
)
POSITIVE_FEEDBACK_POLICY: Final = SignalPolicy(
    "medium",
    POSITIVE_PATTERN_ROUTE,
    "Protect the behavior only if a later change risks regressing the useful "
    "output shape.",
)
_DERIVED_SIGNAL_POLICIES: Final[tuple[SignalPolicy, ...]] = (
    CONTRADICTORY_OUTCOME_POLICY,
    POSITIVE_DECISION_POLICY,
    POSITIVE_FEEDBACK_POLICY,
)
EMITTED_SUGGESTED_ROUTES: Final[frozenset[str]] = frozenset(
    {policy.suggested_route for policy in DECISION_POLICIES.values()}
    | {policy.suggested_route for policy in QUALITY_POLICIES.values()}
    | {policy.suggested_route for policy in _DERIVED_SIGNAL_POLICIES}
)
EMITTED_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    set(DECISION_POLICIES)
    | set(POSITIVE_DECISIONS)
    | set(QUALITY_POLICIES)
    | set(POSITIVE_FEEDBACK)
)


def build_learning_report(
    state: Mapping[str, object], *, repository: str | None = None
) -> LearningReport:
    source_schema_version = schema_version(state)
    provenance_by_observation_id = decision_provenances(state)
    decision_candidates: list[LearningSignal] = []
    quality_signals: list[LearningSignal] = []
    positive_patterns: list[LearningSignal] = []
    unclassified_decisions: set[str] = set()
    unclassified_feedback: set[str] = set()
    notes: list[str] = [
        "Weak signals such as silence, merge-without-fix, thumbs-up, or a later "
        "code change are not treated as learning candidates.",
    ]

    for episode in _decision_episodes(state, provenance_by_observation_id):
        decision = required_string(episode.latest, "decision")
        if not matches_repository(repository, episode.provenance, episode.latest):
            continue
        if decision in POSITIVE_DECISIONS:
            if _has_contradictory_chain(episode):
                decision_candidates.append(
                    _decision_signal(episode, CONTRADICTORY_OUTCOME_POLICY)
                )
            else:
                positive_patterns.append(
                    _decision_signal(episode, POSITIVE_DECISION_POLICY)
                )
        elif decision in DECISION_POLICIES:
            decision_candidates.append(
                _decision_signal(episode, DECISION_POLICIES[decision])
            )
        else:
            unclassified_decisions.add(decision)

    for row in rows(state, "review_quality_feedback"):
        category = required_string(row, "category")
        if repository is not None and required_string(row, "repository") != repository:
            continue
        if category in POSITIVE_FEEDBACK:
            positive_patterns.append(
                _quality_signal(row, POSITIVE_FEEDBACK_POLICY)
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
        schema_version=source_schema_version,
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


def _decision_episodes(
    state: Mapping[str, object],
    provenances: Mapping[int, DecisionProvenance],
) -> tuple[DecisionEpisode, ...]:
    grouped: dict[str, list[tuple[int, Mapping[str, object]]]] = {}
    for index, row in enumerate(rows(state, "decisions")):
        fingerprint = required_string(row, "fingerprint")
        observation_id = optional_int(row, "observation_id")
        key = (
            f"observation:{observation_id}"
            if observation_id is not None
            else f"legacy:{fingerprint}"
        )
        grouped.setdefault(key, []).append((index, row))

    episodes: list[DecisionEpisode] = []
    for key in sorted(grouped):
        ordered_items = tuple(sorted(grouped[key], key=_decision_order_key))
        ordered = tuple(row for _, row in ordered_items)
        latest = ordered[-1]
        fingerprint = required_string(latest, "fingerprint")
        provenance = provenance_for_decision(latest, provenances)
        event_ids = tuple(
            _decision_event_id(row, index) for index, row in ordered_items
        )
        episodes.append(
            DecisionEpisode(
                fingerprint=fingerprint,
                rows=ordered,
                latest=latest,
                provenance=provenance,
                source_id=event_ids[-1],
                related_event_ids=event_ids,
                decision_chain=tuple(required_string(row, "decision") for row in ordered),
            )
        )
    return tuple(episodes)


def _decision_order_key(item: tuple[int, Mapping[str, object]]) -> tuple[int, int]:
    index, row = item
    decision_id = row_id(row)
    return (decision_id if decision_id is not None else 1_000_000_000 + index, index)


def _decision_event_id(row: Mapping[str, object], fallback_index: int) -> str:
    decision_id = row_id(row)
    if decision_id is not None:
        return f"decision:{decision_id}"
    return f"decision:unidentified:{fallback_index + 1}"


def _has_contradictory_chain(episode: DecisionEpisode) -> bool:
    latest = required_string(episode.latest, "decision")
    if latest not in POSITIVE_DECISIONS:
        return False
    earlier = episode.decision_chain[:-1]
    return any(decision in CONTRADICTORY_DECISIONS for decision in earlier)


def _decision_signal(
    episode: DecisionEpisode,
    policy: SignalPolicy,
) -> LearningSignal:
    decision = required_string(episode.latest, "decision")
    reason = optional_string(episode.latest, "reason")
    missing_evidence: list[str] = []
    if not reason:
        missing_evidence.append("human reason")
    if episode.provenance is None:
        missing_evidence.append("exact observation provenance")
    promotion_eligible = not missing_evidence
    signal_strength = policy.signal_strength if promotion_eligible else "incomplete"
    return LearningSignal(
        source="decision",
        source_id=episode.source_id,
        source_value=decision,
        signal_strength=signal_strength,
        suggested_route=policy.suggested_route,
        title=_signal_title(decision, episode.provenance),
        reason=reason,
        next_step=policy.next_step,
        promotion_eligible=promotion_eligible,
        missing_evidence=tuple(missing_evidence),
        repository=episode.provenance.repository if episode.provenance else "",
        pr_number=episode.provenance.pr_number if episode.provenance else None,
        fingerprint=episode.fingerprint,
        local_reference=episode.provenance.local_reference if episode.provenance else "",
        provenance=episode.provenance,
        related_event_ids=episode.related_event_ids,
        decision_chain=episode.decision_chain,
    )


def _quality_signal(
    row: Mapping[str, object],
    policy: SignalPolicy,
) -> LearningSignal:
    category = required_string(row, "category")
    reason = optional_string(row, "reason")
    promotion_eligible = bool(reason)
    feedback_id = row_id(row)
    return LearningSignal(
        source="review_quality_feedback",
        source_id=f"feedback:{feedback_id}" if feedback_id is not None else "feedback:unidentified",
        source_value=category,
        signal_strength=policy.signal_strength if promotion_eligible else "incomplete",
        suggested_route=policy.suggested_route,
        title=f"Review-quality feedback: {category.replace('_', ' ')}",
        reason=reason,
        next_step=policy.next_step,
        promotion_eligible=promotion_eligible,
        missing_evidence=() if promotion_eligible else ("human reason",),
        repository=required_string(row, "repository"),
        pr_number=optional_int(row, "pr_number"),
        fingerprint="",
        local_reference=optional_string(row, "local_reference"),
        provenance=None,
        related_event_ids=(f"feedback:{feedback_id}",)
        if feedback_id is not None
        else ("feedback:unidentified",),
        decision_chain=(),
    )


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
                f"- Source: `{signal.source_id}` / `{signal.source_value}`",
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
        if signal.decision_chain:
            chain = " -> ".join(signal.decision_chain)
            lines.append(f"- Decision chain: {chain}")
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
