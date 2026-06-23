from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
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
            self.connection, "eneo-ai/eneo", 498, trigger_user="github:ccimen", head_sha="a" * 40
        )
        self.assertEqual(run["status"], "running")
        done = memory_db.complete_run(
            self.connection, run["id"], repository="eneo-ai/eneo", pr_number=498,
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

    def test_complete_unknown_run_is_noop(self):
        self.assertIsNone(memory_db.complete_run(self.connection, 999))

    def test_complete_is_idempotent(self):
        run = memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40)
        first = memory_db.complete_run(self.connection, run["id"], status="done", findings_count=1)
        self.assertIsNotNone(first)
        # Second completion finds no *running* row -> clean no-op, original untouched.
        self.assertIsNone(memory_db.complete_run(self.connection, run["id"], status="failed"))
        self.assertEqual(memory_db.list_runs(self.connection)[0]["status"], "done")

    def test_overlapping_runs_complete_independently(self):
        # Regression: completing by (repo, pr) latest-running could complete the wrong run.
        a = memory_db.start_run(self.connection, "eneo-ai/eneo", 7, head_sha="a" * 40)
        b = memory_db.start_run(self.connection, "eneo-ai/eneo", 7, head_sha="b" * 40)
        memory_db.complete_run(self.connection, a["id"], status="done", findings_count=3)
        runs = {r["id"]: r for r in memory_db.list_runs(self.connection)}
        self.assertEqual(runs[a["id"]]["status"], "done")
        self.assertEqual(runs[a["id"]]["findings_count"], 3)
        self.assertEqual(runs[b["id"]]["status"], "running")  # B is untouched
        self.assertIsNone(runs[b["id"]]["completed_at"])

    def test_repository_pr_guard_blocks_mismatch(self):
        run = memory_db.start_run(self.connection, "eneo-ai/eneo", 5, head_sha="a" * 40)
        self.assertIsNone(
            memory_db.complete_run(self.connection, run["id"], repository="other/repo", pr_number=5)
        )
        self.assertEqual(memory_db.list_runs(self.connection)[0]["status"], "running")

    def test_failed_run(self):
        run = memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40)
        memory_db.complete_run(self.connection, run["id"], status="failed")
        runs = memory_db.list_runs(self.connection)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertIsNone(runs[0]["findings_count"])

    def test_invalid_status_rejected(self):
        run = memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40)
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.complete_run(self.connection, run["id"], status="suppressed")

    def test_invalid_run_id_rejected(self):
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.complete_run(self.connection, 0)

    def test_negative_findings_count_rejected_for_any_db(self):
        # Authoritative guard at the function layer (not reliant on the table CHECK,
        # which only applies to freshly created databases).
        run = memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40)
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.complete_run(self.connection, run["id"], findings_count=-1)

    def test_run_stats_avg_counts_only_done(self):
        a = memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40)
        memory_db.complete_run(self.connection, a["id"], status="done", findings_count=3)
        b = memory_db.start_run(self.connection, "eneo-ai/eneo", 2, head_sha="b" * 40)
        # A failed run with a findings_count must NOT skew the average.
        memory_db.complete_run(self.connection, b["id"], status="failed", findings_count=5)
        stats = memory_db.run_stats(self.connection, repository="eneo-ai/eneo", days=30)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["by_status"]["done"], 1)
        self.assertEqual(stats["by_status"]["failed"], 1)
        self.assertEqual(stats["avg_findings_per_completed_run"], 3.0)
        self.assertIsNotNone(stats["time_to_answer_seconds"]["p50"])

    def test_stale_running_run_is_flagged(self):
        old = memory_db.utc_now() - timedelta(minutes=45)
        memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40, now=old)
        memory_db.start_run(self.connection, "eneo-ai/eneo", 2, head_sha="b" * 40)  # fresh
        stale = [r for r in memory_db.list_runs(self.connection) if memory_db.run_is_stale(r)]
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["pr_number"], 1)
        stats = memory_db.run_stats(self.connection)
        self.assertEqual(stats["stalled_running"], 1)
        self.assertEqual(stats["by_status"]["running"], 2)

    def test_repo_scopes_runs(self):
        memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40)
        memory_db.start_run(self.connection, "other/repo", 1, head_sha="b" * 40)
        self.assertEqual(len(memory_db.list_runs(self.connection, repository="eneo-ai/eneo")), 1)
        self.assertEqual(len(memory_db.list_runs(self.connection)), 2)


if __name__ == "__main__":
    unittest.main()
