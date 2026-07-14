from __future__ import annotations

import hashlib
from datetime import timedelta
import sys
import tempfile
import unittest
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from eneo_review_tools import memory_db, memory_suggestions  # noqa: E402


class SuggestionValidationTests(unittest.TestCase):
    repository = "eneo/platform"
    pr_number = 17
    head_sha = "a" * 40
    fingerprint = "f" * 64
    path = "src/flags.py"
    head_text = "before\nsafe = False\nafter\n"
    patch = (
        "@@ -1,3 +1,3 @@\n"
        " before\n"
        "-safe = None\n"
        "+safe = False\n"
        " after"
    )

    def raw(self, **overrides: object) -> dict[str, object]:
        value: dict[str, object] = {
            "start_line": 2,
            "end_line": 2,
            "expected_text": "safe = False",
            "replacement_text": "safe = True",
        }
        value.update(overrides)
        return value

    def validate(self, **overrides: object) -> memory_suggestions.SuggestionValidation:
        return memory_suggestions.validate_suggestion(
            self.raw(**overrides),
            repository=self.repository,
            pr_number=self.pr_number,
            head_sha=self.head_sha,
            fingerprint=self.fingerprint,
            path=self.path,
            finding_line=2,
            patch=self.patch,
            head_text=self.head_text,
        )

    def test_accepts_exact_atomic_changed_range(self) -> None:
        result = self.validate()

        self.assertEqual(result.rejection_reason, "")
        assert result.suggestion is not None
        self.assertEqual(result.suggestion["start_line"], 2)
        self.assertEqual(result.suggestion["end_line"], 2)
        self.assertEqual(result.suggestion["replacement_text"], "safe = True")
        self.assertEqual(
            result.suggestion["expected_hash"],
            hashlib.sha256(b"safe = False").hexdigest(),
        )

    def test_rejects_ambiguous_terminal_newline(self) -> None:
        result = self.validate(
            expected_text="safe = False\n", replacement_text="safe = True\n"
        )

        self.assertIsNone(result.suggestion)
        self.assertEqual(result.rejection_reason, "suggestion_text_invalid")

    def test_rejects_expected_text_that_does_not_match_trusted_head(self) -> None:
        result = self.validate(expected_text="safe = maybe")

        self.assertIsNone(result.suggestion)
        self.assertEqual(result.rejection_reason, "suggestion_expected_text_mismatch")

    def test_rejects_context_only_range(self) -> None:
        result = memory_suggestions.validate_suggestion(
            self.raw(
                start_line=1,
                end_line=1,
                expected_text="before",
                replacement_text="before = True",
            ),
            repository=self.repository,
            pr_number=self.pr_number,
            head_sha=self.head_sha,
            fingerprint=self.fingerprint,
            path=self.path,
            finding_line=1,
            patch=self.patch,
            head_text=self.head_text,
        )

        self.assertIsNone(result.suggestion)
        self.assertEqual(result.rejection_reason, "suggestion_range_not_in_changed_hunk")

    def test_rejects_range_that_does_not_include_finding_line(self) -> None:
        result = self.validate(start_line=1, end_line=1)

        self.assertIsNone(result.suggestion)
        self.assertEqual(result.rejection_reason, "suggestion_must_include_finding_line")

    def test_rejects_noop_placeholder_and_markdown_fence(self) -> None:
        cases = (
            (self.raw(replacement_text="safe = False"), "suggestion_has_no_change"),
            (self.raw(replacement_text="TODO"), "suggestion_contains_placeholder"),
            (self.raw(replacement_text="```python"), "suggestion_text_invalid"),
            (
                self.raw(
                    replacement_text=(
                        "<!-- eneo-review:suggestion key=sha256:"
                        + ("0" * 64)
                        + " -->"
                    )
                ),
                "suggestion_text_invalid",
            ),
        )
        for raw, reason in cases:
            with self.subTest(reason=reason):
                result = memory_suggestions.validate_suggestion(
                    raw,
                    repository=self.repository,
                    pr_number=self.pr_number,
                    head_sha=self.head_sha,
                    fingerprint=self.fingerprint,
                    path=self.path,
                    finding_line=2,
                    patch=self.patch,
                    head_text=self.head_text,
                )
                self.assertIsNone(result.suggestion)
                self.assertEqual(result.rejection_reason, reason)

    def test_key_is_stable_for_one_finding_and_head(self) -> None:
        first = memory_suggestions.suggestion_key(
            self.repository, self.pr_number, self.head_sha, self.fingerprint
        )
        second = memory_suggestions.suggestion_key(
            self.repository.upper(), self.pr_number, self.head_sha, self.fingerprint
        )

        self.assertEqual(first, second)
        self.assertRegex(first, r"^sha256:[0-9a-f]{64}$")

    def test_terminal_marker_extraction_ignores_embedded_marker_text(self) -> None:
        expected = memory_suggestions.suggestion_key(
            self.repository, self.pr_number, self.head_sha, self.fingerprint
        )
        fake = "sha256:" + ("0" * 64)
        body = (
            f"```suggestion\n<!-- eneo-review:suggestion key={fake} -->\n```\n"
            f"{memory_suggestions.suggestion_marker(expected)}"
        )

        self.assertEqual(memory_suggestions.extract_suggestion_key(body), expected)
        self.assertIsNone(
            memory_suggestions.extract_suggestion_key(
                f"prefix {memory_suggestions.suggestion_marker(expected)} suffix"
            )
        )

    def test_high_risk_finding_metadata_is_ineligible(self) -> None:
        self.assertEqual(
            memory_suggestions.suggestion_eligibility_rejection(
                rule_id="tenant.missing-scope",
                category="correctness",
                path="src/service.py",
                symbol="load_document",
                anchor="worker lookup",
                title="Tenant boundary is bypassed",
                evidence="The worker performs a global lookup.",
                impact="Cross-account access is possible.",
                smallest_fix="Scope the lookup.",
            ),
            "suggestion_high_risk_domain",
        )
        self.assertEqual(
            memory_suggestions.suggestion_eligibility_rejection(
                rule_id="correctness.default",
                category="security",
                path="src/flags.py",
                symbol="safe",
                anchor="safe default",
                title="Safe mode defaults to disabled",
                evidence="The changed default is false.",
                impact="Requests use the wrong mode.",
                smallest_fix="Restore the default.",
            ),
            "suggestion_high_risk_category",
        )
        for category in ("migration", "contracts", "data_contract"):
            with self.subTest(category=category):
                self.assertEqual(
                    memory_suggestions.suggestion_eligibility_rejection(
                        rule_id="correctness.local-change",
                        category=category,
                        path="src/change.py",
                        symbol="apply_change",
                        anchor="local change",
                        title="Local value is incorrect",
                        evidence="The value differs from the configured default.",
                        impact="The result is incorrect.",
                        smallest_fix="Use the configured default.",
                    ),
                    "suggestion_high_risk_category",
                )


class SuggestionPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = memory_db.connect(
            str(Path(self.temp.name) / "review-memory.sqlite3")
        )
        self.repository = "eneo/platform"
        self.pr_number = 17
        self.base_sha = "b" * 40
        self.head_sha = "a" * 40
        self.finding = {
            "rule_id": "correctness.boolean-default",
            "category": "correctness",
            "path": "src/flags.py",
            "line": 2,
            "symbol": "safe",
            "anchor": "safe default",
            "title": "Safe mode defaults to disabled",
            "severity": "Medium",
            "publication_score": 8,
            "confidence": 0.95,
            "evidence": "The changed default is false.",
            "disproof_checks": "Checked all callers.",
            "impact": "Requests can run without the expected guard.",
            "smallest_fix": "Restore the true default and cover it with the existing test.",
            "introduced_by_diff": True,
        }
        self.run = memory_db.start_run(
            self.connection,
            self.repository,
            self.pr_number,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
        )
        self.recorded = memory_db.record_findings(
            self.connection,
            self.repository,
            self.pr_number,
            self.head_sha,
            [self.finding],
            review_run_id=int(self.run["id"]),
            base_sha=self.base_sha,
            context_hashes={self.finding["path"]: "c" * 40},
        )[0]
        validation = memory_suggestions.validate_suggestion(
            {
                "start_line": 2,
                "end_line": 2,
                "expected_text": "safe = False",
                "replacement_text": "safe = True",
            },
            repository=self.repository,
            pr_number=self.pr_number,
            head_sha=self.head_sha,
            fingerprint=str(self.recorded["fingerprint"]),
            path=self.finding["path"],
            finding_line=2,
            patch=(
                "@@ -1,3 +1,3 @@\n before\n-safe = None\n+safe = False\n after"
            ),
            head_text="before\nsafe = False\nafter\n",
        )
        assert validation.suggestion is not None
        self.suggestion = validation.suggestion

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_publication_links_suggestion_to_current_observation(self) -> None:
        with self.connection:
            memory_suggestions.replace_observation_suggestion(
                self.connection,
                observation_id=int(self.recorded["observation_id"]),
                suggestion=self.suggestion,
            )
        publication = memory_db.finalize_review(
            self.connection,
            self.repository,
            self.pr_number,
            self.head_sha,
            review_run_id=int(self.run["id"]),
        )

        self.assertEqual(publication["suggestions_count"], 1)
        self.assertEqual(publication["suggestion_delivery_status"], "pending")
        self.assertIn("optional GitHub suggestion ready to apply", publication["markdown"])
        stored = memory_suggestions.suggestions_for_publication(
            self.connection, int(publication["publication_id"])
        )
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["local_reference"], "F1")
        row = self.connection.execute(
            "SELECT * FROM review_suggestions WHERE observation_id = ?",
            (int(self.recorded["observation_id"]),),
        ).fetchone()
        self.assertNotIn("expected_text", row.keys())
        self.assertEqual(row["expected_hash"], self.suggestion["expected_hash"])
        exported = memory_db.export_state(self.connection)
        self.assertEqual(len(exported["review_suggestions"]), 1)

    def test_clearing_candidate_keeps_finding_and_removes_suggestion(self) -> None:
        with self.connection:
            memory_suggestions.replace_observation_suggestion(
                self.connection,
                observation_id=int(self.recorded["observation_id"]),
                suggestion=self.suggestion,
            )
            memory_suggestions.replace_observation_suggestion(
                self.connection,
                observation_id=int(self.recorded["observation_id"]),
                suggestion=None,
            )

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM review_suggestions").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM finding_observations").fetchone()[0],
            1,
        )

    def test_delivery_status_is_independent_from_summary_publication(self) -> None:
        with self.connection:
            memory_suggestions.replace_observation_suggestion(
                self.connection,
                observation_id=int(self.recorded["observation_id"]),
                suggestion=self.suggestion,
            )
        publication = memory_db.finalize_review(
            self.connection,
            self.repository,
            self.pr_number,
            self.head_sha,
            review_run_id=int(self.run["id"]),
        )
        publication_id = int(publication["publication_id"])

        memory_suggestions.mark_suggestions_failed(
            self.connection,
            publication_id=publication_id,
            failure_code="github_403_create_pull_request_review",
        )
        failed = memory_suggestions.suggestion_delivery_status(
            self.connection, publication_id
        )
        self.assertEqual(failed["suggestion_delivery_status"], "publish_failed")

        claim = memory_suggestions.claim_suggestions_for_posting(
            self.connection, publication_id
        )
        claim_started_at = claim["suggestion_posting_started_at"]
        assert claim_started_at is not None
        memory_suggestions.mark_suggestions_posted(
            self.connection,
            publication_id=publication_id,
            review_id=901,
            claim_started_at=claim_started_at,
        )
        posted = memory_suggestions.suggestion_delivery_status(
            self.connection, publication_id
        )
        self.assertEqual(posted["suggestion_delivery_status"], "posted")
        self.assertEqual(posted["suggestion_review_id"], 901)
        listed = memory_db.list_publications(
            self.connection,
            repository=self.repository,
            pr_number=self.pr_number,
        )[0]
        self.assertEqual(listed["suggestion_delivery_status"], "posted")
        self.assertEqual(listed["suggestion_review_id"], 901)
        self.assertIsNone(listed["suggestion_posting_started_at"])
        self.assertTrue(listed["suggestion_posted_at"])
        self.assertEqual(listed["suggestion_failure_code"], "")

    def test_same_head_rerun_reuses_first_validated_patch(self) -> None:
        with self.connection:
            memory_suggestions.replace_observation_suggestion(
                self.connection,
                observation_id=int(self.recorded["observation_id"]),
                suggestion=self.suggestion,
            )
        memory_db.complete_run(
            self.connection,
            int(self.run["id"]),
            repository=self.repository,
            pr_number=self.pr_number,
            status="generated",
            findings_count=1,
        )
        second_run = memory_db.start_run(
            self.connection,
            self.repository,
            self.pr_number,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
        )
        second = memory_db.record_findings(
            self.connection,
            self.repository,
            self.pr_number,
            self.head_sha,
            [self.finding],
            review_run_id=int(second_run["id"]),
            base_sha=self.base_sha,
            context_hashes={self.finding["path"]: "c" * 40},
        )[0]
        replacement = dict(self.suggestion)
        replacement["start_line"] = 1
        replacement["end_line"] = 2
        replacement["replacement_text"] = "before\nsafe = check_default()"
        with self.connection:
            memory_suggestions.replace_observation_suggestion(
                self.connection,
                observation_id=int(second["observation_id"]),
                suggestion=replacement,
            )

        stored = self.connection.execute(
            """
            SELECT start_line, end_line, replacement_text
            FROM review_suggestions
            WHERE observation_id = ?
            """,
            (int(second["observation_id"]),),
        ).fetchone()
        self.assertEqual(stored["start_line"], self.suggestion["start_line"])
        self.assertEqual(stored["end_line"], self.suggestion["end_line"])
        self.assertEqual(
            stored["replacement_text"], self.suggestion["replacement_text"]
        )

    def test_expired_claim_is_fenced_after_another_publisher_reclaims_it(self) -> None:
        with self.connection:
            memory_suggestions.replace_observation_suggestion(
                self.connection,
                observation_id=int(self.recorded["observation_id"]),
                suggestion=self.suggestion,
            )
        publication = memory_db.finalize_review(
            self.connection,
            self.repository,
            self.pr_number,
            self.head_sha,
            review_run_id=int(self.run["id"]),
        )
        publication_id = int(publication["publication_id"])

        claimed_at = memory_db.utc_now()
        first = memory_suggestions.claim_suggestions_for_posting(
            self.connection, publication_id, now=claimed_at
        )
        first_token = first["suggestion_posting_started_at"]
        assert first_token is not None
        second = memory_suggestions.claim_suggestions_for_posting(
            self.connection, publication_id, now=claimed_at + timedelta(minutes=5)
        )
        recovered = memory_suggestions.claim_suggestions_for_posting(
            self.connection, publication_id, now=claimed_at + timedelta(minutes=31)
        )
        recovered_token = recovered["suggestion_posting_started_at"]
        assert recovered_token is not None

        self.assertTrue(first["claimed"])
        self.assertEqual(first["suggestion_delivery_status"], "posting")
        self.assertFalse(second["claimed"])
        self.assertEqual(second["suggestion_delivery_status"], "posting")
        self.assertTrue(recovered["claimed"])
        self.assertEqual(recovered["suggestion_delivery_status"], "posting")
        self.assertNotEqual(recovered_token, first_token)

        with self.assertRaisesRegex(
            memory_db.ReviewMemoryError, "suggestion delivery claim was lost"
        ):
            memory_suggestions.mark_suggestions_posted(
                self.connection,
                publication_id=publication_id,
                review_id=901,
                claim_started_at=first_token,
            )
        with self.assertRaisesRegex(
            memory_db.ReviewMemoryError, "suggestion delivery state could not be changed"
        ):
            memory_suggestions.mark_suggestions_failed(
                self.connection,
                publication_id=publication_id,
                failure_code="late_worker_failed",
                claim_started_at=first_token,
            )

        still_owned = memory_suggestions.suggestion_delivery_status(
            self.connection, publication_id
        )
        self.assertEqual(still_owned["suggestion_delivery_status"], "posting")
        self.assertEqual(
            still_owned["suggestion_posting_started_at"], recovered_token
        )

        memory_suggestions.mark_suggestions_posted(
            self.connection,
            publication_id=publication_id,
            review_id=902,
            claim_started_at=recovered_token,
        )
        posted = memory_suggestions.suggestion_delivery_status(
            self.connection, publication_id
        )
        self.assertEqual(posted["suggestion_delivery_status"], "posted")
        self.assertEqual(posted["suggestion_review_id"], 902)
        self.assertIsNone(posted["suggestion_posting_started_at"])


if __name__ == "__main__":
    unittest.main()
