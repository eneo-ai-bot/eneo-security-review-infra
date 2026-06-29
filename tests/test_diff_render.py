from __future__ import annotations

import sys
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from eneo_review_tools import diff_render  # noqa: E402


def _cf(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "path": "src/a.py",
        "status": "modified",
        "previous_path": None,
        "blob_sha": "a" * 40,
        "patch": "@@ -1 +1 @@\n-a\n+b",
        "patch_available": True,
        "patch_state": "available",
        "additions": 1,
        "deletions": 1,
        "changes": 2,
    }
    base.update(over)
    return base


class SynthesizeFileDiffTests(unittest.TestCase):
    def test_modified_file_has_git_header_and_patch(self):
        out = diff_render.synthesize_file_diff(_cf())
        assert out is not None
        self.assertTrue(out.startswith("diff --git a/src/a.py b/src/a.py\n"))
        self.assertIn("--- a/src/a.py\n", out)
        self.assertIn("+++ b/src/a.py\n", out)
        self.assertIn("@@ -1 +1 @@\n-a\n+b", out)
        # The git header carries " b/{path}" so _filter_diff / _diff_paths still work.
        self.assertIn(" b/src/a.py", out.splitlines()[0])

    def test_added_file_uses_dev_null_old_side(self):
        out = diff_render.synthesize_file_diff(
            _cf(status="added", patch="@@ -0,0 +1 @@\n+x")
        )
        assert out is not None
        self.assertIn("diff --git a/src/a.py b/src/a.py\n", out)
        self.assertIn("--- /dev/null\n", out)
        self.assertIn("+++ b/src/a.py\n", out)

    def test_removed_file_uses_dev_null_new_side(self):
        out = diff_render.synthesize_file_diff(
            _cf(status="removed", patch="@@ -1 +0,0 @@\n-x")
        )
        assert out is not None
        self.assertIn("--- a/src/a.py\n", out)
        self.assertIn("+++ /dev/null\n", out)

    def test_renamed_with_content_uses_old_and_new_paths(self):
        out = diff_render.synthesize_file_diff(
            _cf(status="renamed", previous_path="src/old.py", path="src/new.py")
        )
        assert out is not None
        self.assertTrue(out.startswith("diff --git a/src/old.py b/src/new.py\n"))
        self.assertIn("--- a/src/old.py\n", out)
        self.assertIn("+++ b/src/new.py\n", out)

    def test_rename_only_renders_header_without_hunks(self):
        out = diff_render.synthesize_file_diff(
            _cf(
                status="renamed",
                previous_path="src/old.py",
                path="src/new.py",
                patch=None,
                patch_available=False,
                patch_state="rename_only",
            )
        )
        assert out is not None
        self.assertIn("diff --git a/src/old.py b/src/new.py\n", out)
        self.assertIn("rename from src/old.py\n", out)
        self.assertIn("rename to src/new.py\n", out)
        self.assertNotIn("@@", out)

    def test_missing_patch_returns_none(self):
        out = diff_render.synthesize_file_diff(
            _cf(patch=None, patch_available=False, patch_state="missing")
        )
        self.assertIsNone(out)


class AssembleFallbackDiffTests(unittest.TestCase):
    def test_fully_packed_file_is_exposed_not_truncated_when_more_remain(self):
        a = _cf(path="a.py", patch="@@ " + "x" * 600)
        b = _cf(path="b.py", patch="@@ " + "y" * 600)
        result = diff_render.assemble_fallback_diff(
            [a, b],  # type: ignore[list-item]
            only_path=None,
            max_chars=1000,
        )
        # A fully fit: it is complete, not truncated. B was dropped for budget.
        self.assertEqual(result.exposed_paths, ["a.py"])
        self.assertEqual(result.truncated_paths, [])
        self.assertTrue(result.more_paths_available)
        self.assertNotIn("b.py", result.exposed_paths)

    def test_single_oversized_file_is_truncated_not_exposed(self):
        big = _cf(path="big.py", patch="@@ " + "z" * 2000)
        result = diff_render.assemble_fallback_diff(
            [big],  # type: ignore[list-item]
            only_path=None,
            max_chars=1000,
        )
        self.assertEqual(result.truncated_paths, ["big.py"])
        self.assertEqual(result.exposed_paths, [])
        self.assertLessEqual(len(result.text), 1000)

    def test_only_path_oversized_is_truncated_not_exposed(self):
        big = _cf(path="big.py", patch="@@ " + "z" * 2000)
        result = diff_render.assemble_fallback_diff(
            [big],  # type: ignore[list-item]
            only_path="big.py",
            max_chars=1000,
        )
        self.assertEqual(result.truncated_paths, ["big.py"])
        self.assertEqual(result.exposed_paths, [])


if __name__ == "__main__":
    unittest.main()
