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


class SuggestibleRightSideRangeTests(unittest.TestCase):
    def test_accepts_added_line_with_omitted_hunk_counts(self):
        patch = "@@ -8 +8 @@\n-old\n+new"

        self.assertTrue(
            diff_render.is_suggestible_right_side_range(
                patch, start_line=8, end_line=8
            )
        )

    def test_accepts_insertion_only_hunk(self):
        patch = "@@ -3,0 +4,2 @@\n+first\n+second"

        self.assertTrue(
            diff_render.is_suggestible_right_side_range(
                patch, start_line=4, end_line=5
            )
        )

    def test_accepts_mixed_context_and_added_lines(self):
        patch = (
            "@@ -10,4 +10,4 @@ section\n"
            " unchanged\n"
            "-old\n"
            "+new\n"
            " tail one\n"
            " tail two"
        )

        self.assertTrue(
            diff_render.is_suggestible_right_side_range(
                patch, start_line=10, end_line=11
            )
        )
        self.assertFalse(
            diff_render.is_suggestible_right_side_range(
                patch, start_line=12, end_line=13
            )
        )

    def test_accepts_added_line_followed_by_no_newline_marker(self):
        patch = "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file"

        self.assertTrue(
            diff_render.is_suggestible_right_side_range(
                patch, start_line=1, end_line=1
            )
        )

    def test_selects_one_of_multiple_hunks(self):
        patch = (
            "@@ -1,2 +1,2 @@\n"
            " first\n"
            "-old first\n"
            "+new first\n"
            "@@ -20,2 +20,2 @@\n"
            " second\n"
            "-old second\n"
            "+new second"
        )

        self.assertTrue(
            diff_render.is_suggestible_right_side_range(
                patch, start_line=21, end_line=21
            )
        )
        self.assertFalse(
            diff_render.is_suggestible_right_side_range(
                patch, start_line=2, end_line=20
            )
        )

    def test_rejects_deleted_only_hunk(self):
        patch = "@@ -4,2 +3,0 @@\n-old\n-lines"

        self.assertFalse(
            diff_render.is_suggestible_right_side_range(
                patch, start_line=3, end_line=3
            )
        )

    def test_rejects_invalid_ranges(self):
        patch = "@@ -1 +1 @@\n-old\n+new"

        for start_line, end_line in [(0, 1), (2, 1), (2, 2), (True, 1)]:
            with self.subTest(start_line=start_line, end_line=end_line):
                self.assertFalse(
                    diff_render.is_suggestible_right_side_range(
                        patch, start_line=start_line, end_line=end_line
                    )
                )

    def test_rejects_malformed_hunk_header_or_body(self):
        malformed = [
            "@@ -1 +1\n-old\n+new",
            "@@ -1 +1 @@\n-old",
            "@@ -1 +1 @@\n-old\n+new\n+extra",
            "@@ -0 +1 @@\n-old\n+new",
            "@@ -1 +1 @@\n\\ No newline at end of file\n-old\n+new",
        ]

        for patch in malformed:
            with self.subTest(patch=patch):
                self.assertFalse(
                    diff_render.is_suggestible_right_side_range(
                        patch, start_line=1, end_line=1
                    )
                )

    def test_rejects_patch_when_another_hunk_is_malformed(self):
        patch = "@@ -1 +1 @@\n-old\n+new\n@@ -10,2 +10,2 @@\n context"

        self.assertFalse(
            diff_render.is_suggestible_right_side_range(
                patch, start_line=1, end_line=1
            )
        )


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

    def test_only_path_oversized_is_truncated_not_exposed(self):
        big = _cf(path="big.py", patch="@@ " + "z" * 2000)
        result = diff_render.assemble_fallback_diff(
            [big],  # type: ignore[list-item]
            only_path="big.py",
            max_chars=1000,
        )
        self.assertEqual(result.truncated_paths, ["big.py"])
        self.assertEqual(result.exposed_paths, [])


class AssembleRenderedDiffTests(unittest.TestCase):
    @staticmethod
    def _block(path: str, body: str = "@@ -1 +1 @@\n-old\n+new\n") -> str:
        return f"diff --git a/{path} b/{path}\n{body}"

    def test_rendered_path_match_is_exact(self):
        text = self._block("src/app.py.extra", "+extra\n") + self._block(
            "src/app.py", "+exact\n"
        )

        result = diff_render.assemble_rendered_diff(
            text, only_path="src/app.py", max_chars=10_000
        )

        self.assertTrue(result.path_present)
        self.assertEqual(result.exposed_paths, ["src/app.py"])
        self.assertNotIn("src/app.py.extra", result.text)
        self.assertIn("+exact", result.text)

    def test_rendered_quoted_path_decodes_git_c_escapes(self):
        text = (
            'diff --git "a/src/old name.py" "b/src/new\\303\\251 name.py"\n'
            "similarity index 100%\n"
        )

        result = diff_render.assemble_rendered_diff(
            text, only_path="src/newé name.py", max_chars=10_000
        )

        self.assertTrue(result.path_present)
        self.assertEqual(result.exposed_paths, ["src/newé name.py"])

    def test_rendered_rename_uses_destination_path(self):
        text = (
            "diff --git a/src/old.py b/src/new.py\n"
            "similarity index 100%\n"
            "rename from src/old.py\n"
            "rename to src/new.py\n"
        )

        result = diff_render.assemble_rendered_diff(
            text, only_path=None, max_chars=10_000
        )
        old = diff_render.assemble_rendered_diff(
            text, only_path="src/old.py", max_chars=10_000
        )

        self.assertEqual(result.exposed_paths, ["src/new.py"])
        self.assertFalse(old.path_present)

    def test_rendered_header_disambiguates_path_containing_b_prefix(self):
        path = "foo b/bar.py"
        text = self._block(path)

        result = diff_render.assemble_rendered_diff(
            text, only_path=path, max_chars=10_000
        )

        self.assertTrue(result.path_present)
        self.assertEqual(result.exposed_paths, [path])

    def test_rendered_budget_preserves_complete_prefix(self):
        first = self._block("a.py", "+" + "a" * 600 + "\n")
        second = self._block("b.py", "+" + "b" * 600 + "\n")

        result = diff_render.assemble_rendered_diff(
            first + second, only_path=None, max_chars=1000
        )

        self.assertEqual(result.text, first)
        self.assertEqual(result.exposed_paths, ["a.py"])
        self.assertEqual(result.truncated_paths, [])
        self.assertTrue(result.more_paths_available)

    def test_rendered_single_oversized_path_is_truncated(self):
        text = self._block("big.py", "+" + "z" * 2000 + "\n")

        result = diff_render.assemble_rendered_diff(
            text, only_path="big.py", max_chars=1000
        )

        self.assertEqual(len(result.text), 1000)
        self.assertEqual(result.exposed_paths, [])
        self.assertEqual(result.truncated_paths, ["big.py"])
        self.assertLessEqual(len(result.text), 1000)

if __name__ == "__main__":
    unittest.main()
