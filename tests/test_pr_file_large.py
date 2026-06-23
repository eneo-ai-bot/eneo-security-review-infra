from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from eneo_review_tools import tools  # noqa: E402


class FileAtRevisionLargeFileTests(unittest.TestCase):
    """_file_at_revision reads <=1 MB files from the Contents API (base64) and larger files
    from the Git Blob API raw media type, bounded by _MAX_FILE_BYTES (a raw-byte cap)."""

    def _contents(self, **over):
        base = {"type": "file", "encoding": "base64", "content": "", "size": 0, "sha": "a" * 40}
        base.update(over)
        return base

    def test_small_file_uses_contents_base64(self):
        raw = b"def handler():\n    return 1\n"
        contents = self._contents(encoding="base64", content=base64.b64encode(raw).decode(), size=len(raw))
        with patch.object(tools, "_request_json", side_effect=[contents]), \
             patch.object(tools, "_request") as raw_get:
            result = tools._file_at_revision("eneo/platform", "backend/a.py", "a" * 40)
        self.assertEqual(result, raw)
        raw_get.assert_not_called()  # no blob fetch for a small (<=1 MB) file

    def test_large_file_reads_blob_raw(self):
        raw = b"line one\nline two\nline three\n"
        contents = self._contents(encoding="none", content="", size=1_103_743, sha="b" * 40)
        with patch.object(tools, "_request_json", side_effect=[contents]), \
             patch.object(tools, "_request", return_value=(raw, False, {})) as raw_get:
            result = tools._file_at_revision("eneo/platform", "frontend/schema.d.ts", "a" * 40)
        self.assertEqual(result, raw)
        self.assertEqual(raw_get.call_count, 1)
        self.assertIn("git/blobs/" + "b" * 40, raw_get.call_args.args[0])
        self.assertEqual(raw_get.call_args.kwargs.get("accept"), "application/vnd.github.raw+json")

    def test_file_over_cap_punts_to_diff_without_blob_fetch(self):
        contents = self._contents(encoding="none", content="", size=tools._MAX_FILE_BYTES + 1, sha="b" * 40)
        with patch.object(tools, "_request_json", side_effect=[contents]), \
             patch.object(tools, "_request") as raw_get:
            with self.assertRaises(tools.ToolInputError) as ctx:
                tools._file_at_revision("eneo/platform", "data/huge.json", "a" * 40)
        self.assertIn("eneo_pr_diff", str(ctx.exception))
        raw_get.assert_not_called()  # cap checked before any blob fetch

    def test_truncated_blob_punts_to_diff(self):
        # size metadata is within the cap, but the raw response truncates -> treat as too large.
        contents = self._contents(encoding="none", content="", size=tools._MAX_FILE_BYTES - 10, sha="b" * 40)
        with patch.object(tools, "_request_json", side_effect=[contents]), \
             patch.object(tools, "_request", return_value=(b"x" * 4096, True, {})):
            with self.assertRaises(tools.ToolInputError) as ctx:
                tools._file_at_revision("eneo/platform", "frontend/big.d.ts", "a" * 40)
        self.assertIn("eneo_pr_diff", str(ctx.exception))

    def test_non_regular_file_is_rejected(self):
        contents = {"type": "dir", "encoding": "none", "content": "", "size": 0}
        with patch.object(tools, "_request_json", side_effect=[contents]), \
             patch.object(tools, "_request") as raw_get:
            with self.assertRaises(tools.ToolInputError) as ctx:
                tools._file_at_revision("eneo/platform", "backend", "a" * 40)
        self.assertIn("not a regular file", str(ctx.exception))
        raw_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
