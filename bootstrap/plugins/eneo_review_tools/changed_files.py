"""Offset-safe enumeration of a pull request's changed files.

The PR-files endpoint inlines per-file ``patch`` text, so a page can exceed the
transport byte budget. GitHub couples ``page`` with ``per_page``, so the page
size must never change *within* a pass — doing so would redefine page
boundaries and silently skip or duplicate files. This owner therefore drives
enumeration as a series of full passes at a *constant* ``per_page``: if any page
overflows the byte budget, the whole pass is discarded and retried at the next
smaller page size, preserving the invariant that a successful pass yields
exactly files ``1..N`` with no gaps or duplicates. A terminal single-file pass
keeps partial progress and surfaces an honest incomplete index rather than
claiming coverage it could not obtain.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import urllib.parse
from typing import Any, Literal, Protocol, TypedDict, cast

JsonObject = dict[str, Any]

PatchState = Literal["available", "missing", "oversized", "binary", "rename_only"]
IndexState = Literal["complete", "incomplete", "api_limit", "budget_exceeded"]

# Descending page sizes tried in order; every full pass uses one constant size.
DEFAULT_PER_PAGE_SEQUENCE: tuple[int, ...] = (100, 50, 25, 10, 5, 2)
# GitHub's PR-files API lists at most this many files.
MAX_CHANGED_FILES = 3000
# Per-page transport budget for enumeration (patches inline can be large).
ENUMERATION_MAX_BYTES = 3_000_000


class RequestFn(Protocol):
    """The bounded GitHub GET used for enumeration: returns (body, truncated, headers)."""

    def __call__(
        self, endpoint: str, *, max_bytes: int
    ) -> tuple[bytes, bool, dict[str, str]]: ...


class ChangedFile(TypedDict):
    path: str
    status: str
    previous_path: str | None
    blob_sha: str
    patch: str | None
    patch_available: bool
    patch_state: PatchState
    additions: int
    deletions: int
    changes: int


@dataclass(frozen=True)
class ChangedFileIndex:
    files: list[ChangedFile]
    index_state: IndexState
    reported: int
    registered: int


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(default if value is None else value)
    except (TypeError, ValueError):
        return default


def _parse_items(raw: bytes) -> list[JsonObject]:
    decoded = json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, list):
        raise ValueError("GitHub returned an unexpected changed-files response")
    items: list[JsonObject] = []
    for entry in cast("list[Any]", decoded):
        if isinstance(entry, dict):
            items.append(cast(JsonObject, entry))
    return items


def _to_changed_file(item: JsonObject) -> ChangedFile:
    status = str(item.get("status", ""))[:40]
    changes = _int(item.get("changes"))
    raw_patch = item.get("patch")
    patch = raw_patch if isinstance(raw_patch, str) else None
    previous = str(item.get("previous_filename", ""))[:500] or None
    if patch is not None:
        patch_state: PatchState = "available"
    elif status == "renamed" and changes == 0:
        patch_state = "rename_only"
    else:
        patch_state = "missing"
    return ChangedFile(
        path=str(item.get("filename", ""))[:500],
        status=status,
        previous_path=previous,
        blob_sha=str(item.get("sha", "")).strip().lower(),
        patch=patch,
        patch_available=patch is not None,
        patch_state=patch_state,
        additions=_int(item.get("additions")),
        deletions=_int(item.get("deletions")),
        changes=changes,
    )


def _files_endpoint(owner_repo: str, number: int, per_page: int, page: int) -> str:
    return f"/repos/{owner_repo}/pulls/{number}/files?per_page={per_page}&page={page}"


def _full_pass(
    request: RequestFn,
    owner_repo: str,
    number: int,
    per_page: int,
    max_files: int,
    max_bytes: int,
) -> list[JsonObject] | None:
    """Enumerate every page at a constant ``per_page``.

    Returns the complete raw-item list, or ``None`` if any page overflowed the
    byte budget (signal to retry the whole pass at a smaller page size).
    """
    items: list[JsonObject] = []
    page = 1
    while len(items) < max_files:
        raw, truncated, _ = request(
            _files_endpoint(owner_repo, number, per_page, page), max_bytes=max_bytes
        )
        if truncated:
            return None
        page_items = _parse_items(raw)
        items.extend(page_items)
        if len(page_items) < per_page:
            break
        page += 1
    return items[:max_files]


def _terminal_pass(
    request: RequestFn,
    owner_repo: str,
    number: int,
    max_files: int,
    max_bytes: int,
) -> list[JsonObject]:
    """Single-file pass: register what fits, skip (but count) overflowed files."""
    items: list[JsonObject] = []
    page = 1
    while page <= max_files and len(items) < max_files:
        raw, truncated, _ = request(
            _files_endpoint(owner_repo, number, 1, page), max_bytes=max_bytes
        )
        if truncated:
            page += 1
            continue
        page_items = _parse_items(raw)
        if not page_items:
            break
        items.extend(page_items)
        page += 1
    return items


def _index(raw_items: list[JsonObject], reported: int, max_files: int, *, terminal: bool) -> ChangedFileIndex:
    files = [_to_changed_file(item) for item in raw_items]
    registered = len(files)
    if registered >= reported and registered > 0:
        state: IndexState = "complete"
    elif registered >= max_files:
        state = "api_limit"
    elif terminal:
        state = "budget_exceeded"
    else:
        state = "incomplete"
    return ChangedFileIndex(files=files, index_state=state, reported=reported, registered=registered)


def enumerate_changed_files(
    request: RequestFn,
    repository: str,
    number: int,
    *,
    reported: int,
    max_files: int = MAX_CHANGED_FILES,
    per_page_sequence: tuple[int, ...] = DEFAULT_PER_PAGE_SEQUENCE,
    max_bytes: int = ENUMERATION_MAX_BYTES,
) -> ChangedFileIndex:
    owner_repo = urllib.parse.quote(repository, safe="/")
    for per_page in per_page_sequence:
        raw_items = _full_pass(request, owner_repo, number, per_page, max_files, max_bytes)
        if raw_items is not None:
            return _index(raw_items, reported, max_files, terminal=False)
    raw_items = _terminal_pass(request, owner_repo, number, max_files, max_bytes)
    return _index(raw_items, reported, max_files, terminal=True)
