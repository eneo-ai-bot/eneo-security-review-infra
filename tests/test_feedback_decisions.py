from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins" / "eneo_review_tools"
sys.path.insert(0, str(PLUGIN))

import memory_db  # noqa: E402


class FeedbackDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.connection = memory_db.connect(str(Path(self.temp.name) / "memory.sqlite3"))
        self.finding = {
            "rule_id": "tenant.missing-scope",
            "category": "security",
            "path": "backend/api/documents.py",
            "line": 42,
            "symbol": "create_document",
            "anchor": "POST /v1/documents",
            "title": "Document creation omits tenant scope",
            "severity": "Critical",
            "publication_score": 9,
            "confidence": 0.93,
            "evidence": "The changed query writes a caller-controlled tenant id.",
            "disproof_checks": "Checked the dependency and repository layer.",
            "impact": "Cross-tenant write.",
            "smallest_fix": "Bind tenant_id from the verified request context.",
            "introduced_by_diff": True,
        }

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def record_finding(self, context_hash="d" * 40):
        recorded = memory_db.record_findings(
            self.connection, "eneo-ai/eneo", 498, "a" * 40, [dict(self.finding)],
            context_hashes={self.finding["path"]: context_hash},
        )[0]
        return recorded["fingerprint"], context_hash

    def link(self, fingerprint, context_hash, comment_id=111):
        memory_db.link_review_comment(
            self.connection, review_comment_id=comment_id, repository="eneo-ai/eneo",
            pr_number=498, fingerprint=fingerprint, context_hash=context_hash, head_sha="a" * 40,
        )

    def feedback(self, **kw):
        base = dict(
            event_id="evt-1", review_comment_id=111, decision="false_positive",
            reason="RLS enforces tenant scope for this application role.", actor_user_id="12345",
        )
        base.update(kw)
        return memory_db.record_feedback_decision(self.connection, **base)

    def test_link_and_lookup(self):
        fp, ch = self.record_finding()
        self.link(fp, ch, comment_id=111)
        link = memory_db.finding_for_review_comment(self.connection, 111)
        self.assertIsNotNone(link)
        self.assertEqual(link["fingerprint"], fp)
        self.assertIsNone(memory_db.finding_for_review_comment(self.connection, 999))

    def test_link_is_idempotent_but_not_repointed(self):
        fp, ch = self.record_finding()
        self.link(fp, ch, comment_id=111)
        self.link(fp, ch, comment_id=111)

        other = dict(
            self.finding,
            path="backend/api/users.py",
            anchor="POST /v1/users",
            symbol="create_user",
        )
        recorded = memory_db.record_findings(
            self.connection, "eneo-ai/eneo", 498, "a" * 40, [other],
            context_hashes={other["path"]: "e" * 40},
        )[0]
        with self.assertRaises(memory_db.ReviewMemoryError):
            self.link(recorded["fingerprint"], "e" * 40, comment_id=111)

    def test_record_false_positive_suppresses(self):
        fp, ch = self.record_finding()
        self.link(fp, ch)
        result = self.feedback(actor_login="ccimen", author_association="MEMBER")
        self.assertEqual(result["status"], "recorded")
        self.assertEqual(result["fingerprint"], fp)
        self.assertIsNotNone(memory_db.active_suppression(self.connection, fp))
        self.assertIsNotNone(memory_db.feedback_event(self.connection, "evt-1"))

    def test_replay_is_noop(self):
        fp, ch = self.record_finding()
        self.link(fp, ch)
        self.assertEqual(self.feedback(event_id="evt-x")["status"], "recorded")
        self.assertIsNone(self.feedback(event_id="evt-x"))  # same event id -> no-op

    def test_no_mapping_is_noop_without_claiming_event(self):
        self.assertIsNone(self.feedback(event_id="evt-2", review_comment_id=777))
        self.assertIsNone(memory_db.feedback_event(self.connection, "evt-2"))

    def test_stale_when_file_changed(self):
        fp, _ = self.record_finding(context_hash="d" * 40)
        self.link(fp, "d" * 40)
        self.record_finding(context_hash="e" * 40)  # file changed since the comment
        result = self.feedback(event_id="evt-3")
        self.assertEqual(result["status"], "stale")
        self.assertIsNotNone(memory_db.feedback_event(self.connection, "evt-3"))
        self.assertIsNone(memory_db.active_suppression(self.connection, fp))

    def test_reopen_restores(self):
        fp, ch = self.record_finding()
        self.link(fp, ch)
        self.feedback(event_id="evt-4")
        self.assertIsNotNone(memory_db.active_suppression(self.connection, fp))
        self.feedback(event_id="evt-5", decision="reopen", reason="actually a real issue")
        self.assertIsNone(memory_db.active_suppression(self.connection, fp))

    def test_accepted_risk_rejected(self):
        fp, ch = self.record_finding()
        self.link(fp, ch)
        with self.assertRaises(memory_db.ReviewMemoryError):
            self.feedback(event_id="evt-6", decision="accepted_risk")

    def test_audit_row_recorded(self):
        fp, ch = self.record_finding()
        self.link(fp, ch)
        self.feedback(
            event_id="evt-7", actor_login="ccimen", author_association="MEMBER",
            allowlist_version="v1", source_comment_id=222, classifier_version="c1",
        )
        audit = self.connection.execute("SELECT * FROM decision_audit").fetchall()
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["actor_user_id"], "12345")
        self.assertEqual(audit[0]["review_comment_id"], 111)
        self.assertEqual(audit[0]["source_comment_id"], 222)

    def test_audit_failure_rolls_back_event_and_decision(self):
        fp, ch = self.record_finding()
        self.link(fp, ch)
        self.connection.execute(
            """
            CREATE TRIGGER fail_decision_audit
            BEFORE INSERT ON decision_audit
            BEGIN
                SELECT RAISE(FAIL, 'audit failed');
            END
            """
        )
        with self.assertRaises(Exception):
            self.feedback(event_id="evt-8")
        self.assertIsNone(memory_db.feedback_event(self.connection, "evt-8"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0], 0
        )

        self.connection.execute("DROP TRIGGER fail_decision_audit")
        result = self.feedback(event_id="evt-8")
        self.assertEqual(result["status"], "recorded")


if __name__ == "__main__":
    unittest.main()
