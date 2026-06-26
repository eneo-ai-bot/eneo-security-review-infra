from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins" / "eneo_review_tools"
sys.path.insert(0, str(PLUGIN))

import memory_db  # noqa: E402


class ReviewStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = memory_db.connect(str(Path(self.temp.name) / "memory.sqlite3"))

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def _finding(self, **over):
        base = {
            "rule_id": "tenant.missing-scope",
            "category": "security",
            "path": "backend/a.py",
            "line": 10,
            "symbol": "handler",
            "anchor": "POST /a",
            "title": "title",
            "severity": "High",
            "publication_score": 9,
            "confidence": 0.9,
            "evidence": "evidence",
            "disproof_checks": "checks",
            "impact": "impact",
            "smallest_fix": "fix",
            "introduced_by_diff": True,
        }
        base.update(over)
        return base

    def _record(
        self,
        *,
        repo="eneo/platform",
        context_hash="d" * 40,
        head_sha="a" * 40,
        **over,
    ):
        finding = self._finding(**over)
        run = memory_db.start_run(
            self.connection,
            repo,
            1,
            base_sha="b" * 40,
            head_sha=head_sha,
        )
        if run["status"] == "duplicate":
            run = memory_db.start_run(
                self.connection,
                repo,
                1,
                base_sha="b" * 40,
                head_sha=head_sha,
                force=True,
            )
        return memory_db.record_findings(
            self.connection,
            repo,
            1,
            head_sha,
            [finding],
            review_run_id=int(run["id"]),
            base_sha="b" * 40,
            context_hashes={finding["path"]: context_hash},
        )[0]

    def test_empty_db_returns_zeros(self):
        stats = memory_db.compute_stats(self.connection, repository="eneo/platform")
        self.assertEqual(stats["findings_total"], 0)
        self.assertEqual(stats["findings_without_decision"], 0)
        self.assertEqual(stats["active_suppressions"], 0)
        self.assertEqual(stats["repeats_after_decision_approx"], 0)
        self.assertEqual(sum(stats["latest_decision_by_type"].values()), 0)
        self.assertEqual(
            stats["findings_by_severity"],
            {"Critical": 0, "High": 0, "Low": 0, "Medium": 0},
        )

    def test_counts_by_severity_and_category(self):
        self._record(path="backend/a.py", anchor="A", severity="Critical", category="security")
        self._record(path="backend/b.py", anchor="B", severity="High", category="performance")
        stats = memory_db.compute_stats(self.connection, repository="eneo/platform")
        self.assertEqual(stats["findings_total"], 2)
        self.assertEqual(
            stats["findings_by_severity"],
            {"Critical": 1, "High": 1, "Low": 0, "Medium": 0},
        )
        self.assertEqual(stats["findings_by_category"]["security"], 1)
        self.assertEqual(stats["findings_by_category"]["performance"], 1)

    def test_active_suppression_and_decision_counts(self):
        result = self._record()
        memory_db.add_decision(
            self.connection, result["fingerprint"], "false_positive", "reason", "github:alice",
            expires_days=180,
            latest=True,
        )
        stats = memory_db.compute_stats(self.connection, repository="eneo/platform")
        self.assertEqual(stats["active_suppressions"], 1)
        self.assertEqual(stats["latest_decision_by_type"]["false_positive"], 1)
        self.assertEqual(stats["findings_without_decision"], 0)

    def test_expired_suppression_is_not_active(self):
        result = self._record()
        memory_db.add_decision(
            self.connection, result["fingerprint"], "accepted_risk", "reason", "github:alice",
            expires_days=1,
            latest=True,
        )
        future = memory_db.utc_now() + timedelta(days=2)
        stats = memory_db.compute_stats(self.connection, repository="eneo/platform", now=future)
        self.assertEqual(stats["active_suppressions"], 0)
        # the decision record still exists; it is simply no longer active.
        self.assertEqual(stats["latest_decision_by_type"]["accepted_risk"], 1)

    def test_hash_mismatched_suppression_is_not_active(self):
        result = self._record(context_hash="d" * 40)
        memory_db.add_decision(
            self.connection, result["fingerprint"], "false_positive", "reason", "github:alice",
            expires_days=180,
            latest=True,
        )
        # The file changes -> the same fingerprint now has a different trusted context hash.
        self._record(context_hash="e" * 40)
        stats = memory_db.compute_stats(self.connection, repository="eneo/platform")
        self.assertEqual(stats["active_suppressions"], 0)

    def test_resolved_and_reopen_are_not_suppressive(self):
        result = self._record()
        memory_db.add_decision(
            self.connection, result["fingerprint"], "resolved", "fixed in #2", "github:alice",
            latest=True,
        )
        stats = memory_db.compute_stats(self.connection, repository="eneo/platform")
        self.assertEqual(stats["active_suppressions"], 0)
        self.assertEqual(stats["latest_decision_by_type"]["resolved"], 1)

    def test_nearing_expiry_respects_window(self):
        result = self._record()
        memory_db.add_decision(
            self.connection, result["fingerprint"], "false_positive", "reason", "github:alice",
            expires_days=10,
            latest=True,
        )
        within = memory_db.compute_stats(
            self.connection, repository="eneo/platform", expiring_within_days=30
        )
        self.assertEqual(within["active_suppressions"], 1)
        self.assertEqual(within["active_suppressions_nearing_expiry"], 1)
        outside = memory_db.compute_stats(
            self.connection, repository="eneo/platform", expiring_within_days=5
        )
        self.assertEqual(outside["active_suppressions_nearing_expiry"], 0)

    def test_repo_scopes_all_counts(self):
        self._record(repo="eneo/platform", path="backend/a.py", anchor="A")
        self._record(repo="other/repo", path="backend/b.py", anchor="B")
        scoped = memory_db.compute_stats(self.connection, repository="eneo/platform")
        self.assertEqual(scoped["findings_total"], 1)
        unscoped = memory_db.compute_stats(self.connection)
        self.assertEqual(unscoped["findings_total"], 2)

    def test_repeats_after_decision_counts_re_record(self):
        result = self._record(context_hash="d" * 40)
        memory_db.add_decision(
            self.connection, result["fingerprint"], "false_positive", "reason", "github:alice",
            expires_days=180,
            latest=True,
        )
        # A later commit still contains the same finding after the human decision.
        self._record(context_hash="d" * 40, head_sha="b" * 40)
        stats = memory_db.compute_stats(self.connection, repository="eneo/platform")
        self.assertEqual(stats["repeats_after_decision_approx"], 1)


if __name__ == "__main__":
    unittest.main()
