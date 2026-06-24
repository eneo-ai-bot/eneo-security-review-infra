"""Deterministic proposal bundles for the private Eneo reviewer coach."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, cast

from eneo_review_coach import COACH_EVENT_GROUPS, COACH_SCHEMA_VERSION
from eneo_review_learning import EMITTED_SIGNAL_STRENGTHS


PROPOSAL_SCHEMA_VERSION: Final = 1
ProposalDecision = Literal["propose", "no_change"]
DEFAULT_MAX_CANDIDATES: Final = 3
DEFAULT_MIN_INDEPENDENT_EPISODES: Final = 2
MAX_EVIDENCE_EVENTS_PER_CANDIDATE: Final = 5
MAX_SUMMARY_TEXT: Final = 500

REVIEW_QUALITY_PROVENANCE_REASON: Final = (
    "review-quality feedback needs exact publication or finding provenance"
)
POSITIVE_PATTERN_REASON: Final = "positive patterns need an explicit regression-risk trigger"
POLICY_CHANGE_GUARDRAIL: Final = (
    "Do not change reviewer policy unless a human has reviewed the proposal and a "
    "focused replay or behavior test proves the current reviewer repeats this "
    "mistake or needs to preserve this pattern."
)

_EVENT_TYPE_PRIORITY: Final[dict[str, int]] = {
    "missed_issue": 0,
    "severity_too_low": 1,
    "reopen": 2,
    "false_positive": 4,
    "intentional_by_design": 5,
    "severity_too_high": 6,
    "too_speculative": 7,
    "remediation_impractical": 8,
    "duplicate": 9,
    "accepted_risk": 10,
    "resolved": 11,
    "unclear": 12,
    "too_verbose": 13,
    "useful": 14,
}

_ROUTE_PRIORITY: Final[dict[str, int]] = {
    "contradictory_outcome": 3,
}

_TARGET_BY_ROUTE: Final[dict[str, str]] = {
    "architecture_context": "adr_or_skill",
    "contradictory_outcome": "replay_then_human_triage",
    "developer_experience": "review_contract",
    "evidence_gate_calibration": "replay_then_skill",
    "exact_decision": "governance_or_adr",
    "judgment_or_procedure": "replay_then_skill",
    "positive_pattern": "replay_guard",
    "procedure_or_mechanical_gap": "replay_then_plugin_or_skill",
    "remediation_quality": "review_contract",
    "root_cause_deduplication": "review_contract",
    "severity_calibration": "replay_then_skill",
    "stability_regression": "replay_then_skill",
}
PROPOSAL_SUPPORTED_SUGGESTED_ROUTES: Final[frozenset[str]] = frozenset(
    _TARGET_BY_ROUTE
) | frozenset(_ROUTE_PRIORITY)
PROPOSAL_SUPPORTED_EVENT_TYPES: Final[frozenset[str]] = frozenset(_EVENT_TYPE_PRIORITY)
_COACH_EXPORT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "snapshot_id",
        "event_set_id",
        "schema_version",
        "source_schema_version",
        "repository_untrusted",
        "source_exported_at",
        "cursor",
        "events",
        "notes",
    }
)
_COACH_EVENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "event_id",
        "event_group",
        "event_type",
        "signal_strength",
        "suggested_route",
        "promotion_eligible",
        "missing_evidence",
        "human_reason_untrusted",
        "reviewer_title_untrusted",
        "next_step_untrusted",
        "related_event_ids",
        "related_event_ids_total",
        "decision_chain",
        "decision_chain_total",
        "source",
    }
)
_COACH_SOURCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "repository_untrusted",
        "pr_number",
        "head_sha",
        "fingerprint",
        "observation_id",
        "local_reference",
        "path_untrusted",
    }
)
PROPOSAL_SUPPORTED_EVENT_GROUPS: Final[frozenset[str]] = COACH_EVENT_GROUPS
PROPOSAL_SUPPORTED_SIGNAL_STRENGTHS: Final[frozenset[str]] = EMITTED_SIGNAL_STRENGTHS
PROPOSAL_DECISIONS: Final[frozenset[str]] = frozenset({"propose", "no_change"})
PROPOSAL_FORBIDDEN_ACTIONS: Final[tuple[str, ...]] = (
    "Do not open a pull request automatically.",
    "Do not edit reviewer policy, prompts, skills, or code from this artifact alone.",
    "Do not suppress findings or change review-memory decisions from this artifact.",
    "Do not proceed without a focused replay fixture or behavior test.",
)
_PROPOSAL_BUNDLE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "source_coach_schema_version",
        "source_event_set_id",
        "source_snapshot_id",
        "repository_untrusted",
        "decision",
        "candidates",
        "governance_observations",
        "rejected_groups",
        "events_considered",
        "notes",
        "proposal_set_id",
    }
)
_PROPOSAL_CANDIDATE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "candidate_key",
        "target_owner",
        "suggested_route",
        "event_type",
        "independent_episode_count",
        "independent_episode_keys",
        "evidence_event_ids",
        "evidence_events_total",
        "evidence",
        "problem_untrusted",
        "proposed_change",
        "required_validation",
        "risk",
        "why_not_no_change",
    }
)
_PROPOSAL_EVIDENCE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "event_id",
        "event_group",
        "event_type",
        "signal_strength",
        "suggested_route",
        "repository_untrusted",
        "pr_number",
        "observation_id",
        "fingerprint",
        "local_reference",
        "reviewer_title_untrusted",
        "human_reason_untrusted",
        "next_step_untrusted",
        "related_event_ids",
    }
)
_PROPOSAL_REJECTED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "candidate_key",
        "suggested_route",
        "event_type",
        "reason",
        "independent_episode_count",
        "event_ids",
        "events_total",
    }
)

@dataclass(frozen=True)
class CoachEvent:
    event_id: str
    event_group: str
    event_type: str
    signal_strength: str
    suggested_route: str
    promotion_eligible: bool
    missing_evidence: tuple[str, ...]
    title_untrusted: str
    human_reason_untrusted: str
    next_step_untrusted: str
    repository_untrusted: str
    pr_number: int | None
    fingerprint: str
    observation_id: int | None
    local_reference: str
    related_event_ids: tuple[str, ...]


@dataclass(frozen=True)
class CandidateGroup:
    key: str
    event_group: str
    suggested_route: str
    event_type: str
    events: tuple[CoachEvent, ...]
    independent_episode_count: int
    independent_episode_keys: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceSummary:
    event_id: str
    event_group: str
    event_type: str
    signal_strength: str
    suggested_route: str
    repository_untrusted: str
    pr_number: int | None
    observation_id: int | None
    fingerprint: str
    local_reference: str
    reviewer_title_untrusted: str
    human_reason_untrusted: str
    next_step_untrusted: str
    related_event_ids: tuple[str, ...]

    def to_json_obj(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_group": self.event_group,
            "event_type": self.event_type,
            "signal_strength": self.signal_strength,
            "suggested_route": self.suggested_route,
            "repository_untrusted": self.repository_untrusted,
            "pr_number": self.pr_number,
            "observation_id": self.observation_id,
            "fingerprint": self.fingerprint,
            "local_reference": self.local_reference,
            "reviewer_title_untrusted": self.reviewer_title_untrusted,
            "human_reason_untrusted": self.human_reason_untrusted,
            "next_step_untrusted": self.next_step_untrusted,
            "related_event_ids": list(self.related_event_ids),
        }


@dataclass(frozen=True)
class CandidateProposal:
    candidate_key: str
    target_owner: str
    suggested_route: str
    event_type: str
    independent_episode_count: int
    independent_episode_keys: tuple[str, ...]
    evidence_event_ids: tuple[str, ...]
    evidence_events_total: int
    evidence: tuple[EvidenceSummary, ...]
    problem_untrusted: str
    proposed_change: str
    required_validation: tuple[str, ...]
    risk: str
    why_not_no_change: str

    def to_json_obj(self) -> dict[str, object]:
        return {
            "candidate_key": self.candidate_key,
            "target_owner": self.target_owner,
            "suggested_route": self.suggested_route,
            "event_type": self.event_type,
            "independent_episode_count": self.independent_episode_count,
            "independent_episode_keys": list(self.independent_episode_keys),
            "evidence_event_ids": list(self.evidence_event_ids),
            "evidence_events_total": self.evidence_events_total,
            "evidence": [item.to_json_obj() for item in self.evidence],
            "problem_untrusted": self.problem_untrusted,
            "proposed_change": self.proposed_change,
            "required_validation": list(self.required_validation),
            "risk": self.risk,
            "why_not_no_change": self.why_not_no_change,
        }


@dataclass(frozen=True)
class RejectedGroup:
    candidate_key: str
    suggested_route: str
    event_type: str
    reason: str
    independent_episode_count: int
    event_ids: tuple[str, ...]
    events_total: int

    def to_json_obj(self) -> dict[str, object]:
        return {
            "candidate_key": self.candidate_key,
            "suggested_route": self.suggested_route,
            "event_type": self.event_type,
            "reason": self.reason,
            "independent_episode_count": self.independent_episode_count,
            "event_ids": list(self.event_ids),
            "events_total": self.events_total,
        }


@dataclass(frozen=True)
class ProposalBundle:
    schema_version: int
    source_coach_schema_version: int
    source_event_set_id: str
    source_snapshot_id: str
    repository_untrusted: str
    decision: ProposalDecision
    candidates: tuple[CandidateProposal, ...]
    governance_observations: tuple[EvidenceSummary, ...]
    rejected_groups: tuple[RejectedGroup, ...]
    events_considered: int
    notes: tuple[str, ...]
    proposal_set_id: str

    def to_json_obj(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_coach_schema_version": self.source_coach_schema_version,
            "source_event_set_id": self.source_event_set_id,
            "source_snapshot_id": self.source_snapshot_id,
            "repository_untrusted": self.repository_untrusted,
            "decision": self.decision,
            "candidates": [item.to_json_obj() for item in self.candidates],
            "governance_observations": [
                item.to_json_obj() for item in self.governance_observations
            ],
            "rejected_groups": [item.to_json_obj() for item in self.rejected_groups],
            "events_considered": self.events_considered,
            "notes": list(self.notes),
            "proposal_set_id": self.proposal_set_id,
        }


@dataclass(frozen=True)
class ProposalVerification:
    proposal_set_id: str
    decision: ProposalDecision
    candidates_count: int
    governance_observations_count: int
    rejected_groups_count: int
    forbidden_actions: tuple[str, ...]

    def to_json_obj(self) -> dict[str, object]:
        return {
            "proposal_set_id": self.proposal_set_id,
            "decision": self.decision,
            "candidates_count": self.candidates_count,
            "governance_observations_count": self.governance_observations_count,
            "rejected_groups_count": self.rejected_groups_count,
            "forbidden_actions": list(self.forbidden_actions),
        }


def load_coach_export(path: Path) -> Mapping[str, object]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("coach export must be a JSON object")
    return cast(Mapping[str, object], raw)


def load_proposal_bundle(path: Path) -> ProposalBundle:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("proposal bundle must be a JSON object")
    bundle = _proposal_bundle(cast(Mapping[str, object], raw))
    verify_proposal_bundle(bundle)
    return bundle


def verify_proposal_bundle(bundle: ProposalBundle) -> ProposalVerification:
    _validate_loaded_bundle(bundle)
    return ProposalVerification(
        proposal_set_id=bundle.proposal_set_id,
        decision=bundle.decision,
        candidates_count=len(bundle.candidates),
        governance_observations_count=len(bundle.governance_observations),
        rejected_groups_count=len(bundle.rejected_groups),
        forbidden_actions=PROPOSAL_FORBIDDEN_ACTIONS,
    )


def build_proposal(
    coach_export: Mapping[str, object],
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    min_independent_episodes: int = DEFAULT_MIN_INDEPENDENT_EPISODES,
) -> ProposalBundle:
    if max_candidates < 1:
        raise ValueError("max_candidates must be at least 1")
    if min_independent_episodes < 1:
        raise ValueError("min_independent_episodes must be at least 1")

    _validate_schema(coach_export)
    events = _events(coach_export)
    grouped = _candidate_groups(events)
    candidates: list[CandidateGroup] = []
    governance: list[CoachEvent] = []
    rejected: list[RejectedGroup] = []

    for group in grouped:
        if group.event_group == "positive_pattern":
            rejected.append(_rejected_group(group, POSITIVE_PATTERN_REASON))
            continue
        if (
            group.event_type == "accepted_risk"
            and group.independent_episode_count < min_independent_episodes
        ):
            governance.extend(group.events)
            continue
        if group.event_group == "review_quality_signal" and not _has_stable_identity(group):
            rejected.append(_rejected_group(group, REVIEW_QUALITY_PROVENANCE_REASON))
            continue
        if not _has_stable_identity(group):
            rejected.append(_rejected_group(group, "missing stable finding identity"))
            continue
        if group.independent_episode_count < min_independent_episodes:
            rejected.append(
                _rejected_group(
                    group,
                    f"requires {min_independent_episodes} independent episodes",
                )
            )
            continue
        candidates.append(group)

    selected = sorted(candidates, key=_candidate_sort_key)[:max_candidates]
    candidate_payloads = tuple(_candidate_payload(group) for group in selected)
    governance_observations = tuple(
        _event_summary(event) for event in sorted(governance, key=lambda item: item.event_id)
    )
    rejected_groups = tuple(
        sorted(rejected, key=lambda item: (item.reason, item.candidate_key))
    )
    source_event_set_id = _required_top_level_string(coach_export, "event_set_id")
    proposal_set_id = _proposal_set_id(
        source_event_set_id=source_event_set_id,
        candidates=candidate_payloads,
        governance_observations=governance_observations,
    )
    return ProposalBundle(
        schema_version=PROPOSAL_SCHEMA_VERSION,
        source_coach_schema_version=COACH_SCHEMA_VERSION,
        source_event_set_id=source_event_set_id,
        source_snapshot_id=_optional_string(
            coach_export, "snapshot_id", "coach export"
        ),
        repository_untrusted=_optional_string(
            coach_export, "repository_untrusted", "coach export"
        ),
        decision="propose" if candidate_payloads else "no_change",
        candidates=candidate_payloads,
        governance_observations=governance_observations,
        rejected_groups=rejected_groups,
        events_considered=len(events),
        notes=(
            "This private artifact is an evidence-selection proposal, not reviewer policy.",
            POLICY_CHANGE_GUARDRAIL,
            "Fields ending in _untrusted remain bounded untrusted text from humans "
            "or repositories.",
        ),
        proposal_set_id=proposal_set_id,
    )


def dumps_proposal_bundle(bundle: ProposalBundle) -> str:
    return json.dumps(bundle.to_json_obj(), sort_keys=True, indent=2) + "\n"


def render_markdown(bundle: ProposalBundle) -> str:
    lines = [
        "# Eneo reviewer coach proposal",
        "",
        "This private bundle selects review-memory signals that may be worth turning "
        "into a replay, skill, ADR, or plugin improvement. It does not change "
        "reviewer policy and it does not open a PR.",
        "",
        f"- Decision: `{bundle.decision}`",
        f"- Proposal set: `{bundle.proposal_set_id}`",
        f"- Source event set: `{bundle.source_event_set_id}`",
        f"- Repository: `{bundle.repository_untrusted or '(all repositories)'}`",
        f"- Events considered: {bundle.events_considered}",
        f"- Candidates: {len(bundle.candidates)}",
        f"- Governance observations: {len(bundle.governance_observations)}",
        f"- Rejected groups: {len(bundle.rejected_groups)}",
        "",
    ]

    if bundle.candidates:
        lines.extend(["## Candidate proposals", ""])
        for index, candidate in enumerate(bundle.candidates, start=1):
            lines.extend(_render_candidate(index, candidate))
    else:
        lines.extend(
            [
                "## Candidate proposals",
                "",
                "No candidate met the admission rules. Keep collecting explicit "
                "decisions or review-quality feedback instead of changing policy "
                "from a single weak signal.",
                "",
            ]
        )

    if bundle.governance_observations:
        lines.extend(["## Governance observations", ""])
        lines.append(
            "These are useful for audit or ADR context, but are not enough by "
            "themselves to change reviewer policy."
        )
        lines.append("")
        for item in bundle.governance_observations:
            reason = item.human_reason_untrusted
            fallback = item.reviewer_title_untrusted
            lines.append(f"- `{item.event_id}`: {_bounded(reason or fallback)}")
        lines.append("")

    if bundle.rejected_groups:
        lines.extend(["## Not promoted", ""])
        for item in bundle.rejected_groups:
            lines.append(
                f"- `{item.candidate_key}`: {item.reason} "
                f"({item.independent_episode_count} independent episode(s))."
            )
        lines.append("")

    if bundle.candidates:
        lines.extend(["## Copyable next step", "", "```text"])
        first = bundle.candidates[0]
        lines.extend(
            [
                "Review this coach proposal as an evidence-backed reviewer-improvement candidate.",
                f"Candidate key: {first.candidate_key}",
                f"Target owner: {first.target_owner}",
                f"Evidence event ids: {', '.join(first.evidence_event_ids)}",
                POLICY_CHANGE_GUARDRAIL,
            ]
        )
        lines.extend(["```", ""])

    return "\n".join(lines)


def _validate_schema(payload: Mapping[str, object]) -> None:
    _reject_unknown_keys(payload, _COACH_EXPORT_KEYS, "coach export")
    schema_value = payload.get("schema_version")
    if type(schema_value) is not int or schema_value != COACH_SCHEMA_VERSION:
        raise ValueError(
            f"coach export schema_version must be {COACH_SCHEMA_VERSION}; got {schema_value!r}"
        )
    _required_top_level_string(payload, "event_set_id")
    if "events" not in payload:
        raise ValueError("coach export is missing events")


def _proposal_bundle(payload: Mapping[str, object]) -> ProposalBundle:
    context = "proposal bundle"
    _reject_unknown_keys(payload, _PROPOSAL_BUNDLE_KEYS, context)
    schema_value = payload.get("schema_version")
    if type(schema_value) is not int or schema_value != PROPOSAL_SCHEMA_VERSION:
        raise ValueError(
            f"proposal bundle schema_version must be {PROPOSAL_SCHEMA_VERSION}; "
            f"got {schema_value!r}"
        )
    source_schema_value = payload.get("source_coach_schema_version")
    if type(source_schema_value) is not int or source_schema_value != COACH_SCHEMA_VERSION:
        raise ValueError(
            "proposal bundle source_coach_schema_version must be "
            f"{COACH_SCHEMA_VERSION}; got {source_schema_value!r}"
        )
    decision = _proposal_decision(_required_string(payload, "decision", context))
    return ProposalBundle(
        schema_version=PROPOSAL_SCHEMA_VERSION,
        source_coach_schema_version=COACH_SCHEMA_VERSION,
        source_event_set_id=_required_string(payload, "source_event_set_id", context),
        source_snapshot_id=_optional_string(payload, "source_snapshot_id", context),
        repository_untrusted=_optional_string(payload, "repository_untrusted", context),
        decision=decision,
        candidates=tuple(
            _proposal_candidate(item, index)
            for index, item in enumerate(
                _sequence_of_mappings_in_context(payload, "candidates", context)
            )
        ),
        governance_observations=tuple(
            _proposal_evidence(item, f"governance_observations[{index}]")
            for index, item in enumerate(
                _sequence_of_mappings_in_context(
                    payload, "governance_observations", context
                )
            )
        ),
        rejected_groups=tuple(
            _proposal_rejected(item, index)
            for index, item in enumerate(
                _sequence_of_mappings_in_context(payload, "rejected_groups", context)
            )
        ),
        events_considered=_required_int(payload, "events_considered", context, minimum=0),
        notes=_string_tuple(payload, "notes", context),
        proposal_set_id=_required_string(payload, "proposal_set_id", context),
    )


def _proposal_candidate(
    item: Mapping[str, object], index: int
) -> CandidateProposal:
    context = f"candidates[{index}]"
    _reject_unknown_keys(item, _PROPOSAL_CANDIDATE_KEYS, context)
    return CandidateProposal(
        candidate_key=_required_string(item, "candidate_key", context),
        target_owner=_required_string(item, "target_owner", context),
        suggested_route=_required_string(item, "suggested_route", context),
        event_type=_required_string(item, "event_type", context),
        independent_episode_count=_required_int(
            item, "independent_episode_count", context, minimum=1
        ),
        independent_episode_keys=_string_tuple(
            item, "independent_episode_keys", context
        ),
        evidence_event_ids=_string_tuple(item, "evidence_event_ids", context),
        evidence_events_total=_required_int(
            item, "evidence_events_total", context, minimum=0
        ),
        evidence=tuple(
            _proposal_evidence(evidence, f"{context}.evidence[{evidence_index}]")
            for evidence_index, evidence in enumerate(
                _sequence_of_mappings_in_context(item, "evidence", context)
            )
        ),
        problem_untrusted=_optional_string(item, "problem_untrusted", context),
        proposed_change=_required_string(item, "proposed_change", context),
        required_validation=_string_tuple(item, "required_validation", context),
        risk=_required_string(item, "risk", context),
        why_not_no_change=_required_string(item, "why_not_no_change", context),
    )


def _proposal_evidence(
    item: Mapping[str, object], context: str
) -> EvidenceSummary:
    _reject_unknown_keys(item, _PROPOSAL_EVIDENCE_KEYS, context)
    return EvidenceSummary(
        event_id=_required_string(item, "event_id", context),
        event_group=_required_string(item, "event_group", context),
        event_type=_required_string(item, "event_type", context),
        signal_strength=_required_string(item, "signal_strength", context),
        suggested_route=_required_string(item, "suggested_route", context),
        repository_untrusted=_optional_string(item, "repository_untrusted", context),
        pr_number=_optional_int(item, "pr_number", context),
        observation_id=_optional_int(item, "observation_id", context),
        fingerprint=_optional_string(item, "fingerprint", context),
        local_reference=_optional_string(item, "local_reference", context),
        reviewer_title_untrusted=_optional_string(
            item, "reviewer_title_untrusted", context
        ),
        human_reason_untrusted=_optional_string(
            item, "human_reason_untrusted", context
        ),
        next_step_untrusted=_optional_string(item, "next_step_untrusted", context),
        related_event_ids=_string_tuple(item, "related_event_ids", context),
    )


def _proposal_rejected(item: Mapping[str, object], index: int) -> RejectedGroup:
    context = f"rejected_groups[{index}]"
    _reject_unknown_keys(item, _PROPOSAL_REJECTED_KEYS, context)
    return RejectedGroup(
        candidate_key=_required_string(item, "candidate_key", context),
        suggested_route=_required_string(item, "suggested_route", context),
        event_type=_required_string(item, "event_type", context),
        reason=_required_string(item, "reason", context),
        independent_episode_count=_required_int(
            item, "independent_episode_count", context, minimum=0
        ),
        event_ids=_string_tuple(item, "event_ids", context),
        events_total=_required_int(item, "events_total", context, minimum=0),
    )


def _validate_loaded_bundle(bundle: ProposalBundle) -> None:
    expected_decision = "propose" if bundle.candidates else "no_change"
    if bundle.decision != expected_decision:
        raise ValueError(
            "proposal bundle decision does not match candidate presence"
        )
    # The proposal id protects candidate/evidence identity and governance membership;
    # field-level consistency checks below guard the non-hashed presentation details.
    expected_id = _proposal_set_id(
        source_event_set_id=bundle.source_event_set_id,
        candidates=bundle.candidates,
        governance_observations=bundle.governance_observations,
    )
    if bundle.proposal_set_id != expected_id:
        raise ValueError("proposal bundle proposal_set_id does not match its content")
    if bundle.events_considered < 0:
        raise ValueError("proposal bundle events_considered must be zero or greater")
    for index, candidate in enumerate(bundle.candidates):
        context = f"candidates[{index}]"
        if candidate.independent_episode_count != len(
            candidate.independent_episode_keys
        ):
            raise ValueError(
                f"{context}.independent_episode_count does not match keys"
            )
        if candidate.evidence_events_total < len(candidate.evidence_event_ids):
            raise ValueError(
                f"{context}.evidence_events_total is smaller than evidence ids"
            )
        evidence_ids = tuple(item.event_id for item in candidate.evidence)
        if candidate.evidence_event_ids != evidence_ids:
            raise ValueError(f"{context}.evidence_event_ids do not match evidence")
        if not candidate.required_validation:
            raise ValueError(f"{context}.required_validation must not be empty")
    for index, group in enumerate(bundle.rejected_groups):
        if group.events_total < len(group.event_ids):
            raise ValueError(
                f"rejected_groups[{index}].events_total is smaller than event ids"
            )


def _required_top_level_string(row: Mapping[str, object], key: str) -> str:
    return _required_string(row, key, "coach export")


def _proposal_decision(value: str) -> ProposalDecision:
    if value == "propose":
        return "propose"
    if value == "no_change":
        return "no_change"
    raise ValueError(f"proposal bundle decision has unsupported value {value!r}")


def _events(payload: Mapping[str, object]) -> tuple[CoachEvent, ...]:
    raw_events = _sequence_of_mappings(payload, "events")
    events: list[CoachEvent] = []
    for index, item in enumerate(raw_events):
        events.append(_event(item, index))
    event_ids = [event.event_id for event in events]
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("coach export events contain duplicate event_id values")
    return tuple(events)


def _event(item: Mapping[str, object], index: int) -> CoachEvent:
    context = f"events[{index}]"
    _reject_unknown_keys(item, _COACH_EVENT_KEYS, context)
    source = _required_mapping(item, "source", index)
    _reject_unknown_keys(source, _COACH_SOURCE_KEYS, f"{context}.source")
    event_group = _required_string(item, "event_group", context)
    if event_group not in PROPOSAL_SUPPORTED_EVENT_GROUPS:
        raise ValueError(f"{context}.event_group has unsupported value {event_group!r}")
    event_type = _required_string(item, "event_type", context)
    if event_type not in PROPOSAL_SUPPORTED_EVENT_TYPES:
        raise ValueError(f"{context}.event_type has unsupported value {event_type!r}")
    signal_strength = _required_string(item, "signal_strength", context)
    if signal_strength not in PROPOSAL_SUPPORTED_SIGNAL_STRENGTHS:
        raise ValueError(
            f"{context}.signal_strength has unsupported value {signal_strength!r}"
        )
    suggested_route = _required_string(item, "suggested_route", context)
    if suggested_route not in PROPOSAL_SUPPORTED_SUGGESTED_ROUTES:
        raise ValueError(
            f"{context}.suggested_route has unsupported value {suggested_route!r}"
        )
    return CoachEvent(
        event_id=_required_string(item, "event_id", context),
        event_group=event_group,
        event_type=event_type,
        signal_strength=signal_strength,
        suggested_route=suggested_route,
        promotion_eligible=_required_bool(item, "promotion_eligible", index),
        missing_evidence=tuple(_string_list(item, "missing_evidence", index)),
        title_untrusted=_optional_string(
            item, "reviewer_title_untrusted", context
        ),
        human_reason_untrusted=_optional_string(
            item, "human_reason_untrusted", context
        ),
        next_step_untrusted=_optional_string(item, "next_step_untrusted", context),
        repository_untrusted=_optional_string(
            source, "repository_untrusted", f"{context}.source"
        ),
        pr_number=_optional_int(source, "pr_number", f"{context}.source"),
        fingerprint=_optional_string(source, "fingerprint", f"{context}.source"),
        observation_id=_optional_int(source, "observation_id", f"{context}.source"),
        local_reference=_optional_string(
            source, "local_reference", f"{context}.source"
        ),
        related_event_ids=tuple(_string_list(item, "related_event_ids", index)),
    )


def _candidate_groups(events: Iterable[CoachEvent]) -> tuple[CandidateGroup, ...]:
    grouped: dict[tuple[str, str, str, str], list[CoachEvent]] = {}
    for event in events:
        if (
            not event.promotion_eligible
            or event.signal_strength == "incomplete"
            or event.missing_evidence
        ):
            continue
        identity = _semantic_identity(event)
        key = (event.event_group, event.suggested_route, event.event_type, identity)
        grouped.setdefault(key, []).append(event)

    groups: list[CandidateGroup] = []
    for (event_group, route, event_type, identity), raw_group_events in grouped.items():
        ordered_events = tuple(sorted(raw_group_events, key=lambda item: item.event_id))
        independent_episode_keys = tuple(
            sorted({_episode_key(event) for event in ordered_events})
        )
        groups.append(
            CandidateGroup(
                key=_candidate_key(route, event_type, identity),
                event_group=event_group,
                suggested_route=route,
                event_type=event_type,
                events=ordered_events,
                independent_episode_count=len(independent_episode_keys),
                independent_episode_keys=independent_episode_keys,
            )
        )
    return tuple(groups)


def _semantic_identity(event: CoachEvent) -> str:
    if event.fingerprint:
        return f"fingerprint:{event.fingerprint}"
    if event.observation_id is not None:
        return f"observation:{event.observation_id}"
    return f"unprovenanced:{_slug(event.title_untrusted or event.event_type)}"


def _episode_key(event: CoachEvent) -> str:
    if event.observation_id is not None:
        return f"observation:{event.observation_id}"
    parts = [event.repository_untrusted, str(event.pr_number or ""), event.local_reference]
    scope = ":".join(part for part in parts if part)
    return f"event:{scope}:{event.event_id}" if scope else f"event:{event.event_id}"


def _has_stable_identity(group: CandidateGroup) -> bool:
    return any(event.fingerprint or event.observation_id is not None for event in group.events)


def _candidate_payload(group: CandidateGroup) -> CandidateProposal:
    evidence_events = group.events[:MAX_EVIDENCE_EVENTS_PER_CANDIDATE]
    evidence_ids = tuple(event.event_id for event in evidence_events)
    return CandidateProposal(
        candidate_key=group.key,
        target_owner=_TARGET_BY_ROUTE.get(group.suggested_route, "human_triage"),
        suggested_route=group.suggested_route,
        event_type=group.event_type,
        independent_episode_count=group.independent_episode_count,
        independent_episode_keys=group.independent_episode_keys,
        evidence_event_ids=evidence_ids,
        evidence_events_total=len(group.events),
        evidence=tuple(_event_summary(event) for event in evidence_events),
        problem_untrusted=_candidate_problem(group),
        proposed_change=_proposed_change(group),
        required_validation=(
            "Add or update a focused replay fixture or behavior test for this exact pattern.",
            "Run strict fixture validation and the reviewer bundle checks before "
            "proposing policy changes.",
        ),
        risk=_risk(group),
        why_not_no_change=(
            "The same stable finding identity appeared in enough independent episodes "
            "to justify human review of a targeted reviewer improvement."
        ),
    )


def _event_summary(event: CoachEvent) -> EvidenceSummary:
    return EvidenceSummary(
        event_id=event.event_id,
        event_group=event.event_group,
        event_type=event.event_type,
        signal_strength=event.signal_strength,
        suggested_route=event.suggested_route,
        repository_untrusted=event.repository_untrusted,
        pr_number=event.pr_number,
        observation_id=event.observation_id,
        fingerprint=event.fingerprint,
        local_reference=event.local_reference,
        reviewer_title_untrusted=_bounded(event.title_untrusted),
        human_reason_untrusted=_bounded(event.human_reason_untrusted),
        next_step_untrusted=_bounded(event.next_step_untrusted),
        related_event_ids=event.related_event_ids,
    )


def _candidate_problem(group: CandidateGroup) -> str:
    reason = next(
        (event.human_reason_untrusted for event in group.events if event.human_reason_untrusted),
        "",
    )
    title = next((event.title_untrusted for event in group.events if event.title_untrusted), "")
    return _bounded(reason or title or f"{group.event_type} via {group.suggested_route}")


def _proposed_change(group: CandidateGroup) -> str:
    target = _TARGET_BY_ROUTE.get(group.suggested_route, "human_triage")
    if group.event_type == "accepted_risk":
        return (
            "Review whether repeated accepted-risk decisions point to a missing ADR, checklist, "
            "or explicit Eneo architecture context before changing reviewer behavior."
        )
    return (
        f"Start in `{target}`. Add a focused replay or behavior test for this pattern, "
        "then make the smallest owner-local change only if the current reviewer reproduces it."
    )


def _risk(group: CandidateGroup) -> str:
    if group.suggested_route in {"evidence_gate_calibration", "judgment_or_procedure"}:
        return (
            "Over-broad prompt changes can hide real defects; protect known true "
            "positives with a negative-control replay."
        )
    if group.suggested_route == "severity_calibration":
        return "Severity changes can normalize real impact; keep examples concrete and diff-scoped."
    if group.suggested_route == "architecture_context":
        return (
            "Architecture context can become a broad suppression; anchor it to an "
            "accepted ADR or exact design invariant."
        )
    return "Changing reviewer behavior from weak evidence can add noise or suppress valid findings."


def _candidate_sort_key(group: CandidateGroup) -> tuple[int, int, str, str]:
    priority = _ROUTE_PRIORITY.get(
        group.suggested_route, _EVENT_TYPE_PRIORITY.get(group.event_type, 100)
    )
    return (priority, -group.independent_episode_count, group.suggested_route, group.key)


def _rejected_group(group: CandidateGroup, reason: str) -> RejectedGroup:
    return RejectedGroup(
        candidate_key=group.key,
        suggested_route=group.suggested_route,
        event_type=group.event_type,
        reason=reason,
        independent_episode_count=group.independent_episode_count,
        event_ids=tuple(
            event.event_id for event in group.events[:MAX_EVIDENCE_EVENTS_PER_CANDIDATE]
        ),
        events_total=len(group.events),
    )


def _candidate_key(route: str, event_type: str, identity: str) -> str:
    digest = hashlib.sha256(f"{route}:{event_type}:{identity}".encode("utf-8")).hexdigest()[:12]
    prefix = _slug(f"{route}-{event_type}")[:48].strip("-") or "candidate"
    return f"{prefix}-{digest}"


def _proposal_set_id(
    *,
    source_event_set_id: str,
    candidates: tuple[CandidateProposal, ...],
    governance_observations: tuple[EvidenceSummary, ...],
) -> str:
    stable = {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "source_event_set_id": source_event_set_id,
        "candidates": [
            {
                "candidate_key": item.candidate_key,
                "evidence_event_ids": list(item.evidence_event_ids),
            }
            for item in candidates
        ],
        "governance_observations": [item.event_id for item in governance_observations],
    }
    digest = hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _render_candidate(index: int, candidate: CandidateProposal) -> list[str]:
    evidence_ids = ", ".join(f"`{event_id}`" for event_id in candidate.evidence_event_ids)
    lines = [
        f"### C{index}: {candidate.candidate_key}",
        "",
        f"- Target owner: `{candidate.target_owner}`",
        f"- Route: `{candidate.suggested_route}` / `{candidate.event_type}`",
        f"- Independent episodes: {candidate.independent_episode_count}",
        f"- Evidence events: {evidence_ids}",
        f"- Problem: {candidate.problem_untrusted}",
        f"- Proposed change: {candidate.proposed_change}",
        f"- Risk: {candidate.risk}",
        "",
    ]
    return lines


def _required_mapping(
    row: Mapping[str, object], key: str, event_index: int
) -> Mapping[str, object]:
    value = row.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"events[{event_index}].{key} must be an object")
    return cast(Mapping[str, object], value)


def _sequence_of_mappings(row: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    value = row.get(key, [])
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{key} must be a list")
    items = cast(Sequence[object], value)
    output: list[Mapping[str, object]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"{key}[{index}] must be an object")
        output.append(cast(Mapping[str, object], item))
    return tuple(output)


def _string_list(
    row: Mapping[str, object], key: str, event_index: int
) -> tuple[str, ...]:
    context = f"events[{event_index}].{key}"
    value = row.get(key, [])
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{context} must be a list")
    items = cast(Sequence[object], value)
    output: list[str] = []
    for item_index, item in enumerate(items):
        if not isinstance(item, str):
            raise ValueError(f"{context}[{item_index}] must be a string")
        normalized = " ".join(item.strip().split())
        if not normalized:
            raise ValueError(f"{context}[{item_index}] must be non-empty")
        output.append(normalized)
    return tuple(output)


def _reject_unknown_keys(
    row: Mapping[str, object], allowed: frozenset[str], context: str
) -> None:
    unknown: list[str] = []
    for key in row:
        if key not in allowed:
            unknown.append(key)
    if unknown:
        formatted = ", ".join(sorted(unknown))
        raise ValueError(f"{context} contains unknown keys: {formatted}")


def _optional_string(row: Mapping[str, object], key: str, context: str) -> str:
    if key not in row:
        return ""
    value = row[key]
    if not isinstance(value, str):
        raise ValueError(f"{context}.{key} must be a string")
    return " ".join(value.strip().split())


def _required_string(row: Mapping[str, object], key: str, context: str) -> str:
    value = _optional_string(row, key, context)
    if not value:
        raise ValueError(f"{context}.{key} is required")
    return value


def _required_int(
    row: Mapping[str, object], key: str, context: str, *, minimum: int
) -> int:
    value = _optional_int(row, key, context)
    if value is None:
        raise ValueError(f"{context}.{key} is required")
    if value < minimum:
        raise ValueError(f"{context}.{key} must be at least {minimum}")
    return value


def _optional_int(row: Mapping[str, object], key: str, context: str) -> int | None:
    if key not in row or row[key] is None:
        return None
    value = row[key]
    if type(value) is not int:
        raise ValueError(f"{context}.{key} must be an integer")
    return value


def _string_tuple(
    row: Mapping[str, object], key: str, context: str
) -> tuple[str, ...]:
    value = row.get(key, [])
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{context}.{key} must be a list")
    items = cast(Sequence[object], value)
    output: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise ValueError(f"{context}.{key}[{index}] must be a string")
        normalized = " ".join(item.strip().split())
        if not normalized:
            raise ValueError(f"{context}.{key}[{index}] must be non-empty")
        output.append(normalized)
    return tuple(output)


def _sequence_of_mappings_in_context(
    row: Mapping[str, object], key: str, context: str
) -> tuple[Mapping[str, object], ...]:
    value = row.get(key, [])
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"{context}.{key} must be a list")
    items = cast(Sequence[object], value)
    output: list[Mapping[str, object]] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"{context}.{key}[{index}] must be an object")
        output.append(cast(Mapping[str, object], item))
    return tuple(output)


def _required_bool(row: Mapping[str, object], key: str, event_index: int) -> bool:
    value = row.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"events[{event_index}].{key} must be a boolean")
    return value


def _bounded(value: str) -> str:
    text = " ".join(value.strip().split())
    if len(text) <= MAX_SUMMARY_TEXT:
        return text
    return text[: MAX_SUMMARY_TEXT - 3].rstrip() + "..."


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
