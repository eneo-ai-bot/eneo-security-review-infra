from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins" / "eneo_review_tools"
sys.path.insert(0, str(PLUGIN))

import memory_db  # noqa: E402


class ReviewRunsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = memory_db.connect(str(Path(self.temp.name) / "memory.sqlite3"))

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_start_then_complete_run(self):
        run = memory_db.start_run(
            self.connection, "eneo-ai/eneo", 498,
            trigger_comment_id=111, trigger_user="github:ccimen", head_sha="a" * 40,
        )
        self.assertEqual(run["status"], "running")
        done = memory_db.complete_run(
            self.connection, "eneo-ai/eneo", 498, 111,
            status="done", findings_count=2, posted_comment_id=222,
        )
        self.assertIsNotNone(done)
        self.assertEqual(done["status"], "done")
        runs = memory_db.list_runs(self.connection, repository="eneo-ai/eneo")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "done")
        self.assertEqual(runs[0]["findings_count"], 2)
        self.assertEqual(runs[0]["posted_comment_id"], 222)
        self.assertIsNotNone(runs[0]["completed_at"])

    def test_complete_without_matching_run_is_noop(self):
        self.assertIsNone(memory_db.complete_run(self.connection, "eneo-ai/eneo", 498, 999))

    def test_failed_run_has_no_findings_count(self):
        memory_db.start_run(self.connection, "eneo-ai/eneo", 1, trigger_comment_id=5)
        memory_db.complete_run(self.connection, "eneo-ai/eneo", 1, 5, status="failed")
        runs = memory_db.list_runs(self.connection)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertIsNone(runs[0]["findings_count"])

    def test_re_review_is_a_separate_run(self):
        memory_db.start_run(self.connection, "eneo-ai/eneo", 7, trigger_comment_id=10)
        memory_db.complete_run(self.connection, "eneo-ai/eneo", 7, 10, findings_count=1)
        memory_db.start_run(self.connection, "eneo-ai/eneo", 7, trigger_comment_id=20)  # re-@review
        runs = memory_db.list_runs(self.connection, repository="eneo-ai/eneo")
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["status"], "running")  # newest first
        self.assertEqual(runs[1]["status"], "done")

    def test_invalid_status_rejected(self):
        memory_db.start_run(self.connection, "eneo-ai/eneo", 1, trigger_comment_id=1)
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.complete_run(self.connection, "eneo-ai/eneo", 1, 1, status="suppressed")

    def test_run_stats(self):
        memory_db.start_run(self.connection, "eneo-ai/eneo", 1, trigger_comment_id=1)
        memory_db.complete_run(self.connection, "eneo-ai/eneo", 1, 1, status="done", findings_count=3)
        memory_db.start_run(self.connection, "eneo-ai/eneo", 2, trigger_comment_id=2)
        memory_db.complete_run(self.connection, "eneo-ai/eneo", 2, 2, status="failed")
        stats = memory_db.run_stats(self.connection, repository="eneo-ai/eneo", days=30)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["by_status"]["done"], 1)
        self.assertEqual(stats["by_status"]["failed"], 1)
        self.assertEqual(stats["avg_findings_per_completed_run"], 3.0)
        self.assertIsNotNone(stats["time_to_answer_seconds"]["p50"])

    def test_repo_scopes_runs(self):
        memory_db.start_run(self.connection, "eneo-ai/eneo", 1, trigger_comment_id=1)
        memory_db.start_run(self.connection, "other/repo", 1, trigger_comment_id=2)
        self.assertEqual(len(memory_db.list_runs(self.connection, repository="eneo-ai/eneo")), 1)
        self.assertEqual(len(memory_db.list_runs(self.connection)), 2)


if __name__ == "__main__":
    unittest.main()
