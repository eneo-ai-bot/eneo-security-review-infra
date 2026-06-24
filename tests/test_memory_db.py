from __future__ import annotations

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


class ReviewMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "memory.sqlite3")
        self.connection = memory_db.connect(self.db)
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
        )
        self.assertIsNotNone(memory_db.active_suppression(self.connection, fingerprint))

        memory_db.add_decision(
            self.connection,
            fingerprint,
            "reopen",
            "The migration removed FORCE ROW LEVEL SECURITY.",
            "github:bob",
        )
        self.assertIsNone(memory_db.active_suppression(self.connection, fingerprint))

    def test_file_change_invalidates_suppression(self):
        first = self.record(context_hash="d" * 40)
        memory_db.add_decision(
            self.connection,
            first["fingerprint"],
            "false_positive",
            "A verified guard exists in the unchanged implementation.",
            "github:alice",
            expires_days=180,
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
            "There are 2 current findings: 1 Critical / P0 and 1 Medium / P2.",
            markdown,
        )
        self.assertIn("### F1 - Critical / P0: Document creation omits tenant scope", markdown)
        self.assertIn("### F2 - Medium / P2: Regression test misses tenant failure path", markdown)
        self.assertIn("Copyable fix brief for a coding agent", markdown)
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

        self.assertIn("There are 2 current findings: 2 High / P1.", result["markdown"])
        self.assertNotIn("I found 2 High / P1 finding.", result["markdown"])

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
        self.assertIn("Review updated for the latest commit.", result["markdown"])
        self.assertIn("Needs recheck: F1", result["markdown"])
        self.assertIn("Still present: F2", result["markdown"])
        self.assertIn(
            "### F1 - Critical / P0: Document creation omits tenant scope",
            result["markdown"],
        )
        self.assertIn("F1 - Critical / P0 - security", result["markdown"])
        self.assertNotIn("Resolved since the previous review: F1", result["markdown"])

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

        self.assertIn("Still present: F2", result["markdown"])
        self.assertIn("Needs recheck: F1", result["markdown"])
        self.assertIn("New findings: F3", result["markdown"])
        self.assertIn(
            "### F3 - High / P1: Migration job can run without bounded retries",
            result["markdown"],
        )

    def test_finalize_review_escapes_model_text_in_markdown_layout(self):
        malicious = dict(
            self.finding,
            title="Break </details> <!-- hidden --> ``` fence",
            evidence="Evidence tries </details> and <!-- hidden --> plus ```fence.",
            impact="Impact closes </details> and starts <!-- hidden -->.",
            smallest_fix="Fix uses ```not a fence``` and keeps layout intact.",
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

        self.assertIn("&lt;/details&gt;", visible)
        self.assertIn("&lt;!-- hidden --&gt;", visible)
        self.assertNotIn("<!-- hidden", visible)
        self.assertNotIn("```fence", visible)
        self.assertEqual(result["markdown"].count("```"), 2)

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
        raw.execute(
            """
            INSERT INTO decisions (
                fingerprint, decision, reason, actor, context_hash, created_at, expires_at
            ) VALUES (?, 'false_positive', 'reason', 'github:alice', ?, ?, NULL)
            """,
            (fingerprint, "d" * 40, created),
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
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM review_comment_links").fetchone()[0],
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
        )
        state = memory_db.export_state(self.connection)
        self.assertEqual(state["schema_version"], memory_db.SCHEMA_VERSION)
        self.assertEqual(len(state["findings"]), 1)
        self.assertEqual(len(state["review_subjects"]), 1)
        self.assertEqual(len(state["finding_observations"]), 1)
        self.assertEqual(len(state["pr_finding_references"]), 1)
        self.assertEqual(len(state["decisions"]), 1)
        self.assertEqual(state["findings"][0]["fingerprint"], result["fingerprint"])


if __name__ == "__main__":
    unittest.main()
