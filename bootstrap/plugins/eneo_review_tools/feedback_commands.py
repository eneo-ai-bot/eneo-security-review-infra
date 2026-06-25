"""Pure parser for deterministic PR feedback commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

try:
    from .feedback_contract import contains_placeholder
    from .memory_validation import (
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        local_reference_number,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from feedback_contract import contains_placeholder
    from memory_validation import (
        ReviewMemoryError,
        clean_multiline,
        clean_text,
        local_reference_number,
    )

DecisionFeedbackValue = Literal["false_positive", "intentional_by_design"]
ReviewFeedbackCategory = Literal["missed_issue"]


@dataclass(frozen=True)
class FindingFeedbackCommand:
    kind: Literal["finding"]
    decision: DecisionFeedbackValue
    local_reference: str
    reason: str
    adr_id: str = ""


@dataclass(frozen=True)
class ReviewQualityFeedbackCommand:
    kind: Literal["review_quality"]
    category: ReviewFeedbackCategory
    reason: str


FeedbackCommand = FindingFeedbackCommand | ReviewQualityFeedbackCommand

__all__ = (
    "DecisionFeedbackValue",
    "FeedbackCommand",
    "FindingFeedbackCommand",
    "ReviewFeedbackCategory",
    "ReviewQualityFeedbackCommand",
    "parse_review_feedback_command",
)

_TRIGGER_RE = re.compile(r"^\s*[@/]review\b", re.IGNORECASE | re.MULTILINE)
_COMMAND_RE = re.compile(r"^\s*[@/]review(?:\s+(?P<body>.*))?\s*$", re.IGNORECASE | re.DOTALL)
_ADR_RE = re.compile(r"ADR-[A-Za-z0-9][A-Za-z0-9._-]{0,76}$")
_LEADING_BECAUSE_RE = re.compile(r"^because\b(?:\s*[:,-]?\s*)?", re.IGNORECASE)


def _local_reference(value: str) -> str:
    reference = clean_text(value, field="local_reference", maximum=12).upper()
    if local_reference_number(reference) < 1:
        raise ReviewMemoryError("local_reference must look like F1, F2, ...")
    return reference


def _reason(value: str) -> str:
    reason = clean_multiline(
        _LEADING_BECAUSE_RE.sub("", value.strip(), count=1).strip(),
        field="reason",
        maximum=2000,
    )
    if contains_placeholder(reason):
        raise ReviewMemoryError("replace placeholder text before submitting feedback")
    return reason


def _adr_id(value: str) -> str:
    adr_id = clean_text(value, field="ADR id", maximum=80)
    if not _ADR_RE.fullmatch(adr_id):
        raise ReviewMemoryError("ADR id must look like ADR-123")
    return adr_id


def parse_review_feedback_command(body: str) -> FeedbackCommand | None:
    text = str(body or "").strip()
    if len(_TRIGGER_RE.findall(text)) > 1:
        raise ReviewMemoryError("one feedback command per comment is supported")

    match = _COMMAND_RE.fullmatch(text)
    if not match:
        return None
    payload = str(match.group("body") or "").strip()
    if not payload:
        return None

    verb, _, rest = payload.partition(" ")
    normalized_verb = verb.strip().lower().replace("_", "-")
    rest = rest.strip()

    if normalized_verb in {"accepted-risk", "accepted_risk"}:
        raise ReviewMemoryError("accepted risk decisions require the governance CLI")

    if normalized_verb == "false-positive":
        reference, separator, reason = rest.partition(" ")
        if not separator:
            raise ReviewMemoryError("reason is required")
        return FindingFeedbackCommand(
            kind="finding",
            decision="false_positive",
            local_reference=_local_reference(reference),
            reason=_reason(reason),
        )

    if normalized_verb == "intentional":
        reference, separator, tail = rest.partition(" ")
        if not separator:
            raise ReviewMemoryError("intentional feedback requires an ADR id")
        raw_adr, separator, reason = tail.strip().partition(" ")
        if not separator:
            raise ReviewMemoryError("intentional feedback requires an ADR id and reason")
        return FindingFeedbackCommand(
            kind="finding",
            decision="intentional_by_design",
            local_reference=_local_reference(reference),
            adr_id=_adr_id(raw_adr),
            reason=_reason(reason),
        )

    if normalized_verb == "feedback":
        category, separator, reason = rest.partition(" ")
        category = category.strip().lower().rstrip(":")
        if category != "missed":
            raise ReviewMemoryError("unknown review feedback category")
        if not separator:
            raise ReviewMemoryError("reason is required")
        if reason.startswith(":"):
            reason = reason[1:].strip()
        return ReviewQualityFeedbackCommand(
            kind="review_quality",
            category="missed_issue",
            reason=_reason(reason),
        )

    raise ReviewMemoryError("unsupported review feedback command")
