"""Synthesize unified-diff text from per-file patches.

When GitHub refuses to render a whole-PR diff (HTTP 406 on large pull requests),
the reviewer falls back to the per-file ``patch`` hunks that the PR-files endpoint
already returns. Those hunks carry no ``diff --git``/``---``/``+++`` framing, so
this module reconstructs that framing per file — keeping the output byte-for-byte
compatible with the existing ``_filter_diff`` / ``_diff_paths`` consumers, which
key on the ``diff --git ... b/{path}`` header line. Files whose patch GitHub
omitted (too large / binary) return ``None`` so the caller can mark them
unavailable and point the reviewer at the file-read tool instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from .changed_files import ChangedFile


@dataclass(frozen=True)
class AssembledDiff:
    text: str
    # Files whose full synthesized diff was returned (complete diff exposure).
    exposed_paths: list[str]
    # Files included but cut at the byte budget (genuine per-path truncation).
    truncated_paths: list[str]
    # Files whose patch GitHub omitted (binary / too large); need eneo_pr_file.
    unavailable_paths: list[str]
    # Other files were left out entirely for budget — request them via path=.
    # This is a RESPONSE-level signal and must NOT mark the included files as
    # truncated, or their coverage could never complete.
    more_paths_available: bool
    path_present: bool


def synthesize_file_diff(changed_file: ChangedFile) -> str | None:
    path = changed_file["path"]
    previous = changed_file["previous_path"] or path
    status = changed_file["status"]
    header = f"diff --git a/{previous} b/{path}\n"

    if changed_file["patch_state"] == "rename_only":
        return f"{header}rename from {previous}\nrename to {path}\n"

    patch = changed_file["patch"]
    if changed_file["patch_state"] != "available" or patch is None:
        return None

    old_marker = "/dev/null" if status == "added" else f"a/{previous}"
    new_marker = "/dev/null" if status == "removed" else f"b/{path}"
    body = patch if patch.endswith("\n") else patch + "\n"
    return f"{header}--- {old_marker}\n+++ {new_marker}\n{body}"


def assemble_fallback_diff(
    files: list[ChangedFile], *, only_path: str | None, max_chars: int
) -> AssembledDiff:
    """Pack per-file synthesized diffs for the 406 fallback.

    With ``only_path`` set, returns just that file's diff (or marks it
    unavailable / not present). Otherwise packs whole-file diffs in the files'
    registered order up to ``max_chars``; files past the budget set
    ``more_paths_available`` so the reviewer can request them individually.
    Files whose patch GitHub omitted are reported in ``unavailable_paths`` and
    never silently dropped.
    """
    if only_path is not None:
        match = next((f for f in files if f["path"] == only_path), None)
        if match is None:
            return AssembledDiff("", [], [], [], False, path_present=False)
        synthesized = synthesize_file_diff(match)
        if synthesized is None:
            return AssembledDiff("", [], [], [only_path], False, path_present=True)
        if len(synthesized) > max_chars:
            return AssembledDiff(
                synthesized[:max_chars], [], [only_path], [], False, path_present=True
            )
        return AssembledDiff(
            synthesized, [only_path], [], [], False, path_present=True
        )

    parts: list[str] = []
    exposed: list[str] = []
    truncated: list[str] = []
    unavailable: list[str] = []
    used = 0
    more = False
    for changed_file in files:
        synthesized = synthesize_file_diff(changed_file)
        if synthesized is None:
            unavailable.append(changed_file["path"])
            continue
        if used + len(synthesized) > max_chars:
            if not parts:
                # A single file larger than the budget is served truncated so the
                # reviewer always gets forward progress rather than an empty diff.
                parts.append(synthesized[:max_chars])
                truncated.append(changed_file["path"])
            more = True
            break
        parts.append(synthesized)
        exposed.append(changed_file["path"])
        used += len(synthesized)
    return AssembledDiff(
        "".join(parts), exposed, truncated, unavailable, more, path_present=True
    )
