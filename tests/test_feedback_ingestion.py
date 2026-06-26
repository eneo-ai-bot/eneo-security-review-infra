from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins" / "eneo_review_tools"
sys.path.insert(0, str(PLUGIN))

import memory_db  # noqa: E402


class FeedbackIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = memory_db.connect(str(Path(self.temp.name) / "memory.sqlite3"))
        self._runs: dict[tuple[int, str, str], int] = {}
        self.finding = {
            "rule_id": "tenant.missing-scope",
            "category": "security",
            "path": "backend/api/documents.py",
            "line": 42,
            "symbol": "create_document",
            "anchor": "POST /v1/documents",
            "title": "Document creation omits tenant scope",
            "severity": "High",
            "publication_score": 9,
            "confidence": 0.93,
            "evidence": "The changed query writes a caller-controlled tenant id.",
            "disproof_checks": "Checked the dependency and repository layer.",
            "impact": "Cross-tenant write.",
            "smallest_fix": "Bind tenant_id from context.",
            "introduced_by_diff": True,
        }

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def record(
        self,
        *,
        pr_number: int = 17,
        head_sha: str = "a" * 40,
        base_sha: str = "b" * 40,
        context_hash: str = "d" * 40,
        findings: list[dict[str, object]] | None = None,
    ) -> list[dict[str, object]]:
        payload = findings if findings is not None else [dict(self.finding)]
        return memory_db.record_findings(
            self.connection,
            "eneo/platform",
            pr_number,
            head_sha,
            payload,
            review_run_id=self.run_for(
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha=base_sha,
            ),
            base_sha=base_sha,
            context_hashes={str(item["path"]): context_hash for item in payload},
        )

    def run_for(
        self,
        *,
        pr_number: int = 17,
        head_sha: str = "a" * 40,
        base_sha: str = "b" * 40,
    ) -> int:
        key = (pr_number, head_sha, base_sha)
        run_id = self._runs.get(key)
        if run_id is not None:
            row = self.connection.execute(
                "SELECT status FROM review_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row and row["status"] == "running":
                return run_id
        run = memory_db.start_run(
            self.connection,
            "eneo/platform",
            pr_number,
            base_sha=base_sha,
            head_sha=head_sha,
        )
        if run["status"] == "duplicate":
            run = memory_db.start_run(
                self.connection,
                "eneo/platform",
                pr_number,
                base_sha=base_sha,
                head_sha=head_sha,
                force=True,
            )
        run_id = int(run["id"])
        self._runs[key] = run_id
        return run_id

    def finalize(
        self,
        *,
        pr_number: int = 17,
        head_sha: str = "a" * 40,
        base_sha: str = "b" * 40,
        previous_verdicts: object = None,
    ) -> dict[str, object]:
        result = memory_db.finalize_review(
            self.connection,
            "eneo/platform",
            pr_number,
            head_sha,
            review_run_id=self.run_for(
                pr_number=pr_number,
                head_sha=head_sha,
                base_sha=base_sha,
            ),
            previous_verdicts=previous_verdicts,
        )
        run_id = int(result["review_run_id"])
        memory_db.mark_publication_posted(
            self.connection,
            publication_id=int(result["publication_id"]),
            review_run_id=run_id,
            comment_id=500 + int(result["publication_id"]),
        )
        memory_db.complete_run(
            self.connection,
            run_id,
            repository=str(result["repository"]),
            pr_number=int(result["pr_number"]),
            status="generated",
            findings_count=int(result["findings_count"]),
            posted_comment_id=500 + int(result["publication_id"]),
        )
        result["delivery_status"] = "posted"
        return result

    def feedback(
        self,
        body: str,
        *,
        event_id: str = "github:issue-comment:500",
        actor_user_id: int = 12345,
        source_comment_id: int = 500,
        allowlist: str = "12345",
    ):
        return memory_db.record_review_feedback_comment(
            self.connection,
            event_id=event_id,
            repository="eneo/platform",
            pr_number=17,
            body=body,
            actor_user_id=actor_user_id,
            actor_login="alice",
            author_association="OWNER",
            source_comment_id=source_comment_id,
            source_comment_url=f"https://github.test/eneo/platform/pull/17#issuecomment-{source_comment_id}",
            allowed_actor_ids=allowlist,
        )

    def test_finalize_records_exact_observation_id_on_publication_findings(self) -> None:
        recorded = self.record()[0]
        self.finalize()

        row = self.connection.execute(
            """
            SELECT local_reference, observation_id, context_hash
            FROM publication_findings
            WHERE local_reference = 'F1'
            """
        ).fetchone()

        self.assertEqual(row["observation_id"], recorded["observation_id"])
        self.assertEqual(row["context_hash"], "d" * 40)

    def test_feedback_ignores_generated_and_failed_publications(self) -> None:
        self.record()
        generated = memory_db.finalize_review(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            review_run_id=self.run_for(),
        )

        first = self.feedback(
            "/review false-positive F1 because the repository scopes tenant_id."
        )
        self.assertEqual(first.status, "no_mapping")

        memory_db.mark_publication_failed(
            self.connection,
            publication_id=int(generated["publication_id"]),
            review_run_id=int(generated["review_run_id"]),
            failure_code="body_too_large",
        )
        second = self.feedback(
            "/review false-positive F1 because the repository scopes tenant_id.",
            event_id="github:issue-comment:501",
            source_comment_id=501,
        )
        self.assertEqual(second.status, "no_mapping")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_carried_forward_publication_keeps_prior_observation_id(self) -> None:
        recorded = self.record()[0]
        self.finalize()
        self.record(head_sha="b" * 40, findings=[])
        self.finalize(head_sha="b" * 40)

        row = self.connection.execute(
            """
            SELECT observation_id, status
            FROM publication_findings
            JOIN review_publications ON review_publications.id = publication_findings.publication_id
            WHERE review_publications.head_sha = ?
              AND publication_findings.local_reference = 'F1'
            """,
            ("b" * 40,),
        ).fetchone()

        self.assertEqual(row["status"], "current")
        self.assertEqual(row["observation_id"], recorded["observation_id"])

    def test_current_publication_unique_index_blocks_duplicates(self) -> None:
        self.record()
        first = self.finalize()

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO review_publications (
                    repository, pr_number, head_sha, policy_revision, comment_id,
                    rendered_hash, delivery_status, published_at, generated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'posted', ?, ?)
                """,
                (
                    first["repository"],
                    first["pr_number"],
                    "b" * 40,
                    first["policy_revision"],
                    999,
                    "duplicate",
                    "2026-06-24T00:00:00Z",
                    "2026-06-24T00:00:00Z",
                ),
            )

    def test_parser_rejects_empty_reason_and_unsupported_commands(self) -> None:
        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "reason is required"):
            memory_db.parse_review_feedback_command("@review false-positive F1")

        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "governance CLI"):
            memory_db.parse_review_feedback_command("@review accepted-risk F1 reason")

        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "one feedback command"):
            memory_db.parse_review_feedback_command(
                "@review false-positive F1 reason\n@review feedback missed another"
            )

        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "replace placeholder"):
            memory_db.parse_review_feedback_command(
                "/review false-positive F1 because "
                "<what code, guard, or invariant disproves it>"
            )
        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "reason is required"):
            memory_db.parse_review_feedback_command("/review false-positive F1 because")
        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "reason is required"):
            memory_db.parse_review_feedback_command("/review feedback missed because")
        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "scope feedback"):
            memory_db.parse_review_feedback_command("/review feedback scope because")
        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "local_reference"):
            memory_db.parse_review_feedback_command("/review feedback scope Fx because branch noise")

        self.assertIsNone(memory_db.parse_review_feedback_command("@review"))

    def test_parser_strips_optional_leading_because_from_reasons(self) -> None:
        finding = memory_db.parse_review_feedback_command(
            "@review false-positive F1 because The repository scopes tenant_id."
        )
        missed = memory_db.parse_review_feedback_command(
            "/review feedback missed because backend/api/documents.py lacks rollback coverage."
        )
        scoped = memory_db.parse_review_feedback_command(
            "/review feedback scope F1 because This change is inherited branch noise."
        )

        self.assertIsNotNone(finding)
        self.assertIsNotNone(missed)
        self.assertIsNotNone(scoped)
        assert finding is not None
        assert missed is not None
        assert scoped is not None
        self.assertEqual(finding.reason, "The repository scopes tenant_id.")
        self.assertEqual(
            missed.reason,
            "backend/api/documents.py lacks rollback coverage.",
        )
        self.assertEqual(scoped.reason, "This change is inherited branch noise.")
        self.assertEqual(scoped.local_reference, "F1")
        self.assertEqual(scoped.category, "scope_confusion")

    def test_rendered_feedback_templates_match_parser_contract(self) -> None:
        for template in memory_db.feedback_templates("F2"):
            with self.subTest(command=template.command):
                with self.assertRaisesRegex(memory_db.ReviewMemoryError, "replace placeholder"):
                    memory_db.parse_review_feedback_command(template.command)
                command = template.command
                command = command.replace(
                    memory_db.FALSE_POSITIVE_PLACEHOLDER,
                    "the repository binds tenant_id before the query",
                )
                command = command.replace(
                    memory_db.MISSED_ISSUE_PLACEHOLDER,
                    "the review missed backend/api/documents.py rollback behavior",
                )
                command = command.replace(
                    memory_db.SCOPE_CONFUSION_PLACEHOLDER,
                    "the finding is inherited from a stacked branch",
                )
                parsed = memory_db.parse_review_feedback_command(command)
                self.assertIsNotNone(parsed)
                assert parsed is not None
                self.assertNotRegex(parsed.reason, r"(?i)^because\b")

    def test_authorization_uses_numeric_actor_id_and_fails_closed(self) -> None:
        self.assertEqual(
            memory_db.authorize_feedback_actor(12345, allowed_actor_ids="12345,678").actor_user_id,
            "12345",
        )
        for actor in [True, 0, -1, "12345", 999]:
            with self.subTest(actor=actor):
                self.assertIsNone(
                    memory_db.authorize_feedback_actor(actor, allowed_actor_ids="12345")
                )
        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "malformed actor id"):
            memory_db.authorize_feedback_actor(12345, allowed_actor_ids="12345,nope")

    def test_allowlisted_false_positive_records_decision_audit_and_replay(self) -> None:
        recorded = self.record()[0]
        self.finalize()

        result = self.feedback("@review false-positive F1 Existing guard disproves this.")
        replay = self.feedback("@review false-positive F1 Existing guard disproves this.")

        self.assertEqual(result.status, "recorded")
        self.assertEqual(result.decision_id, 1)
        self.assertEqual(replay.status, "replay")
        decision = self.connection.execute("SELECT * FROM decisions").fetchone()
        audit = self.connection.execute("SELECT * FROM decision_audit").fetchone()
        self.assertEqual(decision["observation_id"], recorded["observation_id"])
        self.assertEqual(decision["context_hash"], "d" * 40)
        self.assertEqual(audit["actor_user_id"], "12345")
        self.assertTrue(str(audit["allowlist_version"]).startswith("sha256:"))
        self.assertIsNone(audit["review_comment_id"])
        self.assertEqual(audit["source_comment_id"], 500)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 1)

    def test_audit_failure_rolls_back_event_and_decision(self) -> None:
        self.record()
        self.finalize()
        self.connection.execute(
            """
            CREATE TRIGGER fail_decision_audit
            BEFORE INSERT ON decision_audit
            BEGIN
                SELECT RAISE(FAIL, 'audit failed');
            END
            """
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.feedback("@review false-positive F1 Existing guard disproves this.")

        self.assertIsNone(memory_db.feedback_event(self.connection, "github:issue-comment:500"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM decision_audit").fetchone()[0],
            0,
        )

        self.connection.execute("DROP TRIGGER fail_decision_audit")
        result = self.feedback("@review false-positive F1 Existing guard disproves this.")
        self.assertEqual(result.status, "recorded")

    def test_unauthorized_actor_does_not_claim_event(self) -> None:
        self.record()
        self.finalize()

        result = self.feedback(
            "@review false-positive F1 Existing guard disproves this.",
            actor_user_id=999,
        )

        self.assertEqual(result.status, "unauthorized")
        self.assertIsNone(memory_db.feedback_event(self.connection, "github:issue-comment:500"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_unauthorized_malformed_feedback_fails_closed_before_parse(self) -> None:
        self.record()
        self.finalize()

        result = self.feedback(
            "@review accepted-risk F1 do not parse this",
            actor_user_id=999,
        )

        self.assertEqual(result.status, "unauthorized")
        self.assertIsNone(memory_db.feedback_event(self.connection, "github:issue-comment:500"))

    def test_resolved_reference_is_not_current(self) -> None:
        first = dict(self.finding)
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
        self.record(findings=[first, second])
        self.finalize()
        self.record(head_sha="b" * 40, findings=[second], context_hash="e" * 40)
        self.finalize(
            head_sha="b" * 40,
            previous_verdicts=[
                {"local_reference": "F1", "verdict": "resolved", "evidence": "Fixed."},
                {"local_reference": "F2", "verdict": "still_present"},
            ],
        )

        result = self.feedback("@review false-positive F1 Existing guard disproves this.")

        self.assertEqual(result.status, "not_current")
        self.assertEqual(memory_db.feedback_event(self.connection, "github:issue-comment:500")["outcome"], "not_current")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_head_advanced_records_reviewed_observation_without_current_suppression(self) -> None:
        recorded = self.record()[0]
        self.finalize()
        self.record(head_sha="b" * 40, context_hash="e" * 40)

        result = self.feedback("@review false-positive F1 Existing guard disproves this.")

        self.assertEqual(result.status, "recorded")
        decision = self.connection.execute("SELECT * FROM decisions").fetchone()
        self.assertEqual(decision["observation_id"], recorded["observation_id"])
        self.assertEqual(decision["context_hash"], "d" * 40)
        self.assertIsNone(memory_db.active_suppression(self.connection, recorded["fingerprint"]))

    def test_missing_observation_and_empty_hash_fail_closed(self) -> None:
        self.record()
        self.finalize()
        self.connection.execute("UPDATE publication_findings SET observation_id = NULL")
        self.connection.commit()

        missing = self.feedback("@review false-positive F1 Existing guard disproves this.")
        self.assertEqual(missing.status, "no_mapping")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

        self.connection.execute("UPDATE publication_findings SET observation_id = 1, context_hash = ''")
        self.connection.commit()
        empty_hash = self.feedback(
            "@review false-positive F1 Existing guard disproves this.",
            event_id="github:issue-comment:501",
            source_comment_id=501,
        )
        self.assertEqual(empty_hash.status, "stale")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_intentional_requires_adr_id(self) -> None:
        self.record()
        self.finalize()

        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "ADR id"):
            self.feedback("@review intentional F1 this is intentional")

    def test_pr_comment_intentional_is_unsupported_without_adr_validation(self) -> None:
        self.record()
        self.finalize()

        result = self.feedback(
            "@review intentional F1 ADR-0042 This boundary is deliberate."
        )

        self.assertEqual(result.status, "unsupported")
        self.assertIsNone(memory_db.feedback_event(self.connection, "github:issue-comment:500"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 0)

    def test_feedback_missed_records_quality_feedback_and_replay_is_noop(self) -> None:
        publication = self.finalize_after_recording_empty_review()

        result = self.feedback("@review feedback missed The review missed rollback risk.")
        replay = self.feedback("@review feedback missed The review missed rollback risk.")

        self.assertEqual(result.status, "recorded")
        self.assertEqual(result.feedback_id, 1)
        self.assertEqual(replay.status, "replay")
        row = self.connection.execute("SELECT * FROM review_quality_feedback").fetchone()
        self.assertEqual(row["category"], "missed_issue")
        self.assertEqual(row["publication_id"], publication["publication_id"])
        self.assertEqual(row["head_sha"], "a" * 40)
        self.assertEqual(row["reason"], "The review missed rollback risk.")

    def test_feedback_scope_records_referenced_quality_feedback(self) -> None:
        self.record()
        publication = self.finalize()

        result = self.feedback(
            "@review feedback scope F1 because The finding is inherited branch noise."
        )

        self.assertEqual(result.status, "recorded")
        row = self.connection.execute("SELECT * FROM review_quality_feedback").fetchone()
        self.assertEqual(row["category"], "scope_confusion")
        self.assertEqual(row["local_reference"], "F1")
        self.assertEqual(row["publication_id"], publication["publication_id"])
        self.assertEqual(row["reason"], "The finding is inherited branch noise.")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
            0,
        )

    def test_feedback_scope_requires_current_reference(self) -> None:
        self.record()
        self.finalize()

        result = self.feedback(
            "@review feedback scope F2 because This reference is stale."
        )

        self.assertEqual(result.status, "not_current")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM review_quality_feedback"
            ).fetchone()[0],
            0,
        )

    def finalize_after_recording_empty_review(self) -> dict[str, object]:
        self.record(findings=[])
        return self.finalize()


if __name__ == "__main__":
    unittest.main()
