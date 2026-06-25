"""Shared developer-facing feedback command contract."""

from __future__ import annotations

from dataclasses import dataclass

CANONICAL_TRIGGER = "/review"
COMPATIBLE_TRIGGERS = ("/review", "@review")

FALSE_POSITIVE_PLACEHOLDER = "<what code, guard, or invariant disproves it>"
MISSED_ISSUE_PLACEHOLDER = "<what concrete issue was missed and where>"


@dataclass(frozen=True)
class FeedbackCommandTemplate:
    title: str
    command: str


def contains_placeholder(value: str) -> bool:
    return (
        FALSE_POSITIVE_PLACEHOLDER in value
        or MISSED_ISSUE_PLACEHOLDER in value
    )


def false_positive_command(local_reference: str) -> str:
    return (
        f"{CANONICAL_TRIGGER} false-positive {local_reference} because "
        f"{FALSE_POSITIVE_PLACEHOLDER}"
    )


def missed_issue_command() -> str:
    return (
        f"{CANONICAL_TRIGGER} feedback missed because {MISSED_ISSUE_PLACEHOLDER}"
    )


def feedback_templates(
    local_reference: str | None,
) -> tuple[FeedbackCommandTemplate, ...]:
    if local_reference:
        return (
            FeedbackCommandTemplate(
                title="The finding is incorrect",
                command=false_positive_command(local_reference),
            ),
            FeedbackCommandTemplate(
                title="The review missed an important issue",
                command=missed_issue_command(),
            ),
        )
    return (
        FeedbackCommandTemplate(
            title="The review missed an important issue",
            command=missed_issue_command(),
        ),
    )


def usage_lines() -> tuple[str, ...]:
    finding, missed = feedback_templates("F2")
    return (
        "Use `/review` alone to request a review, or:",
        "",
        f"- `{finding.command}`",
        f"- `{missed.command}`",
        "",
        "Post feedback as a new top-level PR comment. Do not edit an old command.",
    )
