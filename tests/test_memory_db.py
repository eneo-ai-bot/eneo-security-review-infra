from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PLUGIN = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins" / "eneo_review_tools"
sys.path.insert(0, str(PLUGIN))

import memory_db  # noqa: E402
import memory_schema  # noqa: E402
import review_renderer  # noqa: E402


class ReviewMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "memory.sqlite3")
        self.connection = memory_db.connect(self.db)
        self.feedback_env = patch.dict(os.environ, {"ENEO_REVIEW_FEEDBACK_ENABLED": "true"})
        self.feedback_env.start()
        self.finding = {
            "rule_id": "tenant.missing-scope",
            "category": "security",
            "path": "backend/api/documents.py",
            "line": 42,
            "symbol": "create_document",
            "anchor": "POST /v1/documents:create",
            "title": "Document creation omits tenant scope",
            "severity": "Critical",
            "publication_score": 9,
            "confidence": 0.93,
            "evidence": "The changed query writes a caller-controlled tenant identifier.",
            "disproof_checks": "Checked the dependency and repository layer; neither binds tenant_id.",
            "impact": "A municipality user can write into another municipality's tenant.",
            "smallest_fix": "Bind tenant_id from the verified request context.",
            "introduced_by_diff": True,
        }

    def tearDown(self):
        self.feedback_env.stop()
        self.connection.close()
        self.temp.cleanup()

    def record(
        self,
        line=42,
        context_hash="d" * 40,
        pr_number=17,
        head_sha="a" * 40,
        **overrides,
    ):
        finding = dict(self.finding, line=line)
        finding.update(overrides)
        return memory_db.record_findings(
            self.connection,
            "Eneo/Platform",
            pr_number,
            head_sha,
            [finding],
            context_hashes={finding["path"]: context_hash},
        )[0]

    def test_fingerprint_is_stable_across_line_moves(self):
        first = self.record(42)
        second = self.record(97)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        row = self.connection.execute(
            "SELECT occurrences, line FROM findings WHERE fingerprint = ?",
            (first["fingerprint"],),
        ).fetchone()
        self.assertEqual(row["occurrences"], 1)
        self.assertEqual(row["line"], 97)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM finding_observations WHERE fingerprint = ?",
                (first["fingerprint"],),
            ).fetchone()[0],
            1,
        )

    def test_human_decision_suppresses_exact_file_version_and_reopen_restores(self):
        result = self.record()
        fingerprint = result["fingerprint"]
        memory_db.add_decision(
            self.connection,
            fingerprint,
            "false_positive",
            "PostgreSQL RLS enforces tenant scope for this application role.",
            "github:alice",
            expires_days=180,
            latest=True,
        )
        decision = self.connection.execute(
            "SELECT observation_id FROM decisions WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        self.assertEqual(decision["observation_id"], result["observation_id"])
        self.assertIsNotNone(memory_db.active_suppression(self.connection, fingerprint))

        memory_db.add_decision(
            self.connection,
            fingerprint,
            "reopen",
            "The migration removed FORCE ROW LEVEL SECURITY.",
            "github:bob",
            latest=True,
        )
        self.assertIsNone(memory_db.active_suppression(self.connection, fingerprint))

    def test_decision_observation_must_belong_to_same_finding(self):
        first = self.record()
        second = self.record(
            path="backend/api/users.py",
            symbol="create_user",
            anchor="POST /v1/users:create",
        )

        with self.assertRaisesRegex(
            memory_db.ReviewMemoryError,
            "observation_id belongs to a different finding",
        ):
            memory_db.insert_decision(
                self.connection,
                fingerprint=first["fingerprint"],
                decision="resolved",
                reason="Fixed with a regression test.",
                actor="github:alice",
                context_hash="",
                observation_id=second["observation_id"],
            )

    def test_file_change_invalidates_suppression(self):
        first = self.record(context_hash="d" * 40)
        memory_db.add_decision(
            self.connection,
            first["fingerprint"],
            "false_positive",
            "A verified guard exists in the unchanged implementation.",
            "github:alice",
            expires_days=180,
            latest=True,
        )
        second = self.record(context_hash="e" * 40)
        self.assertFalse(second["suppressed"])
        self.assertIsNone(memory_db.active_suppression(self.connection, first["fingerprint"]))

    def test_context_returns_historical_human_suppression(self):
        result = self.record()
        memory_db.add_decision(
            self.connection,
            result["fingerprint"],
            "accepted_risk",
            "Temporary exception approved for the migration window.",
            "github:security-team",
            expires_days=30,
            latest=True,
        )
        context = memory_db.memory_context(
            self.connection, "eneo/platform", ["backend/api/documents.py"]
        )
        self.assertEqual(len(context["historical_suppressions"]), 1)
        self.assertEqual(
            context["historical_suppressions"][0]["decision"], "accepted_risk"
        )

    def test_context_returns_unsuppressed_recent_finding_for_reexamination(self):
        result = self.record()
        context = memory_db.memory_context(
            self.connection, "eneo/platform", ["backend/api/documents.py"], pr_number=17
        )
        self.assertEqual(context["historical_suppressions"], [])
        recent = context["recent_findings"]
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["fingerprint"], result["fingerprint"])
        self.assertEqual(recent[0]["pr_number"], 17)
        self.assertFalse(recent[0]["suppressed_for_last_seen_file_version"])
        self.assertIsNone(recent[0]["latest_decision"])
        self.assertEqual(len(context["repeat_review_findings"]), 1)
        self.assertEqual(
            context["repeat_review_findings"][0]["fingerprint"], result["fingerprint"]
        )
        repeat = context["repeat_review_findings"][0]
        self.assertNotIn("evidence", repeat)
        self.assertEqual(
            repeat["prior_claim"],
            "The changed query writes a caller-controlled tenant identifier.",
        )
        self.assertIn("neither binds tenant_id", repeat["prior_disproof_checks"])

    def test_context_separates_same_pr_repeat_findings_from_cross_pr_history(self):
        same_pr = self.record()
        other_pr = self.record(
            pr_number=99,
            rule_id="tests.missing-regression",
            category="tests",
            severity="Medium",
            publication_score=7,
            anchor="document regression test",
        )
        context = memory_db.memory_context(
            self.connection, "eneo/platform", ["backend/api/documents.py"], pr_number=17
        )
        self.assertEqual(
            {item["fingerprint"] for item in context["recent_findings"]},
            {same_pr["fingerprint"], other_pr["fingerprint"]},
        )
        self.assertEqual(len(context["repeat_review_findings"]), 1)
        self.assertEqual(
            context["repeat_review_findings"][0]["fingerprint"], same_pr["fingerprint"]
        )
        self.assertEqual(context["repeat_review_findings"][0]["pr_number"], 17)

    def test_same_fingerprint_in_other_pr_does_not_overwrite_repeat_candidate(self):
        same_pr = self.record(line=42, pr_number=17, context_hash="d" * 40)
        other_pr = self.record(line=99, pr_number=99, context_hash="e" * 40)
        self.assertEqual(same_pr["fingerprint"], other_pr["fingerprint"])

        context = memory_db.memory_context(
            self.connection, "eneo/platform", ["backend/api/documents.py"], pr_number=17
        )

        self.assertEqual(len(context["repeat_review_findings"]), 1)
        repeat = context["repeat_review_findings"][0]
        self.assertEqual(repeat["fingerprint"], same_pr["fingerprint"])
        self.assertEqual(repeat["pr_number"], 17)
        self.assertEqual(repeat["line"], 42)
        self.assertEqual(repeat["context_hash"], "d" * 40)
        self.assertNotIn("evidence", repeat)
        self.assertEqual(
            repeat["prior_claim"],
            "The changed query writes a caller-controlled tenant identifier.",
        )
        self.assertEqual(repeat["previous_head"], "a" * 40)

        row = self.connection.execute(
            "SELECT occurrences FROM findings WHERE fingerprint = ?", (same_pr["fingerprint"],)
        ).fetchone()
        self.assertEqual(row["occurrences"], 2)

    def test_finalize_review_renders_all_current_findings_with_hidden_fingerprints(self):
        second = dict(
            self.finding,
            rule_id="tests.missing-regression",
            category="tests",
            path="backend/api/test_documents.py",
            line=80,
            anchor="test_create_document",
            title="Regression test misses tenant failure path",
            severity="Medium",
            publication_score=7,
            evidence="The changed test covers success but not the rejected cross-tenant path.",
            impact="A future tenant-scope regression can ship without a failing test.",
            smallest_fix="Add a focused test that asserts cross-tenant creation is rejected.",
        )
        recorded = memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [self.finding, second],
            context_hashes={
                self.finding["path"]: "d" * 40,
                second["path"]: "e" * 40,
            },
        )

        result = memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)

        markdown = result["markdown"]
        visible = markdown.split("<!--", 1)[0]
        self.assertEqual(result["findings_count"], 2)
        self.assertIn(
            "There are 2 current findings: 1 Critical (P0) and 1 Medium (P2).",
            markdown,
        )
        self.assertIn("### F1 · Critical (P0): Document creation omits tenant scope", markdown)
        self.assertIn("### F2 · Medium (P2): Regression test misses tenant failure path", markdown)
        self.assertIn("**Impact:**", markdown)
        self.assertIn("**Reviewer checks:**", markdown)
        self.assertNotIn("Required outcome:", markdown)
        self.assertNotIn("Verification:", markdown)
        self.assertIn("Copyable fix brief for a coding agent", markdown)
        self.assertIn("Give feedback on this review", markdown)
        self.assertIn("Post one command as a new top-level PR comment", markdown)
        self.assertIn("```text\n/review false-positive F1 because <what code, guard, or invariant disproves it>\n```", markdown)
        self.assertIn("```text\n/review feedback missed because <what concrete issue was missed and where>\n```", markdown)
        self.assertNotIn("@review false-positive", markdown)
        self.assertNotIn("/review intentional", markdown)
        self.assertNotIn("| Severity |", markdown)
        for item in recorded:
            self.assertNotIn(item["fingerprint"], visible)
            self.assertIn(item["fingerprint"], markdown)

    def test_finalize_review_pluralizes_multiple_findings_in_one_severity(self):
        first = dict(
            self.finding,
            severity="High",
            publication_score=8,
            title="First high issue",
            rule_id="tenant.first-high",
            anchor="POST /v1/documents:first",
        )
        second = dict(
            self.finding,
            severity="High",
            publication_score=8,
            title="Second high issue",
            rule_id="tenant.second-high",
            anchor="POST /v1/documents:second",
        )
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [first, second],
            context_hashes={self.finding["path"]: "d" * 40},
        )

        result = memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)

        self.assertIn("There are 2 current findings: 2 High (P1).", result["markdown"])
        self.assertNotIn("I found 2 High (P1) finding.", result["markdown"])

    def test_lifecycle_summary_pluralizes_reference_groups(self):
        summary = review_renderer.lifecycle_summary(
            findings=[],
            closed=[],
            still_present=[],
            partially_resolved=[],
            new_refs=["F4", "F3"],
            needs_recheck=["F2", "F1"],
        )

        self.assertIn("F1 and F2 need recheck", summary)
        self.assertIn("F3 and F4 are new", summary)

    def test_runtime_connection_requires_initialized_schema(self):
        missing = str(Path(self.temp.name) / "missing.sqlite3")
        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "run `eneo-review-memory init`"):
            memory_db.connect_existing(missing)

        empty = str(Path(self.temp.name) / "empty.sqlite3")
        sqlite3.connect(empty).close()
        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "schema version 0"):
            memory_db.connect_existing(empty)

        runtime = memory_db.connect_existing(self.db)
        try:
            self.assertEqual(
                runtime.execute("PRAGMA user_version").fetchone()[0],
                memory_db.SCHEMA_VERSION,
            )
        finally:
            runtime.close()

    def test_required_table_contract_matches_fresh_schema(self):
        fresh_path = str(Path(self.temp.name) / "fresh.sqlite3")
        fresh = memory_db.connect(fresh_path)
        try:
            actual_tables = {
                str(row["name"])
                for row in fresh.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    """
                )
            }
        finally:
            fresh.close()

        self.assertEqual(actual_tables, memory_schema.REQUIRED_TABLES)

    def test_migrate_volume_uses_sqlite_backup_and_preserves_committed_wal_rows(self):
        source_path = Path(self.temp.name) / "source.sqlite3"
        destination_path = Path(self.temp.name) / "dest" / "review_memory.sqlite3"
        source = memory_db.connect(str(source_path))
        try:
            source.execute("PRAGMA wal_checkpoint(FULL)")
            memory_db.start_run(
                source,
                "eneo/platform",
                17,
                trigger_comment_id=123,
                trigger_user="github:alice",
                head_sha="a" * 40,
            )

            result = memory_db.migrate_volume(str(source_path), str(destination_path))
        finally:
            source.close()

        self.assertEqual(result["schema_version"], memory_db.SCHEMA_VERSION)
        self.assertEqual(result["table_counts"]["review_runs"], 1)
        migrated = memory_db.connect_existing(str(destination_path))
        try:
            self.assertEqual(
                migrated.execute("SELECT COUNT(*) FROM review_runs").fetchone()[0],
                1,
            )
        finally:
            migrated.close()

    def test_finalize_review_omits_feedback_help_when_disabled(self):
        self.record()

        with patch.dict(os.environ, {"ENEO_REVIEW_FEEDBACK_ENABLED": "false"}):
            result = memory_db.finalize_review(
                self.connection, "eneo/platform", 17, "a" * 40
            )

        self.assertIn("Copyable fix brief for a coding agent", result["markdown"])
        self.assertNotIn("Give feedback on this review", result["markdown"])

    def test_finalize_review_carries_unobserved_previous_findings(self):
        first = self.finding
        second = dict(
            self.finding,
            rule_id="tests.missing-regression",
            category="tests",
            path="backend/api/test_documents.py",
            line=80,
            anchor="test_create_document",
            title="Regression test misses tenant failure path",
            severity="Medium",
            publication_score=7,
        )
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [first, second],
            context_hashes={first["path"]: "d" * 40, second["path"]: "e" * 40},
        )
        memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)

        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            [second],
            context_hashes={second["path"]: "e" * 40},
        )
        result = memory_db.finalize_review(self.connection, "eneo/platform", 17, "b" * 40)

        self.assertEqual(result["findings_count"], 2)
        self.assertEqual(result["resolved_count"], 0)
        self.assertIn("**Since the previous review:**", result["markdown"])
        self.assertIn("F1 needs recheck", result["markdown"])
        self.assertIn("F2 still present", result["markdown"])
        self.assertIn(
            "### F1 · Critical (P0): Document creation omits tenant scope",
            result["markdown"],
        )
        self.assertIn("F1 - Critical (P0) - security", result["markdown"])
        self.assertNotIn("F1 resolved", result["markdown"])

    def test_finalize_review_resolves_prior_finding_with_explicit_verdict(self):
        first = self.finding
        second = dict(
            self.finding,
            rule_id="tests.missing-regression",
            category="tests",
            path="backend/api/test_documents.py",
            line=80,
            anchor="test_create_document",
            title="Regression test misses tenant failure path",
            severity="Medium",
            publication_score=7,
        )
        first_recorded = memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [first, second],
            context_hashes={first["path"]: "d" * 40, second["path"]: "e" * 40},
        )
        memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)

        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            [second],
            context_hashes={second["path"]: "e" * 40},
        )
        result = memory_db.finalize_review(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            previous_verdicts=[
                {
                    "local_reference": "F1",
                    "verdict": "resolved",
                    "evidence": "Tenant scope is now bound from the verified request.",
                },
                {"local_reference": "F2", "verdict": "still_present"},
            ],
        )

        self.assertEqual(result["findings_count"], 1)
        self.assertEqual(result["resolved_count"], 1)
        self.assertEqual(result["closed_count"], 1)
        self.assertIn("F1 resolved", result["markdown"])
        self.assertIn("F2 still present", result["markdown"])
        self.assertNotIn("F1 needs recheck", result["markdown"])
        visible_brief = result["markdown"].split(
            "<summary>Copyable fix brief for a coding agent</summary>",
            1,
        )[1]
        self.assertNotIn("F1 - Critical (P0) - security", visible_brief)
        self.assertIn("F2 - Medium (P2) - tests", visible_brief)
        self.assertNotIn("/review false-positive F1", result["markdown"])
        self.assertIn("/review false-positive F2", result["markdown"])
        context = memory_db.memory_context(
            self.connection,
            "eneo/platform",
            [first["path"], second["path"]],
            pr_number=17,
        )
        self.assertEqual(
            {item["fingerprint"] for item in context["repeat_review_findings"]},
            {first_recorded[1]["fingerprint"]},
        )

    def test_finalize_review_tracks_partial_resolution_for_current_finding(self):
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [self.finding],
            context_hashes={self.finding["path"]: "d" * 40},
        )
        memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            [self.finding],
            context_hashes={self.finding["path"]: "d" * 40},
        )

        result = memory_db.finalize_review(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            previous_verdicts=[
                {
                    "local_reference": "F1",
                    "verdict": "partially_resolved",
                    "evidence": "The API path is guarded but the repository write is still unscoped.",
                }
            ],
        )

        self.assertEqual(result["findings_count"], 1)
        self.assertIn("F1 partially resolved", result["markdown"])
        self.assertNotIn("F1 still present", result["markdown"])

    def test_finalize_review_invalidates_absent_prior_finding(self):
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [self.finding],
            context_hashes={self.finding["path"]: "d" * 40},
        )
        memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            [],
            context_hashes={},
        )

        result = memory_db.finalize_review(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            previous_verdicts=[
                {
                    "local_reference": "F1",
                    "verdict": "invalidated",
                    "evidence": "The prior claim relied on a stale caller path.",
                }
            ],
        )

        self.assertEqual(result["findings_count"], 0)
        self.assertEqual(result["resolved_count"], 0)
        self.assertEqual(result["closed_count"], 1)
        self.assertIn("F1 withdrawn after recheck", result["markdown"])
        self.assertIn("I did not identify any current in-scope findings in this review.", result["markdown"])
        self.assertNotIn("Copyable fix brief for a coding agent", result["markdown"])
        self.assertIn("Give feedback on this review", result["markdown"])
        self.assertIn("/review feedback missed because <what concrete issue was missed and where>", result["markdown"])
        self.assertNotIn("/review false-positive", result["markdown"])

    def test_finalize_review_closes_prior_finding_with_human_suppression(self):
        recorded = memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [self.finding],
            context_hashes={self.finding["path"]: "d" * 40},
        )[0]
        memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)
        memory_db.add_decision(
            self.connection,
            recorded["fingerprint"],
            "false_positive",
            "The tenant guard exists in an unchanged dependency.",
            "github:alice",
            expires_days=180,
            latest=True,
        )
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            [],
            context_hashes={},
        )

        result = memory_db.finalize_review(self.connection, "eneo/platform", 17, "b" * 40)

        self.assertEqual(result["findings_count"], 0)
        self.assertEqual(result["resolved_count"], 0)
        self.assertEqual(result["closed_count"], 1)
        self.assertIn("F1 suppressed by human decision", result["markdown"])

    def test_finalize_review_closes_observed_finding_with_human_suppression(self):
        recorded = memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [self.finding],
            context_hashes={self.finding["path"]: "d" * 40},
        )[0]
        memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)
        memory_db.add_decision(
            self.connection,
            recorded["fingerprint"],
            "false_positive",
            "The tenant guard exists in an unchanged dependency.",
            "github:alice",
            expires_days=180,
            latest=True,
        )
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            [self.finding],
            context_hashes={self.finding["path"]: "d" * 40},
        )

        result = memory_db.finalize_review(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            previous_verdicts=[{"local_reference": "F1", "verdict": "still_present"}],
        )

        self.assertEqual(result["findings_count"], 0)
        self.assertEqual(result["resolved_count"], 0)
        self.assertEqual(result["closed_count"], 1)
        self.assertIn("F1 suppressed by human decision", result["markdown"])
        self.assertNotIn("### F1", result["markdown"])

    def test_finalize_review_rejects_contradictory_prior_verdict(self):
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [self.finding],
            context_hashes={self.finding["path"]: "d" * 40},
        )
        memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            [self.finding],
            context_hashes={self.finding["path"]: "d" * 40},
        )

        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.finalize_review(
                self.connection,
                "eneo/platform",
                17,
                "b" * 40,
                previous_verdicts=[
                    {"local_reference": "F1", "verdict": "resolved"}
                ],
            )

    def test_finalize_review_missing_path_verdict_defaults_to_not_checked(self):
        first = self.finding
        second = dict(
            self.finding,
            rule_id="tests.missing-regression",
            category="tests",
            path="backend/api/test_documents.py",
            line=80,
            anchor="test_create_document",
            title="Regression test misses tenant failure path",
            severity="Medium",
            publication_score=7,
        )
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [first, second],
            context_hashes={first["path"]: "d" * 40, second["path"]: "e" * 40},
        )
        memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)
        context = memory_db.memory_context(
            self.connection,
            "eneo/platform",
            [second["path"]],
            pr_number=17,
        )
        self.assertEqual(
            [item["local_reference"] for item in context["repeat_review_findings"]],
            ["F2"],
        )
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            [second],
            context_hashes={second["path"]: "e" * 40},
        )

        result = memory_db.finalize_review(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            previous_verdicts=[{"local_reference": "F2", "verdict": "still_present"}],
        )

        self.assertEqual(result["findings_count"], 2)
        self.assertIn("F1 needs recheck", result["markdown"])
        self.assertIn("F2 still present", result["markdown"])

    def test_carried_reference_is_not_reused_by_new_finding(self):
        first = self.finding
        second = dict(
            self.finding,
            rule_id="tests.missing-regression",
            category="tests",
            path="backend/api/test_documents.py",
            line=80,
            anchor="test_create_document",
            title="Regression test misses tenant failure path",
            severity="Medium",
            publication_score=7,
        )
        third = dict(
            self.finding,
            rule_id="reliability.unbounded-job",
            category="reliability",
            path="backend/jobs/migrate.py",
            line=12,
            anchor="run migration job",
            title="Migration job can run without bounded retries",
            severity="High",
            publication_score=8,
        )
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [first, second],
            context_hashes={first["path"]: "d" * 40, second["path"]: "e" * 40},
        )
        memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            [second],
            context_hashes={second["path"]: "e" * 40},
        )
        memory_db.finalize_review(self.connection, "eneo/platform", 17, "b" * 40)
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "c" * 40,
            [second, third],
            context_hashes={second["path"]: "e" * 40, third["path"]: "f" * 40},
        )

        result = memory_db.finalize_review(self.connection, "eneo/platform", 17, "c" * 40)

        self.assertIn("F2 still present", result["markdown"])
        self.assertIn("F1 needs recheck", result["markdown"])
        self.assertIn("F3 is new", result["markdown"])
        self.assertIn(
            "### F3 · High (P1): Migration job can run without bounded retries",
            result["markdown"],
        )

    def test_finalize_review_escapes_model_text_in_markdown_layout(self):
        malicious = dict(
            self.finding,
            title="Break </details> <!-- hidden --> ``` fence",
            evidence="Evidence tries </details> and <!-- hidden --> plus ```fence.",
            impact="Impact closes </details> and starts <!-- hidden -->.",
            smallest_fix="Fix uses ```not a fence``` plus List<T> and a && b.",
            disproof_checks="Verify </details> is escaped and ``` is neutralized.",
        )
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [malicious],
            context_hashes={malicious["path"]: "d" * 40},
        )

        result = memory_db.finalize_review(self.connection, "eneo/platform", 17, "a" * 40)
        visible = result["markdown"].split("<!--\neneo-review:", 1)[0]
        rendered_prose = visible.split("```text\nTask:", 1)[0]

        self.assertIn("&lt;/details&gt;", rendered_prose)
        self.assertIn("&lt;!-- hidden --&gt;", rendered_prose)
        self.assertIn(
            "[`backend/api/documents.py:42`](https://github.com/eneo/platform/blob/"
            + "a" * 40
            + "/backend/api/documents.py#L42)",
            rendered_prose,
        )
        self.assertNotIn("<!-- hidden", rendered_prose)
        self.assertNotIn("```fence", rendered_prose)
        self.assertEqual(result["markdown"].count("```"), 6)
        brief = result["markdown"].split("```text\nTask:", 1)[1].split("\n```", 1)[0]
        self.assertIn("List<T>", brief)
        self.assertIn("a && b", brief)
        self.assertIn("` ` `not a fence` ` `", brief)
        self.assertNotIn("List&lt;T&gt;", brief)

    def test_repeat_review_findings_are_bounded_but_not_by_recent_history_limit(self):
        findings = []
        context_hashes = {}
        for index in range(35):
            path = f"backend/api/repeat_{index}.py"
            findings.append(
                dict(
                    self.finding,
                    path=path,
                    anchor=f"repeat route {index}",
                    line=index + 1,
                )
            )
            context_hashes[path] = f"{index:040x}"[-40:]
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "b" * 40,
            findings,
            context_hashes=context_hashes,
        )
        context = memory_db.memory_context(
            self.connection,
            "eneo/platform",
            [item["path"] for item in findings],
            pr_number=17,
        )
        self.assertEqual(len(context["recent_findings"]), 30)
        self.assertEqual(len(context["repeat_review_findings"]), 35)
        self.assertNotIn("evidence", context["repeat_review_findings"][0])
        self.assertIn("prior_claim", context["repeat_review_findings"][0])

    def test_repeat_review_findings_keep_latest_observation_per_fingerprint(self):
        first = self.record(line=42, context_hash="d" * 40, head_sha="a" * 40)
        second = self.record(line=99, context_hash="e" * 40, head_sha="b" * 40)
        self.assertEqual(first["fingerprint"], second["fingerprint"])

        context = memory_db.memory_context(
            self.connection,
            "eneo/platform",
            ["backend/api/documents.py"],
            pr_number=17,
        )

        self.assertEqual(len(context["repeat_review_findings"]), 1)
        repeat = context["repeat_review_findings"][0]
        self.assertEqual(repeat["fingerprint"], first["fingerprint"])
        self.assertEqual(repeat["line"], 99)
        self.assertEqual(repeat["context_hash"], "e" * 40)
        self.assertNotIn("smallest_fix", repeat)
        self.assertEqual(repeat["prior_smallest_fix"], self.finding["smallest_fix"])
        self.assertEqual(repeat["previous_head"], "b" * 40)

    def test_record_findings_rejects_runaway_batch(self):
        findings = [
            dict(self.finding, path=f"backend/api/runaway_{index}.py")
            for index in range(memory_db.MAX_FINDINGS_PER_REVIEW + 1)
        ]
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.record_findings(
                self.connection,
                "eneo/platform",
                17,
                "b" * 40,
                findings,
                context_hashes={},
            )

    def test_fingerprint_prefix_can_be_used_for_human_triage(self):
        result = self.record()
        prefix = result["fingerprint"][:12]
        self.assertEqual(
            memory_db.resolve_fingerprint(self.connection, prefix), result["fingerprint"]
        )

    def test_operator_decision_requires_explicit_observation_target(self):
        result = self.record()

        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "exactly one target"):
            memory_db.add_decision(
                self.connection,
                result["fingerprint"],
                "resolved",
                "Fixed with a regression test.",
                "github:alice",
            )

    def test_operator_decision_can_target_observation_id(self):
        result = self.record()

        decision = memory_db.add_decision(
            self.connection,
            result["fingerprint"],
            "resolved",
            "Fixed with a regression test.",
            "github:alice",
            observation_id=result["observation_id"],
        )

        self.assertEqual(decision["observation_id"], result["observation_id"])
        self.assertEqual(decision["context_hash"], "d" * 40)

    def test_operator_decision_can_target_pr_local_reference(self):
        result = self.record()

        decision = memory_db.add_decision(
            self.connection,
            result["fingerprint"],
            "resolved",
            "Fixed with a regression test.",
            "github:alice",
            repository="eneo/platform",
            pr_number=17,
            local_reference="F1",
        )

        self.assertEqual(decision["observation_id"], result["observation_id"])

    def test_weak_finding_is_rejected(self):
        weak = dict(self.finding, confidence=0.84)
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.record_findings(
                self.connection,
                "eneo/platform",
                1,
                "b" * 40,
                [weak],
                context_hashes={weak["path"]: "c" * 40},
            )

    def test_low_publication_score_is_rejected(self):
        weak = dict(self.finding, publication_score=7)
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.record_findings(
                self.connection,
                "eneo/platform",
                1,
                "b" * 40,
                [weak],
                context_hashes={weak["path"]: "c" * 40},
            )

    def test_medium_and_low_use_lower_score_gate(self):
        medium = dict(
            self.finding,
            severity="Medium",
            publication_score=7,
            rule_id="tests.missing-regression",
            category="tests",
            path="backend/api/medium.py",
            anchor="medium",
        )
        low = dict(
            self.finding,
            severity="Low",
            publication_score=7,
            rule_id="maintainability.small-cleanup",
            category="maintainability",
            path="backend/api/low.py",
            anchor="low",
        )
        recorded = memory_db.record_findings(
            self.connection,
            "eneo/platform",
            1,
            "b" * 40,
            [medium],
            context_hashes={medium["path"]: "c" * 40},
        )
        self.assertEqual(recorded[0]["path"], medium["path"])

        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.record_findings(
                self.connection,
                "eneo/platform",
                1,
                "b" * 40,
                [dict(low, publication_score=6)],
                context_hashes={low["path"]: "d" * 40},
            )

    def test_lower_priority_findings_can_mix_with_high_priority(self):
        medium = dict(
            self.finding,
            severity="Medium",
            publication_score=7,
            rule_id="tests.missing-regression",
            category="tests",
            path="backend/api/medium.py",
            anchor="medium",
        )
        high = dict(
            self.finding,
            path="backend/api/high.py",
            anchor="high",
        )
        recorded = memory_db.record_findings(
            self.connection,
            "eneo/platform",
            1,
            "b" * 40,
            [high, medium],
            context_hashes={
                high["path"]: "c" * 40,
                medium["path"]: "d" * 40,
            },
        )
        self.assertEqual(
            {item["path"] for item in recorded}, {high["path"], medium["path"]}
        )

    def test_suppressed_high_does_not_block_one_medium(self):
        high = dict(
            self.finding,
            path="backend/api/high.py",
            anchor="high",
        )
        first = memory_db.record_findings(
            self.connection,
            "eneo/platform",
            1,
            "b" * 40,
            [high],
            context_hashes={high["path"]: "c" * 40},
        )[0]
        memory_db.add_decision(
            self.connection,
            first["fingerprint"],
            "false_positive",
            "Covered by a verified guard.",
            "github:alice",
            latest=True,
        )
        medium = dict(
            self.finding,
            severity="Medium",
            publication_score=7,
            rule_id="tests.missing-regression",
            category="tests",
            path="backend/api/medium.py",
            anchor="medium",
        )
        recorded = memory_db.record_findings(
            self.connection,
            "eneo/platform",
            1,
            "b" * 40,
            [high, medium],
            context_hashes={
                high["path"]: "c" * 40,
                medium["path"]: "d" * 40,
            },
        )
        by_path = {item["path"]: item for item in recorded}
        self.assertTrue(by_path[high["path"]]["suppressed"])
        self.assertFalse(by_path[medium["path"]]["suppressed"])

    def test_multiple_lower_priority_findings_are_allowed(self):
        medium = dict(
            self.finding,
            severity="Medium",
            publication_score=7,
            rule_id="tests.missing-regression",
            category="tests",
            path="backend/api/medium.py",
            anchor="medium",
        )
        low = dict(
            self.finding,
            severity="Low",
            publication_score=7,
            rule_id="maintainability.small-cleanup",
            category="maintainability",
            path="backend/api/low.py",
            anchor="low",
        )
        recorded = memory_db.record_findings(
            self.connection,
            "eneo/platform",
            1,
            "b" * 40,
            [medium, low],
            context_hashes={
                medium["path"]: "c" * 40,
                low["path"]: "d" * 40,
            },
        )
        self.assertEqual({item["path"] for item in recorded}, {medium["path"], low["path"]})

    def test_legacy_severity_check_schema_migrates(self):
        self.connection.close()
        db = str(Path(self.temp.name) / "legacy.sqlite3")
        created = memory_db.isoformat()
        raw = sqlite3.connect(db)
        raw.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE findings (
                fingerprint TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                path TEXT NOT NULL,
                line INTEGER,
                symbol TEXT NOT NULL DEFAULT '',
                anchor TEXT NOT NULL,
                title TEXT NOT NULL,
                severity TEXT NOT NULL CHECK (severity IN ('Critical', 'High')),
                category TEXT NOT NULL DEFAULT 'correctness',
                publication_score INTEGER NOT NULL DEFAULT 8,
                confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                context_hash TEXT NOT NULL DEFAULT '',
                pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                evidence TEXT NOT NULL,
                disproof_checks TEXT NOT NULL DEFAULT '',
                impact TEXT NOT NULL DEFAULT '',
                smallest_fix TEXT NOT NULL,
                introduced_by_diff INTEGER NOT NULL CHECK (introduced_by_diff IN (0, 1)),
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL,
                decision TEXT NOT NULL CHECK (
                    decision IN ('false_positive', 'accepted_risk', 'duplicate', 'resolved', 'reopen')
                ),
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                context_hash TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                expires_at TEXT,
                FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint)
            );
            CREATE TABLE decision_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id INTEGER NOT NULL,
                actor_user_id TEXT NOT NULL,
                actor_login TEXT NOT NULL DEFAULT '',
                author_association TEXT NOT NULL DEFAULT '',
                allowlist_version TEXT NOT NULL DEFAULT '',
                review_comment_id INTEGER,
                source_comment_id INTEGER,
                source_comment_url TEXT NOT NULL DEFAULT '',
                classifier_version TEXT NOT NULL DEFAULT '',
                classifier_output TEXT NOT NULL DEFAULT '',
                hmac_key_version TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (decision_id) REFERENCES decisions(id)
            );
            CREATE TABLE review_comment_links (
                review_comment_id INTEGER PRIMARY KEY,
                repository TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                context_hash TEXT NOT NULL DEFAULT '',
                head_sha TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                FOREIGN KEY (fingerprint) REFERENCES findings(fingerprint)
            );
            """
        )
        fingerprint = memory_db.compute_fingerprint(
            "eneo/platform", "tenant.missing-scope", "backend/api/documents.py",
            "create_document", "POST /v1/documents:create",
        )
        raw.execute(
            """
            INSERT INTO findings (
                fingerprint, repository, rule_id, path, line, symbol, anchor, title,
                severity, category, publication_score, confidence, context_hash, pr_number,
                head_sha, evidence, disproof_checks, impact, smallest_fix,
                introduced_by_diff, first_seen_at, last_seen_at, occurrences
            ) VALUES (?, 'eneo/platform', 'tenant.missing-scope', 'backend/api/documents.py',
                42, 'create_document', 'POST /v1/documents:create',
                'Document creation omits tenant scope', 'High', 'security', 9, 0.93,
                ?, 17, ?, 'evidence', 'checks', 'impact', 'fix', 1, ?, ?, 1)
            """,
            (fingerprint, "d" * 40, "a" * 40, created, created),
        )
        decision = raw.execute(
            """
            INSERT INTO decisions (
                fingerprint, decision, reason, actor, context_hash, created_at, expires_at
            ) VALUES (?, 'false_positive', 'reason', 'github:alice', ?, ?, NULL)
            """,
            (fingerprint, "d" * 40, created),
        )
        raw.execute(
            """
            INSERT INTO decision_audit (
                decision_id, actor_user_id, actor_login, author_association,
                allowlist_version, review_comment_id, source_comment_id,
                source_comment_url, classifier_version, classifier_output,
                hmac_key_version, created_at
            ) VALUES (?, '12345', 'alice', 'MEMBER', 'v1', 111, 222,
                'https://github.test/comment/222', 'classifier-v1', '{}', 'hmac-v1', ?)
            """,
            (decision.lastrowid, created),
        )
        raw.execute(
            """
            INSERT INTO review_comment_links (
                review_comment_id, repository, pr_number, fingerprint, context_hash,
                head_sha, created_at
            ) VALUES (111, 'eneo/platform', 17, ?, ?, ?, ?)
            """,
            (fingerprint, "d" * 40, "a" * 40, created),
        )
        raw.commit()
        raw.close()

        self.connection = memory_db.connect(db)
        schema = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'findings'"
        ).fetchone()["sql"]
        self.assertNotIn("severity IN ('Critical', 'High')", schema)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1
        )
        decision = self.connection.execute(
            "SELECT observation_id FROM decisions"
        ).fetchone()
        self.assertIsNone(decision["observation_id"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM finding_observations"
            ).fetchone()[0],
            1,
        )
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'review_comment_links'"
            ).fetchone()
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0],
            1,
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        memory_db.init_schema(self.connection)
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

        medium = dict(
            self.finding,
            severity="Medium",
            publication_score=7,
            rule_id="tests.missing-regression",
            category="tests",
            path="backend/api/medium.py",
            anchor="medium",
        )
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            18,
            "b" * 40,
            [medium],
            context_hashes={medium["path"]: "e" * 40},
        )

    def test_legacy_backfill_is_gated_after_schema_version_is_set(self):
        self.connection.close()
        db = str(Path(self.temp.name) / "backfill.sqlite3")
        created = memory_db.isoformat()
        fingerprint = memory_db.compute_fingerprint(
            "eneo/platform",
            "tenant.missing-scope",
            "backend/api/documents.py",
            "create_document",
            "POST /v1/documents:create",
        )
        raw = sqlite3.connect(db)
        raw.executescript(
            """
            CREATE TABLE findings (
                fingerprint TEXT PRIMARY KEY,
                repository TEXT NOT NULL,
                rule_id TEXT NOT NULL,
                path TEXT NOT NULL,
                line INTEGER,
                symbol TEXT NOT NULL DEFAULT '',
                anchor TEXT NOT NULL,
                title TEXT NOT NULL,
                severity TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'correctness',
                publication_score INTEGER NOT NULL DEFAULT 8,
                confidence REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                context_hash TEXT NOT NULL DEFAULT '',
                pr_number INTEGER NOT NULL,
                head_sha TEXT NOT NULL,
                evidence TEXT NOT NULL,
                disproof_checks TEXT NOT NULL DEFAULT '',
                impact TEXT NOT NULL DEFAULT '',
                smallest_fix TEXT NOT NULL,
                introduced_by_diff INTEGER NOT NULL CHECK (introduced_by_diff IN (0, 1)),
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                occurrences INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        raw.execute(
            """
            INSERT INTO findings (
                fingerprint, repository, rule_id, path, line, symbol, anchor, title,
                severity, category, publication_score, confidence, context_hash, pr_number,
                head_sha, evidence, disproof_checks, impact, smallest_fix,
                introduced_by_diff, first_seen_at, last_seen_at, occurrences
            ) VALUES (?, 'eneo/platform', 'tenant.missing-scope', 'backend/api/documents.py',
                42, 'create_document', 'POST /v1/documents:create',
                'Document creation omits tenant scope', 'High', 'security', 9, 0.93,
                ?, 17, ?, 'evidence', 'checks', 'impact', 'fix', 1, ?, ?, 1)
            """,
            (fingerprint, "d" * 40, "a" * 40, created, created),
        )
        raw.commit()
        raw.close()

        with patch.object(
            memory_schema,
            "_backfill_observations",
            wraps=memory_schema._backfill_observations,
        ) as backfill:
            self.connection = memory_db.connect(db)
            self.assertEqual(backfill.call_count, 1)
        self.connection.close()

        with patch.object(
            memory_schema,
            "_backfill_observations",
            wraps=memory_schema._backfill_observations,
        ) as backfill:
            self.connection = memory_db.connect(db)
            self.assertEqual(backfill.call_count, 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM finding_observations"
            ).fetchone()[0],
            1,
        )

    def test_missing_line_is_rejected(self):
        invalid = dict(self.finding)
        invalid.pop("line")
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.record_findings(
                self.connection,
                "eneo/platform",
                1,
                "c" * 40,
                [invalid],
                context_hashes={invalid["path"]: "d" * 40},
            )

    def test_invalid_head_sha_is_rejected(self):
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.record_findings(
                self.connection,
                "eneo/platform",
                1,
                "not-a-sha",
                [self.finding],
                context_hashes={self.finding["path"]: "d" * 40},
            )

    def test_path_traversal_is_rejected(self):
        invalid = dict(self.finding, path="../secrets.env")
        with self.assertRaises(memory_db.ReviewMemoryError):
            memory_db.record_findings(
                self.connection,
                "eneo/platform",
                1,
                "b" * 40,
                [invalid],
                context_hashes={invalid["path"]: "c" * 40},
            )

    def test_export_state_round_trips_findings_and_decisions(self):
        result = self.record()
        memory_db.add_decision(
            self.connection,
            result["fingerprint"],
            "false_positive",
            "PostgreSQL RLS enforces tenant scope for this application role.",
            "github:alice",
            expires_days=180,
            latest=True,
        )
        state = memory_db.export_state(self.connection)
        self.assertEqual(state["schema_version"], memory_db.SCHEMA_VERSION)
        self.assertEqual(len(state["findings"]), 1)
        self.assertEqual(len(state["review_subjects"]), 1)
        self.assertEqual(len(state["finding_observations"]), 1)
        self.assertEqual(len(state["pr_finding_references"]), 1)
        self.assertEqual(len(state["decisions"]), 1)
        self.assertEqual(state["findings"][0]["fingerprint"], result["fingerprint"])
        self.assertEqual(state["decisions"][0]["observation_id"], result["observation_id"])


if __name__ == "__main__":
    unittest.main()
