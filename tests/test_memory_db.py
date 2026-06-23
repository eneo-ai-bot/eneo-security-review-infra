from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins" / "eneo_review_tools"
sys.path.insert(0, str(PLUGIN))

import memory_db  # noqa: E402


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

    def record(self, line=42, context_hash="d" * 40):
        finding = dict(self.finding, line=line)
        return memory_db.record_findings(
            self.connection,
            "Eneo/Platform",
            17,
            "a" * 40,
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
        self.assertEqual(row["occurrences"], 2)
        self.assertEqual(row["line"], 97)

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
        self.assertEqual(state["schema_version"], 2)
        self.assertEqual(len(state["findings"]), 1)
        self.assertEqual(len(state["decisions"]), 1)
        self.assertEqual(state["findings"][0]["fingerprint"], result["fingerprint"])


if __name__ == "__main__":
    unittest.main()
