from __future__ import annotations

import json
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "bootstrap" / "plugins" / "eneo_review_tools"
sys.path.insert(0, str(PLUGIN))

import memory_db  # noqa: E402


class ReviewRunsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "memory.sqlite3"
        self.connection = memory_db.connect(str(self.db_path))

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
            status="generated", findings_count=2, posted_comment_id=222,
        )
        self.assertIsNotNone(done)
        self.assertEqual(done["status"], "generated")
        runs = memory_db.list_runs(self.connection, repository="eneo-ai/eneo")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "generated")
        self.assertEqual(runs[0]["findings_count"], 2)
        self.assertEqual(runs[0]["posted_comment_id"], 222)
        self.assertIsNotNone(runs[0]["completed_at"])

    def test_complete_unknown_run_is_noop(self):
        self.assertIsNone(memory_db.complete_run(self.connection, 999))

    def test_complete_is_idempotent(self):
        run = memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40)
        first = memory_db.complete_run(self.connection, run["id"], status="generated", findings_count=1)
        self.assertIsNotNone(first)
        # Second completion finds no *running* row -> clean no-op, original untouched.
        self.assertIsNone(memory_db.complete_run(self.connection, run["id"], status="failed"))
        self.assertEqual(memory_db.list_runs(self.connection)[0]["status"], "generated")

    def test_overlapping_runs_complete_independently(self):
        # Regression: completing by (repo, pr) latest-running could complete the wrong run.
        a = memory_db.start_run(self.connection, "eneo-ai/eneo", 7, head_sha="a" * 40)
        b = memory_db.start_run(self.connection, "eneo-ai/eneo", 7, head_sha="b" * 40)
        memory_db.complete_run(self.connection, a["id"], status="generated", findings_count=3)
        runs = {r["id"]: r for r in memory_db.list_runs(self.connection)}
        self.assertEqual(runs[a["id"]]["status"], "generated")
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

    def test_run_stats_avg_counts_only_generated(self):
        a = memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40)
        memory_db.complete_run(self.connection, a["id"], status="generated", findings_count=3)
        b = memory_db.start_run(self.connection, "eneo-ai/eneo", 2, head_sha="b" * 40)
        # A failed run with a findings_count must NOT skew the average.
        memory_db.complete_run(self.connection, b["id"], status="failed", findings_count=5)
        stats = memory_db.run_stats(self.connection, repository="eneo-ai/eneo", days=30)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["by_status"]["generated"], 1)
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

    def test_mark_stale_running_runs_failed(self):
        now = memory_db.utc_now()
        old = now - timedelta(minutes=45)
        fresh = now - timedelta(minutes=5)
        memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40, now=old)
        memory_db.start_run(self.connection, "eneo-ai/eneo", 2, head_sha="b" * 40, now=fresh)

        result = memory_db.mark_stale_runs_failed(self.connection, now=now)

        self.assertEqual(result["failed_count"], 1)
        self.assertEqual(result["runs"][0]["pr_number"], 1)
        runs = {r["pr_number"]: r for r in memory_db.list_runs(self.connection)}
        self.assertEqual(runs[1]["status"], "failed")
        self.assertEqual(runs[2]["status"], "running")
        self.assertIsNotNone(runs[1]["completed_at"])

    def test_start_run_marks_stale_same_pr_only(self):
        now = memory_db.utc_now()
        old = now - timedelta(minutes=45)
        memory_db.start_run(self.connection, "eneo-ai/eneo", 7, head_sha="a" * 40, now=old)
        memory_db.start_run(self.connection, "eneo-ai/eneo", 8, head_sha="b" * 40, now=old)

        memory_db.start_run(self.connection, "eneo-ai/eneo", 7, head_sha="c" * 40, now=now)

        runs = memory_db.list_runs(self.connection, repository="eneo-ai/eneo")
        by_pr = {}
        for run in runs:
            by_pr.setdefault(run["pr_number"], []).append(run)
        self.assertEqual(
            sorted(run["status"] for run in by_pr[7]),
            ["failed", "running"],
        )
        self.assertEqual(by_pr[8][0]["status"], "running")

    def test_mark_stale_rejects_bad_age(self):
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.mark_stale_runs_failed(self.connection, older_than_minutes=0)

    def test_run_stats_time_to_answer_excludes_failed_runs(self):
        now = memory_db.utc_now()
        generated = memory_db.start_run(
            self.connection,
            "eneo-ai/eneo",
            1,
            head_sha="a" * 40,
            now=now - timedelta(seconds=10),
        )
        memory_db.complete_run(
            self.connection,
            generated["id"],
            status="generated",
            findings_count=1,
            now=now,
        )
        failed = memory_db.start_run(
            self.connection,
            "eneo-ai/eneo",
            2,
            head_sha="b" * 40,
            now=now - timedelta(hours=3),
        )
        memory_db.complete_run(
            self.connection,
            failed["id"],
            status="failed",
            findings_count=1,
            now=now,
        )

        stats = memory_db.run_stats(self.connection, repository="eneo-ai/eneo", now=now)

        self.assertEqual(stats["time_to_answer_seconds"], {"p50": 10.0, "p95": 10.0})

    def test_runs_cli_marks_stalled_as_json(self):
        now = memory_db.utc_now()
        old = now - timedelta(minutes=45)
        memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40, now=old)
        self.connection.close()

        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "eneo_review_memory.py"),
                "--db",
                str(self.db_path),
                "runs",
                "--mark-stalled",
                "--older-than-minutes",
                "30",
                "--repo",
                "eneo-ai/eneo",
                "--json",
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        payload = json.loads(completed.stdout)
        self.assertEqual(payload["failed_count"], 1)
        self.assertEqual(payload["runs"][0]["status"], "failed")
        self.connection = memory_db.connect(str(self.db_path))

    def test_runs_cli_reports_schema_mismatch_without_traceback(self):
        self.connection.close()
        with sqlite3.connect(str(self.db_path)) as connection:
            connection.execute("PRAGMA user_version = 6")

        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "eneo_review_memory.py"),
                "--db",
                str(self.db_path),
                "runs",
                "--repo",
                "eneo-ai/eneo",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("review memory schema version 6", completed.stderr)
        self.assertIn("run `eneo-review-memory init`", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        self.connection = memory_db.connect(str(self.db_path))

    def test_repo_scopes_runs(self):
        memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40)
        memory_db.start_run(self.connection, "other/repo", 1, head_sha="b" * 40)
        self.assertEqual(len(memory_db.list_runs(self.connection, repository="eneo-ai/eneo")), 1)
        self.assertEqual(len(memory_db.list_runs(self.connection)), 2)

    def test_done_status_aliases_to_generated_for_older_prompts(self):
        run = memory_db.start_run(self.connection, "eneo-ai/eneo", 1, head_sha="a" * 40)
        result = memory_db.complete_run(self.connection, run["id"], status="done", findings_count=1)
        self.assertEqual(result["status"], "generated")
        self.assertEqual(memory_db.list_runs(self.connection)[0]["status"], "generated")


if __name__ == "__main__":
    unittest.main()
