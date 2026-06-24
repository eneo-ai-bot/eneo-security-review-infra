from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PLUGINS = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGINS))

from eneo_review_tools import memory_db, tools  # noqa: E402


class RunToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        os.environ["ENEO_REVIEW_DB"] = str(Path(self.temp.name) / "memory.sqlite3")
        os.environ["ENEO_ALLOWED_REPOSITORIES"] = "eneo-ai/eneo"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.temp.cleanup()

    def call(self, handler, args):
        return json.loads(handler(args))

    def start(self, pr=498, sha="a" * 40):
        return self.call(
            tools.review_run_start,
            {"repository": "eneo-ai/eneo", "pr_number": pr, "head_sha": sha},
        )

    def test_start_then_complete(self):
        start = self.start()
        self.assertEqual(start["status"], "running")
        self.assertIn("run_id", start)
        done = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 498, "run_id": start["run_id"],
             "status": "generated", "findings_count": 2},
        )
        self.assertTrue(done["updated"])
        self.assertEqual(done["status"], "generated")
        with closing(memory_db.connect()) as connection:
            self.assertEqual(memory_db.list_runs(connection)[0]["findings_count"], 2)

    def test_overlapping_runs_complete_by_id(self):
        a = self.start(pr=7, sha="a" * 40)
        b = self.start(pr=7, sha="b" * 40)
        self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 7, "run_id": a["run_id"], "status": "generated", "findings_count": 1},
        )
        with closing(memory_db.connect()) as connection:
            runs = {r["id"]: r for r in memory_db.list_runs(connection)}
        self.assertEqual(runs[a["run_id"]]["status"], "generated")
        self.assertEqual(runs[b["run_id"]]["status"], "running")

    def test_missing_run_id_rejected(self):
        result = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 1, "status": "generated"},
        )
        self.assertIn("error", result)
        self.assertIn("run_id", result["error"])

    def test_unknown_run_id_is_noop(self):
        result = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 1, "run_id": 999, "status": "generated"},
        )
        self.assertFalse(result["updated"])

    def test_non_allowlisted_repo_rejected(self):
        result = self.call(
            tools.review_run_start,
            {"repository": "evil/repo", "pr_number": 1, "head_sha": "a" * 40},
        )
        self.assertIn("error", result)
        self.assertIn("allowlisted", result["error"])

    def test_bad_head_sha_rejected(self):
        result = self.call(
            tools.review_run_start,
            {"repository": "eneo-ai/eneo", "pr_number": 1, "head_sha": "not-a-sha"},
        )
        self.assertIn("error", result)
        self.assertIn("head_sha", result["error"])

    def test_bad_findings_count_is_input_error(self):
        start = self.start(pr=3)
        result = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 3, "run_id": start["run_id"],
             "status": "generated", "findings_count": "lots"},
        )
        self.assertIn("error", result)

    def test_done_status_aliases_to_generated_for_older_prompts(self):
        start = self.start(pr=5)
        result = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 5, "run_id": start["run_id"], "status": "done"},
        )
        self.assertTrue(result["updated"])
        self.assertEqual(result["status"], "generated")

    def test_bad_status_rejected(self):
        start = self.start(pr=4)
        result = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 4, "run_id": start["run_id"], "status": "suppressed"},
        )
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
