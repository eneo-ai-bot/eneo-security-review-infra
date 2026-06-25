"""Pure Markdown rendering for published Eneo PR reviews."""

from __future__ import annotations

from typing import Any, Literal, Sequence, TypedDict

try:
    from .feedback_contract import feedback_templates
    from .memory_validation import (
        SEVERITY_ORDER,
        SEVERITY_PRIORITY,
        compact_text,
        local_reference_number,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from feedback_contract import feedback_templates
    from memory_validation import (
        SEVERITY_ORDER,
        SEVERITY_PRIORITY,
        compact_text,
        local_reference_number,
    )


class PublishedFinding(TypedDict):
    local_reference: str
    fingerprint: str
    observation_id: int | None
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
    observation_id: int | None
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


def severity_label(severity: str) -> str:
    return f"{severity} (P{SEVERITY_PRIORITY[severity]})"


def severity_summary(findings: Sequence[PublishedFinding]) -> str:
    if not findings:
        return "I did not identify any current in-scope findings in this review."
    total = len(findings)
    counts = {severity: 0 for severity in SEVERITY_ORDER}
    for item in findings:
        counts[str(item["severity"])] += 1
    parts = [
        f"{counts[severity]} {severity_label(severity)}"
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


def joined_refs(items: Sequence[str]) -> str:
    ordered = ordered_refs(items)
    if not ordered:
        return ""
    if len(ordered) == 1:
        return ordered[0]
    if len(ordered) == 2:
        return f"{ordered[0]} and {ordered[1]}"
    return f"{', '.join(ordered[:-1])}, and {ordered[-1]}"


def lifecycle_summary(
    *,
    findings: Sequence[PublishedFinding],
    closed: Sequence[ClosedFinding],
    still_present: Sequence[str],
    partially_resolved: Sequence[str],
    new_refs: Sequence[str],
    needs_recheck: Sequence[str],
) -> str:
    if not (closed or still_present or partially_resolved or new_refs or needs_recheck):
        return severity_summary(findings)

    clauses: list[str] = []
    if closed:
        grouped = _closed_by_verdict(closed)
        if grouped["resolved"]:
            clauses.append(
                f"{joined_refs([item['local_reference'] for item in grouped['resolved']])} resolved"
            )
        if grouped["invalidated"]:
            clauses.append(
                f"{joined_refs([item['local_reference'] for item in grouped['invalidated']])} withdrawn after recheck"
            )
        if grouped["suppressed"]:
            clauses.append(
                f"{joined_refs([item['local_reference'] for item in grouped['suppressed']])} suppressed by human decision"
            )
    if still_present:
        clauses.append(f"{joined_refs(still_present)} still present")
    if partially_resolved:
        clauses.append(f"{joined_refs(partially_resolved)} partially resolved")
    if needs_recheck:
        clauses.append(f"{joined_refs(needs_recheck)} needs recheck")
    if new_refs:
        clauses.append(f"{joined_refs(new_refs)} new")

    detail = "; ".join(clauses)
    return f"I rechecked the latest commit. {detail}. {severity_summary(findings)}"


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
        "Review and address all current findings from the Eneo PR review.",
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
                    f"{item['local_reference']} - {severity_label(item['severity'])} "
                    f"- {item['category']}"
                ),
                f"Location: {safe_text(item['path'], maximum=500)}:{item['line']}",
                f"Problem: {safe_text(item['title'], maximum=220)}",
                f"Impact: {safe_text(item['impact'], maximum=420)}",
                f"Suggested approach: {safe_text(item['smallest_fix'], maximum=520)}",
                f"Reviewer checks: {verification}",
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


def render_feedback_help(findings: Sequence[PublishedFinding]) -> str:
    local_reference = findings[0]["local_reference"] if findings else None
    templates = feedback_templates(local_reference)
    lines = [
        "<details>",
        "<summary>Give feedback on this review</summary>",
        "",
    ]
    if local_reference:
        lines.extend(
            [
                (
                    "Post one command as a new top-level PR comment after "
                    "replacing the text in angle brackets."
                ),
                (
                    "Use the F reference from the relevant finding heading. "
                    "The bot reacts 👍 when feedback is recorded."
                ),
                "",
                (
                    "It does not need to be a reply to the bot comment. Do not "
                    "edit an old feedback command after posting it."
                ),
                "",
            ]
        )
    else:
        lines.extend(
            [
                "Did the review miss something important? Post this as a new top-level PR comment:",
                "",
            ]
        )

    for template in templates:
        lines.extend(
            [
                f"**{template.title}**",
                "",
                "```text",
                template.command,
                "```",
                "",
            ]
        )

    if not local_reference:
        lines.extend(["The bot reacts 👍 when feedback is recorded.", ""])
    lines.append("</details>")
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
    feedback_enabled: bool = False,
) -> str:
    current = ordered_findings(findings)
    lines = ["## Eneo AI code & security review", ""]
    lines.extend(
        [
            lifecycle_summary(
                findings=current,
                closed=closed,
                still_present=still_present,
                partially_resolved=partially_resolved,
                new_refs=new_refs,
                needs_recheck=needs_recheck,
            ),
            "",
        ]
    )

    for item in current:
        location = inline_code(f"{item['path']}:{item['line']}", maximum=520)
        lines.extend(
            [
                (
                    f"### {item['local_reference']} · {severity_label(item['severity'])}: "
                    f"{safe_text(item['title'], maximum=160)}"
                ),
                f"{location} · {item['category']}",
                "",
                safe_text(item["evidence"], maximum=900),
                "",
                f"**Impact:** {safe_text(item['impact'], maximum=700)}",
                "",
                f"**Suggested change:** {safe_text(item['smallest_fix'], maximum=700)}",
                "",
                f"**Reviewer checks:** {safe_text(item['disproof_checks'], maximum=500)}",
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
            if item["verdict"] == "invalidated":
                label = "withdrawn after recheck"
            line = f"- {item['local_reference']} - {label}"
            if title:
                line += f": {title}"
            if evidence:
                line += f" ({evidence})"
            lines.append(line)
        lines.extend(["", "</details>", ""])

    if current:
        lines.extend([render_fix_brief(repository, pr_number, head_sha, current), ""])

    if feedback_enabled:
        lines.extend([render_feedback_help(current), ""])

    lines.extend(["<!--", "eneo-review:", f"head={head_sha}"])
    for item in current:
        lines.append(f"{item['local_reference']}={item['fingerprint']}")
    lines.extend(["-->", ""])
    return "\n".join(lines).rstrip() + "\n"
