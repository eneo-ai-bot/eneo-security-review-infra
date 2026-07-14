"""Parse and synthesize bounded unified-diff text by exact changed path.

When GitHub refuses to render a whole-PR diff (HTTP 406 on large pull requests),
the reviewer falls back to the per-file ``patch`` hunks that the PR-files endpoint
already returns. Those hunks carry no ``diff --git``/``---``/``+++`` framing, so
this module reconstructs that framing per file. It also owns parsing and packing
GitHub's rendered whole-PR diff, so path selection and coverage accounting share
one exact block boundary. Files whose patch GitHub omitted (too large / binary)
return ``None`` so the caller can point the reviewer at the file-read tool.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from .changed_files import ChangedFile
    from .memory_validation import ReviewMemoryError, normalize_path
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from changed_files import ChangedFile
    from memory_validation import ReviewMemoryError, normalize_path


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


@dataclass(frozen=True)
class _DiffChunk:
    path: str
    text: str


@dataclass(frozen=True)
class _RightSideHunk:
    start_line: int
    end_line: int
    added_lines: frozenset[int]


_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>[0-9]+)(?:,(?P<old_count>[0-9]+))? "
    r"\+(?P<new_start>[0-9]+)(?:,(?P<new_count>[0-9]+))? @@(?: .*)?$"
)
_NO_NEWLINE_MARKER = "\\ No newline at end of file"


def _parse_right_side_hunks(patch: str) -> list[_RightSideHunk] | None:
    """Parse GitHub's per-file patch, rejecting incomplete or malformed hunks."""
    lines = patch.splitlines()
    if not lines:
        return None

    hunks: list[_RightSideHunk] = []
    index = 0
    while index < len(lines):
        header = _HUNK_HEADER_RE.fullmatch(lines[index])
        if header is None:
            return None

        old_start = int(header.group("old_start"))
        old_count = int(header.group("old_count") or "1")
        new_start = int(header.group("new_start"))
        new_count = int(header.group("new_count") or "1")
        if (old_count > 0 and old_start == 0) or (
            new_count > 0 and new_start == 0
        ):
            return None

        index += 1
        old_seen = 0
        new_seen = 0
        new_line = new_start
        added_lines: set[int] = set()
        previous_was_content = False

        while index < len(lines) and not lines[index].startswith("@@"):
            line = lines[index]
            if line == _NO_NEWLINE_MARKER:
                if not previous_was_content:
                    return None
                previous_was_content = False
                index += 1
                continue
            if not line or line[0] not in {" ", "+", "-"}:
                return None

            prefix = line[0]
            if prefix in {" ", "-"}:
                old_seen += 1
            if prefix in {" ", "+"}:
                if prefix == "+":
                    added_lines.add(new_line)
                new_seen += 1
                new_line += 1
            if old_seen > old_count or new_seen > new_count:
                return None

            previous_was_content = True
            index += 1

        if old_seen != old_count or new_seen != new_count:
            return None

        hunks.append(
            _RightSideHunk(
                start_line=new_start,
                end_line=new_start + new_count - 1,
                added_lines=frozenset(added_lines),
            )
        )

    return hunks or None


def is_suggestible_right_side_range(
    patch: str | None, *, start_line: int, end_line: int
) -> bool:
    """Return whether a RIGHT-side range is safe to anchor as a suggestion.

    The inclusive range must be fully contained in one valid hunk and touch at
    least one added line. Context-only ranges are intentionally ineligible.
    ``patch`` is the hunk-only per-file patch returned by GitHub's PR-files API.
    """
    if (
        patch is None
        or isinstance(start_line, bool)
        or isinstance(end_line, bool)
        or start_line < 1
        or end_line < start_line
    ):
        return False

    hunks = _parse_right_side_hunks(patch)
    if hunks is None:
        return False

    matches = [
        hunk
        for hunk in hunks
        if hunk.start_line <= start_line and end_line <= hunk.end_line
    ]
    if len(matches) != 1:
        return False
    return any(start_line <= line <= end_line for line in matches[0].added_lines)


_GIT_ESCAPE_BYTES = {
    "a": 0x07,
    "b": 0x08,
    "t": 0x09,
    "n": 0x0A,
    "v": 0x0B,
    "f": 0x0C,
    "r": 0x0D,
    '"': 0x22,
    "\\": 0x5C,
}


def _parse_quoted_git_token(value: str, start: int = 0) -> tuple[str, int] | None:
    """Decode one Git C-quoted path token and return its end position."""
    if start >= len(value) or value[start] != '"':
        return None
    decoded = bytearray()
    index = start + 1
    while index < len(value):
        character = value[index]
        if character == '"':
            try:
                return decoded.decode("utf-8"), index + 1
            except UnicodeDecodeError:
                return None
        if character != "\\":
            decoded.extend(character.encode("utf-8"))
            index += 1
            continue

        index += 1
        if index >= len(value):
            return None
        escaped = value[index]
        if escaped in _GIT_ESCAPE_BYTES:
            decoded.append(_GIT_ESCAPE_BYTES[escaped])
            index += 1
            continue
        if escaped in "01234567":
            end = index + 1
            while end < len(value) and end < index + 3 and value[end] in "01234567":
                end += 1
            byte = int(value[index:end], 8)
            if byte > 0xFF:
                return None
            decoded.append(byte)
            index = end
            continue
        return None
    return None


def _decode_git_path(value: str) -> str | None:
    """Decode one complete quoted or unquoted Git path value."""
    if value.startswith('"'):
        parsed = _parse_quoted_git_token(value)
        if parsed is None:
            return None
        decoded, end = parsed
        if value[end:].strip():
            return None
        return decoded
    # Marker lines may carry a tab-separated timestamp. Git quotes real tabs in
    # paths, so a literal tab is safe to treat as the metadata boundary here.
    return value.split("\t", 1)[0].rstrip()


def _validated_path(value: str, *, prefix: str = "") -> str | None:
    decoded = _decode_git_path(value)
    if decoded is None or (prefix and not decoded.startswith(prefix)):
        return None
    candidate = decoded[len(prefix) :] if prefix else decoded
    try:
        return normalize_path(candidate)
    except ReviewMemoryError:
        return None


def _header_destination_path(header: str) -> str | None:
    prefix = "diff --git "
    if not header.startswith(prefix):
        return None
    payload = header[len(prefix) :]
    if payload.startswith('"'):
        old_token = _parse_quoted_git_token(payload)
        if old_token is None:
            return None
        _, old_end = old_token
        new_start = old_end
        while new_start < len(payload) and payload[new_start].isspace():
            new_start += 1
        new_token = _parse_quoted_git_token(payload, new_start)
        if new_token is None:
            return None
        new_path, new_end = new_token
        if payload[new_end:].strip() or not new_path.startswith("b/"):
            return None
        return _validated_path(new_path, prefix="b/")

    if not payload.startswith("a/"):
        return None
    markers: list[int] = []
    offset = 0
    while True:
        marker = payload.find(" b/", offset)
        if marker < 0:
            break
        markers.append(marker)
        offset = marker + 1
    if not markers:
        return None

    # Git leaves spaces unquoted in diff headers. For ordinary modifications,
    # the real separator is the candidate where the a/ and b/ paths are equal;
    # this also handles names containing the literal substring " b/".
    equal_candidates = [
        marker
        for marker in markers
        if payload[2:marker] == payload[marker + len(" b/") :]
    ]
    marker = equal_candidates[0] if len(equal_candidates) == 1 else markers[-1]
    return _validated_path(payload[marker + 1 :], prefix="b/")


def _chunk_destination_path(chunk: str) -> str | None:
    """Resolve the registered (post-rename) path for one rendered diff block."""
    old_marker: str | None = None
    new_marker: str | None = None
    for line in chunk.splitlines()[1:]:
        if line.startswith("@@"):
            break
        if line.startswith("rename to "):
            return _validated_path(line[len("rename to ") :])
        if line.startswith("copy to "):
            return _validated_path(line[len("copy to ") :])
        if line.startswith("--- "):
            old_marker = line[len("--- ") :]
        elif line.startswith("+++ "):
            new_marker = line[len("+++ ") :]

    if new_marker and new_marker != "/dev/null":
        parsed = _validated_path(new_marker, prefix="b/")
        if parsed is not None:
            return parsed
    if new_marker == "/dev/null" and old_marker and old_marker != "/dev/null":
        parsed = _validated_path(old_marker, prefix="a/")
        if parsed is not None:
            return parsed
    return _header_destination_path(chunk.splitlines()[0] if chunk else "")


def _rendered_chunks(text: str) -> list[_DiffChunk]:
    starts = [
        match.start()
        for match in re.finditer(r"^diff --git ", text, flags=re.MULTILINE)
    ]
    chunks: list[_DiffChunk] = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(text)
        chunk_text = text[start:end]
        path = _chunk_destination_path(chunk_text)
        if path is not None:
            chunks.append(_DiffChunk(path=path, text=chunk_text))
    return chunks


def _assemble_chunks(
    chunks: list[_DiffChunk],
    *,
    only_path: str | None,
    max_chars: int,
    unavailable_paths: list[str] | None = None,
) -> AssembledDiff:
    unavailable = unavailable_paths or []
    if only_path is not None:
        match = next((chunk for chunk in chunks if chunk.path == only_path), None)
        if match is None:
            return AssembledDiff(
                "",
                [],
                [],
                unavailable,
                False,
                path_present=only_path in unavailable,
            )
        if len(match.text) > max_chars:
            return AssembledDiff(
                match.text[:max_chars],
                [],
                [only_path],
                unavailable,
                False,
                path_present=True,
            )
        return AssembledDiff(
            match.text,
            [only_path],
            [],
            unavailable,
            False,
            path_present=True,
        )

    parts: list[str] = []
    exposed: list[str] = []
    truncated: list[str] = []
    used = 0
    more = False
    for chunk in chunks:
        if used + len(chunk.text) > max_chars:
            if not parts:
                parts.append(chunk.text[:max_chars])
                truncated.append(chunk.path)
            more = True
            break
        parts.append(chunk.text)
        if chunk.path not in exposed:
            exposed.append(chunk.path)
        used += len(chunk.text)
    return AssembledDiff(
        "".join(parts),
        exposed,
        truncated,
        unavailable,
        more,
        path_present=True,
    )


def assemble_rendered_diff(
    text: str, *, only_path: str | None, max_chars: int
) -> AssembledDiff:
    """Select and pack exact file blocks from a complete GitHub-rendered diff."""
    return _assemble_chunks(
        _rendered_chunks(text), only_path=only_path, max_chars=max_chars
    )


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
    chunks: list[_DiffChunk] = []
    unavailable: list[str] = []
    for changed_file in files:
        synthesized = synthesize_file_diff(changed_file)
        if synthesized is None:
            unavailable.append(changed_file["path"])
            continue
        chunks.append(_DiffChunk(path=changed_file["path"], text=synthesized))
    return _assemble_chunks(
        chunks,
        only_path=only_path,
        max_chars=max_chars,
        unavailable_paths=unavailable,
    )
