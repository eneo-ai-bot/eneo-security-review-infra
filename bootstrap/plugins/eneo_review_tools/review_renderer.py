"""Pure Markdown rendering for published PR reviews."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Literal, Sequence, TypedDict, cast
import urllib.parse

try:
    from .feedback_contract import feedback_templates
    from .memory_validation import (
        SEVERITY_ORDER,
        SEVERITY_PRIORITY,
        compact_text,
        local_reference_number,
    )
    from .review_identity import (
        FIX_BRIEF_PROJECT_CONSTRAINT,
        FIX_BRIEF_TASK,
        REVIEW_COMMENT_TITLE,
    )
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from feedback_contract import feedback_templates
    from memory_validation import (
        SEVERITY_ORDER,
        SEVERITY_PRIORITY,
        compact_text,
        local_reference_number,
    )
    from review_identity import (  # type: ignore[no-redef]
        FIX_BRIEF_PROJECT_CONSTRAINT,
        FIX_BRIEF_TASK,
        REVIEW_COMMENT_TITLE,
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


class ReviewCoverageSummary(TypedDict):
    state: Literal["complete", "incomplete", "unknown"]
    changed_paths: int
    diff_exposed: int
    context_reads: int
    unavailable: int
    diff_truncated: int
    coverage_hash: str
    unavailable_paths: list[str]
    truncated_paths: list[str]


ReviewBlockKind = Literal[
    "header",
    "finding",
    "closed_history",
    "fix_brief",
    "feedback_help",
    "metadata",
]


@dataclass(frozen=True)
class ReviewBlock:
    kind: ReviewBlockKind
    markdown: str


@dataclass(frozen=True)
class RenderedReview:
    markdown: str
    blocks: tuple[ReviewBlock, ...]


_BLOCK_KINDS = frozenset(
    {
        "header",
        "finding",
        "closed_history",
        "fix_brief",
        "feedback_help",
        "metadata",
    }
)
_FIX_BRIEF_FINDINGS_PER_BLOCK = 10


def review_markdown_from_blocks(blocks: Sequence[ReviewBlock]) -> str:
    body = "\n\n".join(
        block.markdown.rstrip() for block in blocks if block.markdown.strip()
    )
    return body.rstrip() + "\n" if body else ""


def review_blocks_to_json(blocks: Sequence[ReviewBlock]) -> str:
    return json.dumps(
        [{"kind": block.kind, "markdown": block.markdown} for block in blocks],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def review_blocks_from_json(
    raw: str, *, fallback_markdown: str = ""
) -> tuple[ReviewBlock, ...]:
    if not raw.strip():
        return (
            (ReviewBlock(kind="header", markdown=fallback_markdown),)
            if fallback_markdown
            else ()
        )
    try:
        value: object = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("rendered_blocks_json is invalid JSON") from exc
    if not isinstance(value, list):
        raise ValueError("rendered_blocks_json must be a list")
    blocks: list[ReviewBlock] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, dict):
            raise ValueError(f"rendered_blocks_json[{index}] must be an object")
        item_map = cast(dict[object, object], item)
        kind = item_map.get("kind")
        markdown = item_map.get("markdown")
        if kind not in _BLOCK_KINDS:
            raise ValueError(f"rendered_blocks_json[{index}].kind is unsupported")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError(f"rendered_blocks_json[{index}].markdown must be text")
        blocks.append(ReviewBlock(kind=cast(ReviewBlockKind, kind), markdown=markdown))
    return tuple(blocks)


def safe_text(value: Any, *, maximum: int = 800) -> str:
    text = compact_text(value, maximum=maximum)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "'")
    )


def safe_fenced_text(value: Any, *, maximum: int = 800) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    cleaned = "".join(
        character
        if character in {"\n", "\t"} or ord(character) >= 32
        else " "
        for character in text
    )
    if len(cleaned) > maximum:
        cleaned = cleaned[: maximum - 1].rstrip() + "..."
    return cleaned.replace("```", "` ` `")


def inline_code(value: Any, *, maximum: int = 800) -> str:
    return f"`{safe_text(value, maximum=maximum)}`"


def source_link(repository: str, head_sha: str, path: str, line: int) -> str:
    repository_part = urllib.parse.quote(repository, safe="/")
    path_part = urllib.parse.quote(path, safe="/")
    label = safe_text(f"{path}:{line}", maximum=520)
    return f"[`{label}`](https://github.com/{repository_part}/blob/{head_sha}/{path_part}#L{line})"


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


def ref_clause(items: Sequence[str], singular: str, plural: str) -> str:
    ordered = ordered_refs(items)
    if not ordered:
        return ""
    suffix = singular if len(ordered) == 1 else plural
    return f"{joined_refs(ordered)} {suffix}"


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
                ref_clause(
                    [item["local_reference"] for item in grouped["resolved"]],
                    "resolved",
                    "resolved",
                )
            )
        if grouped["invalidated"]:
            clauses.append(
                ref_clause(
                    [item["local_reference"] for item in grouped["invalidated"]],
                    "withdrawn after recheck",
                    "withdrawn after recheck",
                )
            )
        if grouped["suppressed"]:
            clauses.append(
                ref_clause(
                    [item["local_reference"] for item in grouped["suppressed"]],
                    "suppressed by human decision",
                    "suppressed by human decision",
                )
            )
    if still_present:
        clauses.append(ref_clause(still_present, "still present", "still present"))
    if partially_resolved:
        clauses.append(
            ref_clause(partially_resolved, "partially resolved", "partially resolved")
        )
    if needs_recheck:
        clauses.append(ref_clause(needs_recheck, "needs recheck", "need recheck"))
    if new_refs:
        clauses.append(ref_clause(new_refs, "is new", "are new"))

    detail = " · ".join(clauses)
    return f"**Since the previous review:** {detail}\n\n{severity_summary(findings)}"


def coverage_summary_line(coverage: ReviewCoverageSummary | None) -> str:
    if coverage is None or coverage["state"] == "unknown":
        return ""
    if coverage["state"] == "complete":
        return (
            f"<sub>Review context: all {coverage['changed_paths']} changed paths "
            f"were available in the diff; {coverage['context_reads']} received "
            "additional source-context reads.</sub>"
        )
    representative = coverage["unavailable_paths"] or coverage["truncated_paths"]
    suffix = ""
    if representative:
        suffix = " Representative paths: " + ", ".join(
            safe_text(path, maximum=120) for path in representative
        ) + "."
    return (
        f"**Coverage incomplete:** {coverage['diff_exposed']} of "
        f"{coverage['changed_paths']} changed paths were available in the diff; "
        f"{coverage['unavailable']} unavailable and "
        f"{coverage['diff_truncated']} truncated.{suffix} "
        "This review is not a clean result."
    )


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


def _finding_ref_range(findings: Sequence[PublishedFinding]) -> str:
    first = findings[0]["local_reference"]
    last = findings[-1]["local_reference"]
    return first if first == last else f"{first}-{last}"


def render_fix_brief(
    repository: str,
    pr_number: int,
    head_sha: str,
    findings: Sequence[PublishedFinding],
    *,
    part_number: int = 1,
    total_parts: int = 1,
) -> str:
    summary = "Copyable fix brief for a coding agent"
    if total_parts > 1:
        summary = (
            f"{summary} ({part_number} of {total_parts}, "
            f"{_finding_ref_range(findings)})"
        )
    lines = [
        "<details>",
        f"<summary>{summary}</summary>",
        "",
        "```text",
        "Task:",
        FIX_BRIEF_TASK,
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
        verification = safe_fenced_text(item["disproof_checks"], maximum=420)
        if not verification:
            verification = (
                "Add or run the focused checks that prove the demonstrated failure path."
            )
        lines.extend(
            [
                (
                    f"{safe_fenced_text(item['local_reference'], maximum=12)} - "
                    f"{severity_label(item['severity'])} - "
                    f"{safe_fenced_text(item['category'], maximum=80)}"
                ),
                f"Location: {safe_fenced_text(item['path'], maximum=500)}:{item['line']}",
                f"Problem: {safe_fenced_text(item['title'], maximum=220)}",
                f"Impact: {safe_fenced_text(item['impact'], maximum=420)}",
                f"Suggested approach: {safe_fenced_text(item['smallest_fix'], maximum=520)}",
                f"Reviewer checks: {verification}",
                "",
            ]
        )
    lines.extend(
        [
            "Constraints:",
            FIX_BRIEF_PROJECT_CONSTRAINT,
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


def render_fix_brief_blocks(
    repository: str,
    pr_number: int,
    head_sha: str,
    findings: Sequence[PublishedFinding],
) -> list[ReviewBlock]:
    blocks: list[ReviewBlock] = []
    chunks = [
        findings[index : index + _FIX_BRIEF_FINDINGS_PER_BLOCK]
        for index in range(0, len(findings), _FIX_BRIEF_FINDINGS_PER_BLOCK)
    ]
    total = len(chunks)
    for index, chunk in enumerate(chunks, start=1):
        blocks.append(
            ReviewBlock(
                kind="fix_brief",
                markdown=render_fix_brief(
                    repository,
                    pr_number,
                    head_sha,
                    chunk,
                    part_number=index,
                    total_parts=total,
                ),
            )
        )
    return blocks


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


def render_review(
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
    coverage: ReviewCoverageSummary | None = None,
) -> RenderedReview:
    current = ordered_findings(findings)
    header_lines = [
        f"## {REVIEW_COMMENT_TITLE}",
        "",
        lifecycle_summary(
            findings=current,
            closed=closed,
            still_present=still_present,
            partially_resolved=partially_resolved,
            new_refs=new_refs,
            needs_recheck=needs_recheck,
        ),
    ]
    coverage_line = coverage_summary_line(coverage)
    if coverage_line:
        header_lines.extend(["", coverage_line])
    blocks: list[ReviewBlock] = []
    blocks.append(
        ReviewBlock(
            kind="header",
            markdown="\n".join(header_lines),
        )
    )

    for item in current:
        location = source_link(
            repository,
            head_sha,
            str(item["path"]),
            int(item["line"]),
        )
        finding_lines = [
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
        ]
        if item["review_status"] == "carried_forward":
            finding_lines.extend(
                [
                    "",
                    (
                        "**Recheck needed:** This previous finding was not explicitly "
                        "observed in the latest run, so it remains current until verified."
                    ),
                ]
            )
        blocks.append(ReviewBlock(kind="finding", markdown="\n".join(finding_lines)))

    if closed:
        closed_lines = [
            "<details>",
            "<summary>Closed since the previous review</summary>",
            "",
        ]
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
            closed_lines.append(line)
        closed_lines.extend(["", "</details>"])
        blocks.append(
            ReviewBlock(kind="closed_history", markdown="\n".join(closed_lines))
        )

    if current:
        blocks.extend(render_fix_brief_blocks(repository, pr_number, head_sha, current))

    if feedback_enabled:
        blocks.append(
            ReviewBlock(kind="feedback_help", markdown=render_feedback_help(current))
        )

    metadata_lines = ["<!--", "eneo-review:", f"head={head_sha}"]
    if coverage is not None and coverage["state"] != "unknown":
        metadata_lines.extend(
            [
                f"coverage_state={coverage['state']}",
                f"changed_paths={coverage['changed_paths']}",
                f"diff_exposed={coverage['diff_exposed']}",
                f"context_reads={coverage['context_reads']}",
                f"unavailable={coverage['unavailable']}",
                f"diff_truncated={coverage['diff_truncated']}",
                f"coverage_hash={coverage['coverage_hash']}",
            ]
        )
    for item in current:
        metadata_lines.append(f"{item['local_reference']}={item['fingerprint']}")
    metadata_lines.append("-->")
    blocks.append(ReviewBlock(kind="metadata", markdown="\n".join(metadata_lines)))

    result_blocks = tuple(blocks)
    return RenderedReview(
        markdown=review_markdown_from_blocks(result_blocks),
        blocks=result_blocks,
    )


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
    coverage: ReviewCoverageSummary | None = None,
) -> str:
    return render_review(
        repository=repository,
        pr_number=pr_number,
        head_sha=head_sha,
        findings=findings,
        closed=closed,
        still_present=still_present,
        partially_resolved=partially_resolved,
        new_refs=new_refs,
        needs_recheck=needs_recheck,
        feedback_enabled=feedback_enabled,
        coverage=coverage,
    ).markdown
