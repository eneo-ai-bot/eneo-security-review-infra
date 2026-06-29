"""Validation, constants, and small value helpers for review memory."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Literal, get_args

DEFAULT_POLICY_REVISION = "policy-v1"
SEVERITY_ORDER = ("Critical", "High", "Medium", "Low")
SEVERITY_PRIORITY = {severity: index for index, severity in enumerate(SEVERITY_ORDER)}
SEVERITY_SCORE_GATES = {
    severity: 8 if severity in {"Critical", "High"} else 7
    for severity in SEVERITY_ORDER
}
SEVERITIES = set(SEVERITY_ORDER)
LOWER_PRIORITY_SEVERITIES = {"Medium", "Low"}
MAX_FINDINGS_PER_REVIEW = 200
PriorFindingVerdictValue = Literal[
    "resolved",
    "still_present",
    "partially_resolved",
    "invalidated",
    "suppressed",
    "not_checked",
]
PRIOR_FINDING_VERDICTS = get_args(PriorFindingVerdictValue)
CATEGORIES = {
    "security",
    "correctness",
    "reliability",
    "contracts",
    "tests",
    "maintainability",
    "performance",
    "migration",
}
DECISIONS = {
    "false_positive",
    "intentional_by_design",
    "accepted_risk",
    "duplicate",
    "resolved",
    "reopen",
}
SUPPRESSIVE_DECISIONS = {
    "false_positive",
    "intentional_by_design",
    "accepted_risk",
    "duplicate",
}
# accepted_risk acknowledges a real risk and needs the stronger governance path,
# not the lightweight in-PR feedback loop.
FEEDBACK_DECISIONS = {"false_positive", "intentional_by_design", "reopen"}
REVIEW_FEEDBACK_CATEGORIES = {
    "useful",
    "too_verbose",
    "unclear",
    "too_speculative",
    "severity_too_high",
    "severity_too_low",
    "remediation_impractical",
    "missed_issue",
    "scope_confusion",
}
MIN_CONFIDENCE = 0.85
MIN_PUBLICATION_SCORE = min(SEVERITY_SCORE_GATES.values())
_RULE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,80}$")
HASH_RE = re.compile(r"^[0-9a-f]{40,64}$")
_ELLIPSIS = "..."


class ReviewMemoryError(ValueError):
    """Raised for invalid memory input."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None = None) -> str:
    value = value or utc_now()
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def current_policy_revision(explicit: str | None = None) -> str:
    value = explicit or os.environ.get(
        "ENEO_REVIEW_POLICY_REVISION", DEFAULT_POLICY_REVISION
    )
    return clean_text(value, field="policy_revision", maximum=120)


def clean_text(value: Any, *, field: str, maximum: int, required: bool = True) -> str:
    text = " ".join(str(value or "").strip().split())
    if required and not text:
        raise ReviewMemoryError(f"{field} is required")
    if len(text) > maximum:
        raise ReviewMemoryError(f"{field} exceeds {maximum} characters")
    return text


def compact_text(value: Any, *, maximum: int = 800) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= maximum:
        return text
    return truncate_text(text, maximum=maximum)


def truncate_text(value: str, *, maximum: int) -> str:
    if maximum <= 0:
        return ""
    if len(value) <= maximum:
        return value
    if maximum <= len(_ELLIPSIS):
        return _ELLIPSIS[:maximum]
    limit = maximum - len(_ELLIPSIS)
    # Preserve length <= maximum; use a word break only when it keeps useful text.
    floor = limit // 2
    cut = max(value.rfind(" ", 0, limit + 1), value.rfind("\n", 0, limit + 1))
    if cut <= floor:
        cut = limit
    return value[:cut].rstrip() + _ELLIPSIS


def local_reference_number(value: str) -> int:
    match = re.fullmatch(r"F([1-9][0-9]*)", str(value or "").strip().upper())
    return int(match.group(1)) if match else 0


def clean_multiline(
    value: Any, *, field: str, maximum: int, required: bool = True
) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ReviewMemoryError(f"{field} is required")
    if len(text) > maximum:
        raise ReviewMemoryError(f"{field} exceeds {maximum} characters")
    return text


def normalize_repository(repository: str) -> str:
    value = repository.strip().lower()
    if value.count("/") != 1 or not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", value):
        raise ReviewMemoryError("repository must be owner/name")
    return value


def normalize_path(path: str) -> str:
    value = path.strip().replace("\\", "/")
    if not value or value.startswith("/") or "\x00" in value:
        raise ReviewMemoryError("invalid repository path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ReviewMemoryError("repository path may not contain traversal segments")
    if len(value) > 500:
        raise ReviewMemoryError("repository path is too long")
    return value


def normalize_rule_id(rule_id: str) -> str:
    value = rule_id.strip().lower()
    if not _RULE_RE.fullmatch(value):
        raise ReviewMemoryError(
            "rule_id must be stable lower-case letters, digits, dots, dashes, or underscores"
        )
    return value


def normalize_context_hash(value: str) -> str:
    cleaned = str(value or "").strip().lower()
    if not HASH_RE.fullmatch(cleaned):
        raise ReviewMemoryError(
            "context_hash must be a 40 to 64 character hexadecimal hash"
        )
    return cleaned


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
