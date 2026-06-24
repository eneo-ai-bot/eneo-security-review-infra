"""Pure Markdown rendering for published Eneo PR reviews."""

from __future__ import annotations

from typing import Any, Sequence, TypedDict

try:
    from .memory_validation import SEVERITY_ORDER, SEVERITY_PRIORITY
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_validation import SEVERITY_ORDER, SEVERITY_PRIORITY


class PublishedFinding(TypedDict):
    local_reference: str
    fingerprint: str
    context_hash: str
    rule_id: str
    category: str
    path: str
    line: int
    title: str
    severity: str
    publication_score: int
    evidence: str
    impact: str
    smallest_fix: str


class ResolvedFinding(TypedDict):
    local_reference: str
    fingerprint: str
    context_hash: str
    title: str


def compact_text(value: Any, *, maximum: int = 800) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= maximum:
        return text
    return text[: maximum - 1].rstrip() + "..."


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
        return f"I found {total} {noun}: {parts[0]}."
    if len(parts) == 2:
        return f"I found {total} findings: {parts[0]} and {parts[1]}."
    return f"I found {total} findings: {', '.join(parts[:-1])}, and {parts[-1]}."


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
        lines.extend(
            [
                (
                    f"{item['local_reference']} - {item['severity']} / "
                    f"P{SEVERITY_PRIORITY[item['severity']]} - {item['category']}"
                ),
                f"Location: {item['path']}:{item['line']}",
                f"Problem: {compact_text(item['title'], maximum=220)}",
                f"Required outcome: {compact_text(item['impact'], maximum=420)}",
                f"Suggested approach: {compact_text(item['smallest_fix'], maximum=520)}",
                (
                    "Verification: Add or run the focused checks that prove the "
                    "demonstrated failure path."
                ),
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
    resolved: Sequence[ResolvedFinding],
    still_present: Sequence[str],
    new_refs: Sequence[str],
) -> str:
    current = ordered_findings(findings)
    lines = ["## Eneo AI code & security review", ""]
    if resolved or still_present or new_refs:
        lines.extend(["Review updated for the latest commit.", ""])
        if resolved:
            lines.append(
                "Resolved since the previous review: "
                + ", ".join(item["local_reference"] for item in resolved)
            )
        if still_present:
            lines.append("Still present: " + ", ".join(still_present))
        if new_refs:
            lines.append("New findings: " + ", ".join(new_refs))
        lines.append("")

    lines.extend([severity_summary(current), ""])

    for item in current:
        priority = f"P{SEVERITY_PRIORITY[item['severity']]}"
        lines.extend(
            [
                (
                    f"### {item['local_reference']} - {item['severity']} / {priority}: "
                    f"{compact_text(item['title'], maximum=160)}"
                ),
                f"`{item['path']}:{item['line']}` · {item['category']}",
                "",
                compact_text(item["evidence"], maximum=900),
                "",
                f"**Suggested change:** {compact_text(item['smallest_fix'], maximum=700)}",
                "",
            ]
        )

    if resolved:
        lines.extend(
            [
                "<details>",
                "<summary>Resolved since the previous review</summary>",
                "",
            ]
        )
        for item in resolved:
            lines.append(
                f"- {item['local_reference']} - {compact_text(item.get('title', ''), maximum=180)}"
            )
        lines.extend(["", "</details>", ""])

    if current:
        lines.extend([render_fix_brief(repository, pr_number, head_sha, current), ""])

    lines.extend(["<!--", "eneo-review:", f"head={head_sha}"])
    for item in current:
        lines.append(f"{item['local_reference']}={item['fingerprint']}")
    lines.extend(["-->", ""])
    return "\n".join(lines).rstrip() + "\n"
