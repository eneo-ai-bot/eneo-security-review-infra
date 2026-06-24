"""Pure Markdown rendering for published Eneo PR reviews."""

from __future__ import annotations

from typing import Any, Literal, Sequence, TypedDict

try:
    from .memory_validation import (
        SEVERITY_ORDER,
        SEVERITY_PRIORITY,
        compact_text,
        local_reference_number,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_validation import (
        SEVERITY_ORDER,
        SEVERITY_PRIORITY,
        compact_text,
        local_reference_number,
    )


class PublishedFinding(TypedDict):
    local_reference: str
    fingerprint: str
    context_hash: str
    review_status: Literal["observed", "carried_forward"]
    rule_id: str
    category: str
    path: str
    line: int
    title: str
    severity: str
    publication_score: int
    evidence: str
    disproof_checks: str
    impact: str
    smallest_fix: str


class ClosedFinding(TypedDict):
    local_reference: str
    fingerprint: str
    context_hash: str
    verdict: Literal["resolved", "invalidated", "suppressed"]
    title: str
    evidence: str


def safe_text(value: Any, *, maximum: int = 800) -> str:
    text = compact_text(value, maximum=maximum)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "'")
    )


def inline_code(value: Any, *, maximum: int = 800) -> str:
    return f"`{safe_text(value, maximum=maximum)}`"


def severity_summary(findings: Sequence[PublishedFinding]) -> str:
    if not findings:
        return "No current findings survived this review."
    total = len(findings)
    counts = {severity: 0 for severity in SEVERITY_ORDER}
    for item in findings:
        counts[str(item["severity"])] += 1
    parts = [
        f"{counts[severity]} {severity} / P{SEVERITY_PRIORITY[severity]}"
        for severity in SEVERITY_ORDER
        if counts[severity]
    ]
    noun = "finding" if total == 1 else "findings"
    if len(parts) == 1:
        verb = "is" if total == 1 else "are"
        return f"There {verb} {total} current {noun}: {parts[0]}."
    if len(parts) == 2:
        return f"There are {total} current findings: {parts[0]} and {parts[1]}."
    return f"There are {total} current findings: {', '.join(parts[:-1])}, and {parts[-1]}."


def ordered_findings(items: Sequence[PublishedFinding]) -> list[PublishedFinding]:
    return sorted(
        items,
        key=lambda item: (
            SEVERITY_PRIORITY.get(str(item["severity"]), 99),
            -int(item.get("publication_score", 0) or 0),
            str(item.get("rule_id", "")),
            str(item.get("fingerprint", "")),
        ),
    )


def ordered_refs(items: Sequence[str]) -> list[str]:
    return sorted(items, key=local_reference_number)


def _closed_by_verdict(
    items: Sequence[ClosedFinding],
) -> dict[str, list[ClosedFinding]]:
    grouped: dict[str, list[ClosedFinding]] = {
        "resolved": [],
        "invalidated": [],
        "suppressed": [],
    }
    for item in items:
        grouped[item["verdict"]].append(item)
    for verdict in grouped:
        grouped[verdict].sort(
            key=lambda item: local_reference_number(item["local_reference"])
        )
    return grouped


def render_fix_brief(
    repository: str,
    pr_number: int,
    head_sha: str,
    findings: Sequence[PublishedFinding],
) -> str:
    lines = [
        "<details>",
        "<summary>Copyable fix brief for a coding agent</summary>",
        "",
        "```text",
        "Task:",
        "Address all confirmed findings from the Eneo PR review.",
        "",
        "Review basis:",
        f"{repository} PR #{pr_number} at commit {head_sha[:7]}.",
        "",
        "Before changing code:",
        "Re-check every finding against the current PR head. Skip anything already fixed",
        "and explain why. Do not blindly apply this brief if the code has changed.",
        "",
        "Findings:",
        "",
    ]
    for item in findings:
        verification = safe_text(item["disproof_checks"], maximum=420)
        if not verification:
            verification = (
                "Add or run the focused checks that prove the demonstrated failure path."
            )
        lines.extend(
            [
                (
                    f"{item['local_reference']} - {item['severity']} / "
                    f"P{SEVERITY_PRIORITY[item['severity']]} - {item['category']}"
                ),
                f"Location: {safe_text(item['path'], maximum=500)}:{item['line']}",
                f"Problem: {safe_text(item['title'], maximum=220)}",
                f"Required outcome: {safe_text(item['impact'], maximum=420)}",
                f"Suggested approach: {safe_text(item['smallest_fix'], maximum=520)}",
                f"Verification: {verification}",
                "",
            ]
        )
    lines.extend(
        [
            "Constraints:",
            "- Reuse existing Eneo abstractions where they fit.",
            "- Avoid unrelated refactoring.",
            "- Do not weaken validation, authorization, tenant isolation, or error handling.",
            "",
            "Completion:",
            "Run the focused tests, relevant type checks, and formatting checks. Summarize",
            "what changed and identify any finding that was not implemented.",
            "```",
            "",
            "</details>",
        ]
    )
    return "\n".join(lines)


def render_review_markdown(
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    findings: Sequence[PublishedFinding],
    closed: Sequence[ClosedFinding],
    still_present: Sequence[str],
    partially_resolved: Sequence[str],
    new_refs: Sequence[str],
    needs_recheck: Sequence[str],
) -> str:
    current = ordered_findings(findings)
    lines = ["## Eneo AI code & security review", ""]
    if closed or still_present or partially_resolved or new_refs or needs_recheck:
        lines.extend(["Review updated for the latest commit.", ""])
        if closed:
            grouped = _closed_by_verdict(closed)
            if grouped["resolved"]:
                lines.append(
                    "Resolved since the previous review: "
                    + ", ".join(item["local_reference"] for item in grouped["resolved"])
                )
            if grouped["invalidated"]:
                lines.append(
                    "Invalidated since the previous review: "
                    + ", ".join(
                        item["local_reference"] for item in grouped["invalidated"]
                    )
                )
            if grouped["suppressed"]:
                lines.append(
                    "Suppressed by human decision: "
                    + ", ".join(item["local_reference"] for item in grouped["suppressed"])
                )
        if still_present:
            lines.append("Still present: " + ", ".join(ordered_refs(still_present)))
        if partially_resolved:
            lines.append(
                "Partially resolved: " + ", ".join(ordered_refs(partially_resolved))
            )
        if needs_recheck:
            lines.append("Needs recheck: " + ", ".join(ordered_refs(needs_recheck)))
        if new_refs:
            lines.append("New findings: " + ", ".join(ordered_refs(new_refs)))
        lines.append("")

    lines.extend([severity_summary(current), ""])

    for item in current:
        priority = f"P{SEVERITY_PRIORITY[item['severity']]}"
        location = inline_code(f"{item['path']}:{item['line']}", maximum=520)
        lines.extend(
            [
                (
                    f"### {item['local_reference']} - {item['severity']} / {priority}: "
                    f"{safe_text(item['title'], maximum=160)}"
                ),
                f"{location} · {item['category']}",
                "",
                safe_text(item["evidence"], maximum=900),
                "",
                f"**Suggested change:** {safe_text(item['smallest_fix'], maximum=700)}",
                "",
            ]
        )
        if item["review_status"] == "carried_forward":
            lines.extend(
                [
                    (
                        "**Recheck needed:** This previous finding was not explicitly "
                        "observed in the latest run, so it remains current until verified."
                    ),
                    "",
                ]
            )

    if closed:
        lines.extend(
            [
                "<details>",
                "<summary>Closed since the previous review</summary>",
                "",
            ]
        )
        for item in closed:
            title = safe_text(item.get("title", ""), maximum=180)
            evidence = safe_text(item.get("evidence", ""), maximum=320)
            label = item["verdict"].replace("_", " ")
            line = f"- {item['local_reference']} - {label}"
            if title:
                line += f": {title}"
            if evidence:
                line += f" ({evidence})"
            lines.append(line)
        lines.extend(["", "</details>", ""])

    if current:
        lines.extend([render_fix_brief(repository, pr_number, head_sha, current), ""])

    lines.extend(["<!--", "eneo-review:", f"head={head_sha}"])
    for item in current:
        lines.append(f"{item['local_reference']}={item['fingerprint']}")
    lines.extend(["-->", ""])
    return "\n".join(lines).rstrip() + "\n"
