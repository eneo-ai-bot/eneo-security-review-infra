from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from eneo_review_tools import changed_files, tools  # noqa: E402


def _files(n: int, *, with_patch: bool = True):
    out = []
    for i in range(n):
        item = {
            "filename": f"src/file_{i:04d}.py",
            "status": "modified",
            "sha": f"{i:040x}",
            "additions": 1,
            "deletions": 1,
            "changes": 2,
        }
        if with_patch:
            item["patch"] = "@@ -1 +1 @@\n-a\n+b"
        out.append(item)
    return out


def _fake_request(
    master: list[dict[str, object]],
    truncate_on: "frozenset[tuple[int, int]] | set[tuple[int, int]]" = frozenset(),
):
    """A faithful GitHub /pulls/{n}/files paginator over `master`.

    truncate_on: set of (per_page, page) pairs that report transport truncation
    (the signal the pager must treat as page byte-overflow).
    """

    def request(endpoint: str, *, max_bytes: int):
        del max_bytes
        query = parse_qs(urlparse(endpoint).query)
        per_page = int(query["per_page"][0])
        page = int(query["page"][0])
        if (per_page, page) in truncate_on:
            return (b'[{"filename":"partial', True, {})
        start = (page - 1) * per_page
        chunk = master[start : start + per_page]
        return (json.dumps(chunk).encode("utf-8"), False, {})

    return request


class ChangedFilePagerTests(unittest.TestCase):
    def test_enumerates_all_pages_past_300(self):
        master = _files(350)
        result = changed_files.enumerate_changed_files(
            _fake_request(master), "eneo-ai/eneo", 309, reported=350
        )
        self.assertEqual(len(result.files), 350)
        self.assertEqual(
            [f["path"] for f in result.files], [m["filename"] for m in master]
        )
        self.assertEqual(result.index_state, "complete")
        self.assertEqual(result.registered, 350)

    def test_overflow_restarts_at_smaller_per_page_gap_free(self):
        master = _files(200)
        request = _fake_request(master, truncate_on={(100, 2)})
        result = changed_files.enumerate_changed_files(
            request, "eneo-ai/eneo", 309, reported=200
        )
        # Exact 1..N in order: no gaps, no duplicates, after the per_page change.
        self.assertEqual(
            [f["path"] for f in result.files], [m["filename"] for m in master]
        )
        self.assertEqual(result.index_state, "complete")

    def test_terminal_per_page_1_overflow_is_honest_incomplete(self):
        master = _files(3)
        # Every constant-per_page full pass overflows on its first page, forcing the
        # terminal single-file pass; within that pass, the middle file still overflows.
        full_overflow = {(per_page, 1) for per_page in (100, 50, 25, 10, 5, 2)}
        request = _fake_request(master, truncate_on=full_overflow | {(1, 2)})
        result = changed_files.enumerate_changed_files(
            request, "eneo-ai/eneo", 309, reported=3
        )
        # The overflowed middle file is skipped, not faked or duplicated.
        self.assertEqual(
            [f["path"] for f in result.files],
            [master[0]["filename"], master[2]["filename"]],
        )
        self.assertEqual(result.registered, 2)
        self.assertEqual(result.index_state, "budget_exceeded")

    def test_full_pass_undercount_is_incomplete(self):
        # GitHub reports more changed files than the files endpoint enumerates,
        # with no truncation: the index must be honestly incomplete, not complete.
        master = _files(3)
        result = changed_files.enumerate_changed_files(
            _fake_request(master), "eneo-ai/eneo", 309, reported=5
        )
        self.assertEqual(result.registered, 3)
        self.assertEqual(result.index_state, "incomplete")

    def test_rename_only_file_is_marked_rename_only(self):
        master = [
            {
                "filename": "src/new_name.py",
                "previous_filename": "src/old_name.py",
                "status": "renamed",
                "sha": "a" * 40,
                "additions": 0,
                "deletions": 0,
                "changes": 0,
            }
        ]
        result = changed_files.enumerate_changed_files(
            _fake_request(master), "eneo-ai/eneo", 309, reported=1
        )
        self.assertEqual(result.files[0]["patch_state"], "rename_only")
        self.assertEqual(result.files[0]["previous_path"], "src/old_name.py")
        self.assertFalse(result.files[0]["patch_available"])


class ChangedFilesIntegrationTests(unittest.TestCase):
    """tools._changed_files delegates to the pager: full pagination, stable contract."""

    def _request_stub(self, master: list[dict[str, object]]):
        def request(endpoint: str, *, max_bytes: int):
            del max_bytes
            query = parse_qs(urlparse(endpoint).query)
            per_page = int(query["per_page"][0])
            page = int(query["page"][0])
            start = (page - 1) * per_page
            chunk = master[start : start + per_page]
            return (json.dumps(chunk).encode("utf-8"), False, {})

        return request

    def test_changed_files_paginates_past_300(self):
        master = _files(350)
        with patch.object(
            tools, "_request", side_effect=self._request_stub(master)
        ):
            files = tools._changed_files("eneo-ai/eneo", 309)
        self.assertEqual(len(files), 350)
        # Stable downstream contract: path + trusted context hash fields preserved.
        self.assertEqual(files[0]["path"], "src/file_0000.py")
        self.assertEqual(files[0]["context_hash_source"], "blob")
        self.assertEqual(files[0]["context_hash"], f"{0:040x}")

    def test_changed_files_truncates_previous_path_to_500(self):
        # Historical contract: previous_filename was truncated to 500 chars, matching
        # the repository path validator bound used for renamed-file base reads.
        master = [
            {
                "filename": "src/new_name.py",
                "previous_filename": "src/" + "x" * 600,
                "status": "renamed",
                "sha": "b" * 40,
                "additions": 0,
                "deletions": 0,
                "changes": 0,
            }
        ]
        with patch.object(
            tools, "_request", side_effect=self._request_stub(master)
        ):
            files = tools._changed_files("eneo-ai/eneo", 309)
        self.assertEqual(len(files[0]["previous_path"]), 500)

    def test_changed_files_patch_hash_fallback_when_no_blob_sha(self):
        # Suppression-compatibility contract: with no blob SHA, the context hash is the
        # deterministic patch hash and the source is "patch" (not the head fallback yet).
        master = [
            {
                "filename": "src/a.py",
                "status": "modified",
                "sha": "",
                "patch": "@@ -1 +1 @@\n-a\n+b",
                "additions": 1,
                "deletions": 1,
                "changes": 2,
            }
        ]
        with patch.object(
            tools, "_request", side_effect=self._request_stub(master)
        ):
            files = tools._changed_files("eneo-ai/eneo", 309)
        self.assertEqual(files[0]["context_hash_source"], "patch")
        import hashlib

        expected = hashlib.sha256(
            "src/a.py\nmodified\n1\n1\n@@ -1 +1 @@\n-a\n+b".encode("utf-8")
        ).hexdigest()
        self.assertEqual(files[0]["context_hash"], expected)


if __name__ == "__main__":
    unittest.main()
