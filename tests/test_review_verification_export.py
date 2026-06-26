from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "bootstrap" / "plugins" / "eneo_review_tools"
TOOLS = ROOT / "tools"
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(TOOLS))

import memory_db  # noqa: E402
from eneo_review_verification import (  # noqa: E402
    build_verification_export,
    dumps_verification_export,
)


class VerificationExportTests(unittest.TestCase):
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

    def generate_review(self) -> int:
        run = memory_db.start_run(
            self.connection,
            "eneo/platform",
            17,
            base_sha="b" * 40,
            head_sha="a" * 40,
        )
        run_id = int(run["id"])
        memory_db.register_changed_files(
            self.connection,
            run_id=run_id,
            repository="eneo/platform",
            pr_number=17,
            files=[{"path": "backend/api/documents.py", "status": "modified"}],
        )
        memory_db.record_diff_exposure(
            self.connection,
            run_id=run_id,
            repository="eneo/platform",
            pr_number=17,
            paths=["backend/api/documents.py"],
            truncated=False,
        )
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            [self.finding],
            review_run_id=run_id,
            base_sha="b" * 40,
            context_hashes={"backend/api/documents.py": "d" * 40},
        )
        publication = memory_db.finalize_review(
            self.connection,
            "eneo/platform",
            17,
            "a" * 40,
            review_run_id=run_id,
        )
        memory_db.complete_run(
            self.connection,
            run_id,
            repository="eneo/platform",
            pr_number=17,
            status="generated",
            findings_count=int(publication["findings_count"]),
        )
        return run_id

    def build_payload(self, run_id: int) -> dict[str, object]:
        return build_verification_export(
            memory_db.verification_export_source(
                self.connection,
                review_run_id=run_id,
            ),
            coverage=memory_db.coverage_summary(self.connection, run_id=run_id),
        )

    def test_verification_export_is_stable_and_bounded(self) -> None:
        run_id = self.generate_review()

        first = self.build_payload(run_id)
        second = self.build_payload(run_id)
        rendered = dumps_verification_export(first)
        decoded = json.loads(rendered)

        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["event_set_id"], second["event_set_id"])
        self.assertEqual(decoded["schema_version"], 1)
        self.assertEqual(decoded["source"], "review-verification-shadow")
        self.assertEqual(decoded["review_run_id"], run_id)
        self.assertEqual(decoded["coverage"]["state"], "complete")
        self.assertEqual(decoded["verification_mode"], {"kind": "shadow_non_gating"})
        self.assertEqual(len(decoded["findings"]), 1)
        finding = decoded["findings"][0]
        self.assertEqual(finding["local_reference"], "F1")
        self.assertIn("evidence_untrusted", finding)
        self.assertLessEqual(len(finding["evidence_untrusted"]), 1000)
        self.assertNotIn("do_not_publish", rendered)
        self.assertNotIn("do_not_execute_code", rendered)
        self.assertNotIn("rendered_markdown", rendered)
        self.assertNotIn("rendered_blocks_json", rendered)

    def test_export_omits_feedback_actor_and_source_url_rows(self) -> None:
        run_id = self.generate_review()
        self.connection.execute(
            """
            INSERT INTO review_quality_feedback (
                repository, pr_number, publication_id, head_sha, local_reference,
                category, reason, actor_user_id, actor_login, author_association,
                source_comment_id, source_comment_url, created_at
            ) VALUES (
                'eneo/platform', 17, 1, ?, 'F1', 'scope_confusion',
                'reviewed inherited branch work', '123', 'alice',
                'MEMBER', 999, 'https://github.test/private/comment', '2026-06-26T00:00:00Z'
            )
            """,
            ("a" * 40,),
        )
        self.connection.commit()

        rendered = dumps_verification_export(self.build_payload(run_id))

        self.assertNotIn("alice", rendered)
        self.assertNotIn("github.test", rendered)
        self.assertNotIn("source_comment_url", rendered)
        self.assertNotIn("actor_login", rendered)

    def test_building_export_has_no_database_side_effects(self) -> None:
        run_id = self.generate_review()
        before = self.connection.total_changes

        self.build_payload(run_id)

        self.assertEqual(before, self.connection.total_changes)

    def test_export_requires_a_generated_run_with_a_publication(self) -> None:
        run = memory_db.start_run(
            self.connection,
            "eneo/platform",
            17,
            base_sha="b" * 40,
            head_sha="a" * 40,
        )

        with self.assertRaisesRegex(ValueError, "must be generated"):
            build_verification_export(
                {
                    "source_schema_version": 1,
                    "run": dict(run),
                    "publication": {"delivery_status": "generated"},
                    "current_findings": [],
                },
                coverage=None,
            )

    def test_export_source_requires_a_publication(self) -> None:
        run = memory_db.start_run(
            self.connection,
            "eneo/platform",
            17,
            base_sha="b" * 40,
            head_sha="a" * 40,
        )
        run_id = int(run["id"])
        memory_db.complete_run(
            self.connection,
            run_id,
            repository="eneo/platform",
            pr_number=17,
            status="generated",
            findings_count=0,
        )

        with self.assertRaisesRegex(
            memory_db.ReviewMemoryError,
            "no recorded publication",
        ):
            memory_db.verification_export_source(
                self.connection,
                review_run_id=run_id,
            )

    def test_export_rejects_non_publishable_publication_status(self) -> None:
        run_id = self.generate_review()
        self.connection.execute(
            "UPDATE review_publications SET delivery_status = 'posting'"
        )
        self.connection.commit()

        with self.assertRaisesRegex(ValueError, "generated or posted"):
            self.build_payload(run_id)

    def test_export_source_requires_observation_evidence(self) -> None:
        run_id = self.generate_review()
        self.connection.execute(
            "UPDATE publication_findings SET observation_id = NULL WHERE status = 'current'"
        )
        self.connection.commit()

        with self.assertRaisesRegex(
            memory_db.ReviewMemoryError,
            "without observation evidence",
        ):
            memory_db.verification_export_source(
                self.connection,
                review_run_id=run_id,
            )

    def test_cli_writes_private_verification_export(self) -> None:
        run_id = self.generate_review()
        output = Path(self.temp.name) / "verification.json"

        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "eneo_review_memory.py"),
                "--db",
                self.db_path,
                "verification-export",
                "--run-id",
                str(run_id),
                "--output",
                str(output),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )

        self.assertIn(str(output), completed.stdout)
        self.assertEqual(stat.S_IMODE(os.stat(output).st_mode), 0o600)
        payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["review_run_id"], run_id)


if __name__ == "__main__":
    unittest.main()
