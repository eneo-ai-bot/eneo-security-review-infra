from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins" / "eneo_review_tools"
sys.path.insert(0, str(PLUGIN))

import memory_db  # noqa: E402


class VerificationReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "memory.sqlite3")
        self.connection = memory_db.connect(self.db_path)
        self.finding = {
            "rule_id": "tenant.missing-scope",
            "category": "security",
            "path": "backend/api/documents.py",
            "line": 42,
            "symbol": "create_document",
            "anchor": "POST /v1/documents:create",
            "title": "Document creation omits tenant scope",
            "severity": "High",
            "publication_score": 9,
            "confidence": 0.93,
            "evidence": "The changed query writes a caller-controlled tenant identifier.",
            "disproof_checks": "Checked the dependency and repository layer.",
            "impact": "A user can write into another tenant.",
            "smallest_fix": "Bind tenant_id from the verified request context.",
            "introduced_by_diff": True,
        }

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def start_run(self, *, head_sha: str = "a" * 40) -> int:
        run = memory_db.start_run(
            self.connection,
            "eneo/platform",
            17,
            base_sha="b" * 40,
            head_sha=head_sha,
        )
        return int(run["id"])

    def record_finding(
        self, run_id: int, *, head_sha: str = "a" * 40
    ) -> dict[str, object]:
        return memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            head_sha,
            [self.finding],
            review_run_id=run_id,
            base_sha="b" * 40,
            context_hashes={self.finding["path"]: "d" * 40},
        )[0]

    def finalize(
        self,
        run_id: int,
        *,
        head_sha: str = "a" * 40,
        previous_verdicts: object = None,
    ) -> dict[str, object]:
        return memory_db.finalize_review(
            self.connection,
            "eneo/platform",
            17,
            head_sha,
            review_run_id=run_id,
            previous_verdicts=previous_verdicts,
        )

    def test_no_verification_preserves_publish_all_behavior(self) -> None:
        run_id = self.start_run()
        self.record_finding(run_id)

        publication = self.finalize(run_id)

        self.assertEqual(publication["findings_count"], 1)
        self.assertIn(self.finding["title"], str(publication["markdown"]))

    def test_raw_refuted_verdict_does_not_drop_without_reconciliation(self) -> None:
        run_id = self.start_run()
        recorded = self.record_finding(run_id)
        verifier = memory_db.record_verification_run(
            self.connection,
            review_run_id=run_id,
            provider="claude",
            model="opus",
            mode="advise",
            status="completed",
        )
        memory_db.record_candidate_verification(
            self.connection,
            verification_run_id=int(verifier["id"]),
            observation_id=int(recorded["observation_id"]),
            verdict="refuted",
            confidence=0.91,
            counter_evidence="The route always binds tenant_id from request context.",
        )

        publication = self.finalize(run_id)

        self.assertEqual(publication["findings_count"], 1)
        self.assertIn(self.finding["title"], str(publication["markdown"]))

    def test_reconciliation_drop_excludes_candidate_from_publication(self) -> None:
        run_id = self.start_run()
        recorded = self.record_finding(run_id)
        verifier = memory_db.record_verification_run(
            self.connection,
            review_run_id=run_id,
            provider="claude",
            model="opus",
            mode="advise",
            status="completed",
        )
        memory_db.record_candidate_verification(
            self.connection,
            verification_run_id=int(verifier["id"]),
            observation_id=int(recorded["observation_id"]),
            verdict="refuted",
            confidence=0.91,
            counter_evidence="The route always binds tenant_id from request context.",
        )
        memory_db.record_candidate_reconciliation(
            self.connection,
            review_run_id=run_id,
            observation_id=int(recorded["observation_id"]),
            final_decision="drop",
            reason="Codex verified the request-scoped tenant guard.",
            verification_run_id=int(verifier["id"]),
        )

        publication = self.finalize(run_id)

        self.assertEqual(publication["findings_count"], 0)
        self.assertNotIn(self.finding["title"], str(publication["markdown"]))
        count = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM publication_findings
            WHERE publication_id = ? AND status = 'current'
            """,
            (publication["publication_id"],),
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_reconciliation_drop_excludes_prior_carried_candidate(self) -> None:
        first_run_id = self.start_run()
        self.record_finding(first_run_id)
        first_publication = self.finalize(first_run_id)
        self.assertEqual(first_publication["findings_count"], 1)
        memory_db.mark_publication_posted(
            self.connection,
            publication_id=int(first_publication["publication_id"]),
            review_run_id=first_run_id,
            comment_id=901,
        )
        memory_db.complete_run(
            self.connection,
            first_run_id,
            repository="eneo/platform",
            pr_number=17,
            findings_count=1,
        )

        second_head = "c" * 40
        second_run_id = self.start_run(head_sha=second_head)
        recorded = self.record_finding(second_run_id, head_sha=second_head)
        verifier = memory_db.record_verification_run(
            self.connection,
            review_run_id=second_run_id,
            provider="claude",
            model="opus",
            mode="advise",
            status="completed",
        )
        memory_db.record_candidate_reconciliation(
            self.connection,
            review_run_id=second_run_id,
            observation_id=int(recorded["observation_id"]),
            final_decision="drop",
            reason="Codex verified this prior finding is outside the PR scope.",
            verification_run_id=int(verifier["id"]),
        )

        second_publication = self.finalize(
            second_run_id,
            head_sha=second_head,
            previous_verdicts=[
                {"local_reference": "F1", "verdict": "still_present"}
            ],
        )

        self.assertEqual(second_publication["findings_count"], 0)
        self.assertNotIn("### F1", str(second_publication["markdown"]))
        self.assertIn("F1 withdrawn after recheck", str(second_publication["markdown"]))
        rows = self.connection.execute(
            """
            SELECT status
            FROM publication_findings
            WHERE publication_id = ?
            """,
            (second_publication["publication_id"],),
        ).fetchall()
        self.assertEqual([row["status"] for row in rows], ["resolved"])

        memory_db.mark_publication_posted(
            self.connection,
            publication_id=int(second_publication["publication_id"]),
            review_run_id=second_run_id,
            comment_id=902,
        )
        memory_db.complete_run(
            self.connection,
            second_run_id,
            repository="eneo/platform",
            pr_number=17,
            findings_count=0,
        )
        context = memory_db.memory_context(
            self.connection,
            "eneo/platform",
            ["backend/api/documents.py"],
            pr_number=17,
        )
        self.assertEqual(context["repeat_review_findings"], [])

    def test_reconciliation_publish_records_provenance_without_changing_output(self) -> None:
        run_id = self.start_run()
        recorded = self.record_finding(run_id)
        verifier = memory_db.record_verification_run(
            self.connection,
            review_run_id=run_id,
            provider="claude",
            model="opus",
            mode="advise",
            status="completed",
        )
        memory_db.record_candidate_reconciliation(
            self.connection,
            review_run_id=run_id,
            observation_id=int(recorded["observation_id"]),
            final_decision="publish",
            reason="Codex verified the finding still has direct diff evidence.",
            verification_run_id=int(verifier["id"]),
        )

        publication = self.finalize(run_id)

        self.assertEqual(publication["findings_count"], 1)
        self.assertIn(self.finding["title"], str(publication["markdown"]))
        row = self.connection.execute(
            """
            SELECT final_decision, reason
            FROM candidate_reconciliations
            WHERE review_run_id = ?
            """,
            (run_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["final_decision"], "publish")
        self.assertIn("direct diff evidence", row["reason"])

    def test_verifier_status_is_operator_visible_not_public_markdown(self) -> None:
        run_id = self.start_run()
        self.record_finding(run_id)
        memory_db.record_verification_run(
            self.connection,
            review_run_id=run_id,
            provider="claude",
            model="opus",
            mode="advise",
            status="unavailable",
            failure_code="timeout",
        )

        publication = self.finalize(run_id)
        publications = memory_db.list_publications(
            self.connection,
            repository="eneo/platform",
            pr_number=17,
        )

        self.assertEqual(publications[0]["verification_status"], "unavailable")
        self.assertEqual(publications[0]["verification_failure_code"], "timeout")
        markdown = str(publication["markdown"])
        self.assertNotIn("verification=", markdown)
        self.assertNotIn("verifier", markdown.lower())
        self.assertNotIn("claude", markdown.lower())

    def test_reconciliation_cannot_change_a_finalized_publication(self) -> None:
        run_id = self.start_run()
        recorded = self.record_finding(run_id)
        publication = self.finalize(run_id)

        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "already has a publication"):
            memory_db.record_candidate_reconciliation(
                self.connection,
                review_run_id=run_id,
                observation_id=int(recorded["observation_id"]),
                final_decision="drop",
                reason="Late verifier result should not mutate published output.",
            )

        again = self.finalize(run_id)
        self.assertEqual(again["publication_id"], publication["publication_id"])
        self.assertEqual(again["findings_count"], 1)


if __name__ == "__main__":
    unittest.main()
