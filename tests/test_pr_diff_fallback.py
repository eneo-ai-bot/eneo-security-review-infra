from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGINS = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGINS))

from eneo_review_tools import changed_files, memory_db, tools  # noqa: E402


def _cf(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "path": "backend/api.py",
        "status": "modified",
        "previous_path": None,
        "blob_sha": "d" * 40,
        "patch": "@@ -1,2 +1,3 @@\n-old\n+new",
        "patch_available": True,
        "patch_state": "available",
        "additions": 2,
        "deletions": 1,
        "changes": 3,
    }
    base.update(over)
    return base


def _index(files: list[dict[str, object]]):
    return changed_files.ChangedFileIndex(
        files=files,  # type: ignore[arg-type]
        index_state="complete",
        reported=len(files),
        registered=len(files),
    )


def _pull(*, base_sha: str = "b" * 40, head_sha: str = "a" * 40) -> dict[str, object]:
    return {
        "state": "open",
        "draft": False,
        "title": "Test PR",
        "html_url": "https://github.com/eneo-ai/eneo/pull/12",
        "user": {"login": "alice"},
        "changed_files": 1,
        "additions": 2,
        "deletions": 1,
        "head": {"ref": "feature", "sha": head_sha, "repo": {"full_name": "eneo-ai/eneo"}},
        "base": {"ref": "main", "sha": base_sha, "repo": {"full_name": "eneo-ai/eneo"}},
    }


class PrDiffFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        os.environ["ENEO_REVIEW_DB"] = str(Path(self.temp.name) / "memory.sqlite3")
        os.environ["ENEO_ALLOWED_REPOSITORIES"] = "eneo-ai/eneo"
        memory_db.connect(os.environ["ENEO_REVIEW_DB"]).close()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.temp.cleanup()

    def _adapter_file(self, path: str) -> dict[str, object]:
        return {
            "path": path,
            "status": "modified",
            "additions": 2,
            "deletions": 1,
            "changes": 3,
            "patch_available": True,
            "context_hash": "d" * 40,
            "context_hash_source": "blob",
        }

    def _begin_run(self, paths: list[str] | None = None) -> int:
        adapter_files = [self._adapter_file(p) for p in (paths or ["backend/api.py"])]
        pull = _pull()
        pull["changed_files"] = len(adapter_files)
        with (
            patch.object(tools, "_pr", return_value=pull),
            patch.object(tools, "_changed_files", return_value=adapter_files),
        ):
            start = json.loads(
                tools.review_begin({"repository": "eneo-ai/eneo", "pr_number": 12})
            )
        return int(start["run_id"])

    def _pr_diff(self, run_id, index, *, path=None, max_chars=None):
        args: dict[str, object] = {
            "repository": "eneo-ai/eneo",
            "pr_number": 12,
            "run_id": run_id,
        }
        if path is not None:
            args["path"] = path
        if max_chars is not None:
            args["max_chars"] = max_chars
        with (
            patch.object(tools, "_pr", return_value=_pull()),
            patch.object(
                tools,
                "_request",
                side_effect=tools.DiffUnavailableError(
                    "GitHub could not render this diff; inspect smaller files instead"
                ),
            ),
            patch.object(tools, "_changed_file_index", return_value=index),
        ):
            return json.loads(tools.pr_diff(args))

    def test_pr_diff_falls_back_to_per_file_patches_on_406(self):
        run_id = self._begin_run()
        result = self._pr_diff(run_id, _index([_cf()]))
        self.assertNotIn("error", result)
        self.assertEqual(result["diff_source"], "per_file_patch")
        self.assertIn("diff --git a/backend/api.py b/backend/api.py", result["diff"])
        self.assertIn("@@ -1,2 +1,3 @@\n-old\n+new", result["diff"])

    def test_transport_truncation_uses_per_file_fallback_for_exact_path(self):
        run_id = self._begin_run(["src/first.py", "src/target.py"])
        partial = b"diff --git a/src/first.py b/src/first.py\n@@ -1 +1 @@\n-old\n+new\n"
        target = _cf(path="src/target.py")
        with (
            patch.object(tools, "_pr", return_value=_pull()),
            patch.object(tools, "_request", return_value=(partial, True, {})),
            patch.object(tools, "_changed_file_index", return_value=_index([target])),
        ):
            result = json.loads(
                tools.pr_diff(
                    {
                        "repository": "eneo-ai/eneo",
                        "pr_number": 12,
                        "run_id": run_id,
                        "path": "src/target.py",
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["diff_source"], "per_file_patch")
        self.assertIn("b/src/target.py", result["diff"])

    def test_rendered_diff_missing_exact_path_uses_per_file_fallback(self):
        run_id = self._begin_run(["src/first.py", "src/target.py"])
        rendered = b"diff --git a/src/first.py b/src/first.py\n@@ -1 +1 @@\n-old\n+new\n"
        target = _cf(path="src/target.py")
        with (
            patch.object(tools, "_pr", return_value=_pull()),
            patch.object(tools, "_request", return_value=(rendered, False, {})),
            patch.object(tools, "_changed_file_index", return_value=_index([target])),
        ):
            result = json.loads(
                tools.pr_diff(
                    {
                        "repository": "eneo-ai/eneo",
                        "pr_number": 12,
                        "run_id": run_id,
                        "path": "src/target.py",
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["diff_source"], "per_file_patch")
        self.assertIn("b/src/target.py", result["diff"])

    def test_unchanged_path_returns_guidance_without_tool_failure(self):
        run_id = self._begin_run(["src/changed.py"])
        result = self._pr_diff(
            run_id,
            _index([_cf(path="src/changed.py")]),
            path="src/context.py",
        )

        self.assertNotIn("error", result)
        self.assertEqual(result["path_state"], "not_in_changed_files")
        self.assertEqual(result["diff"], "")
        self.assertFalse(result["truncated"])
        self.assertIn("eneo_pr_file", result["next_action"])
        self.assertIn("Do not retry eneo_pr_diff", result["next_action"])

    def test_pr_diff_unavailable_path_returns_non_failure_handoff_to_pr_file(self):
        run_id = self._begin_run()
        index = _index([_cf(patch=None, patch_available=False, patch_state="missing")])
        result = self._pr_diff(run_id, index, path="backend/api.py")
        self.assertNotIn("error", result)
        self.assertEqual(result["path_state"], "diff_unavailable")
        self.assertEqual(result["diff"], "")
        self.assertIn("eneo_pr_file", result["next_action"])
        self.assertIn("Do not retry eneo_pr_diff", result["next_action"])

    def test_pr_diff_no_path_fallback_records_complete_coverage(self):
        run_id = self._begin_run()
        self._pr_diff(run_id, _index([_cf()]))
        from contextlib import closing

        with closing(memory_db.connect()) as connection:
            summary = memory_db.coverage_summary(connection, run_id=run_id)
        assert summary is not None
        self.assertEqual(summary["diff_exposed"], 1)

    def test_no_path_then_path_followup_yields_complete_coverage(self):
        from contextlib import closing

        run_id = self._begin_run(["src/a.py", "src/b.py"])
        big_a = _cf(path="src/a.py", patch="@@ -1 +1 @@\n" + "x" * 600)
        big_b = _cf(path="src/b.py", patch="@@ -1 +1 @@\n" + "y" * 600)
        index = _index([big_a, big_b])

        # No-path fallback at a small budget: A fully fits, B is left out (not truncated).
        first = self._pr_diff(run_id, index, max_chars=1000)
        self.assertTrue(first["more_paths_available"])
        with closing(memory_db.connect()) as connection:
            summary = memory_db.coverage_summary(connection, run_id=run_id)
        assert summary is not None
        self.assertEqual(summary["state"], "incomplete")
        self.assertEqual(summary["diff_exposed"], 1)

        # The reviewer fetches B by path; coverage must now be COMPLETE — A must not
        # have been poisoned as "truncated" just because B was deferred.
        self._pr_diff(run_id, index, path="src/b.py")
        with closing(memory_db.connect()) as connection:
            summary = memory_db.coverage_summary(connection, run_id=run_id)
        assert summary is not None
        self.assertEqual(summary["diff_exposed"], 2)
        self.assertEqual(summary["state"], "complete")


if __name__ == "__main__":
    unittest.main()
