from __future__ import annotations

import sys
import unittest
from pathlib import Path

PLUGIN = (
    Path(__file__).resolve().parents[1]
    / "bootstrap"
    / "plugins"
    / "eneo_review_tools"
)
sys.path.insert(0, str(PLUGIN))

import review_renderer  # noqa: E402
import memory_validation  # noqa: E402


class ReviewRendererTests(unittest.TestCase):
    def coverage(self, **overrides):
        value = {
            "state": "complete",
            "changed_paths": 3,
            "diff_exposed": 3,
            "context_paths_read": 1,
            "context_ranges_read": 1,
            "changed_paths_with_diff": 3,
            "changed_paths_with_source_reads": 1,
            "supporting_context_paths_read": 2,
            "changed_files_reported": 3,
            "changed_files_registered": 3,
            "changed_file_registration_complete": True,
            "unavailable": 0,
            "diff_truncated": 0,
            "coverage_hash": "sha256:abc",
            "unavailable_paths": [],
            "truncated_paths": [],
        }
        value.update(overrides)
        return value

    def finding(self, **overrides):
        value = {
            "local_reference": "F1",
            "fingerprint": "a" * 64,
            "observation_id": 1,
            "context_hash": "b" * 64,
            "review_status": "observed",
            "rule_id": "tests.example",
            "category": "tests",
            "path": "backend/app/example.py",
            "line": 12,
            "title": "Example finding",
            "severity": "Medium",
            "publication_score": 8,
            "evidence": "Evidence text.",
            "disproof_checks": "Reviewer checks text.",
            "impact": "Impact text.",
            "smallest_fix": "Suggested fix.",
        }
        value.update(overrides)
        return value

    def test_compact_text_keeps_short_text_unchanged_and_collapsed(self) -> None:
        self.assertEqual(
            memory_validation.compact_text("  one\n two\tthree  ", maximum=40),
            "one two three",
        )

    def test_compact_text_prefers_word_boundary_without_exceeding_limit(self) -> None:
        text = "one two three four five six seven"

        rendered = memory_validation.compact_text(text, maximum=24)

        self.assertEqual("one two three four...", rendered)
        self.assertLessEqual(len(rendered), 24)

    def test_compact_text_hard_cuts_single_long_token(self) -> None:
        rendered = memory_validation.compact_text("x" * 40, maximum=12)

        self.assertEqual("x" * 9 + "...", rendered)
        self.assertLessEqual(len(rendered), 12)

    def test_compact_text_ignores_too_early_boundary(self) -> None:
        rendered = memory_validation.compact_text("one " + ("x" * 40), maximum=16)

        self.assertEqual("one " + ("x" * 9) + "...", rendered)
        self.assertLessEqual(len(rendered), 16)

    def test_complete_coverage_line_reports_diff_coverage_not_deep_review(self) -> None:
        line = review_renderer.coverage_summary_line(
            self.coverage(
                changed_paths=1,
                diff_exposed=1,
                changed_paths_with_diff=1,
                changed_paths_with_source_reads=1,
                supporting_context_paths_read=1,
                changed_files_reported=1,
                changed_files_registered=1,
            )
        )

        self.assertIn(
            "textual diff content was available for all 1 registered changed path",
            line,
        )
        self.assertIn(
            "Additional source context was read from 1 changed path "
            "and 1 supporting file",
            line,
        )
        self.assertNotIn("This review is not a clean result", line)

    def test_complete_coverage_line_omits_empty_source_context_sentence(self) -> None:
        line = review_renderer.coverage_summary_line(
            self.coverage(
                changed_paths_with_source_reads=0,
                supporting_context_paths_read=0,
            )
        )

        self.assertIn(
            "textual diff content was available for all 3 registered changed paths",
            line,
        )
        self.assertNotIn("Additional source context", line)
        self.assertNotIn("0 changed paths", line)
        self.assertNotIn("0 supporting files", line)

    def test_complete_coverage_line_omits_zero_source_context_counts(self) -> None:
        line = review_renderer.coverage_summary_line(
            self.coverage(
                changed_paths_with_source_reads=1,
                supporting_context_paths_read=0,
            )
        )

        self.assertIn(
            "Additional source context was read from 1 changed path.",
            line,
        )
        self.assertNotIn("0 supporting files", line)

    def test_incomplete_coverage_line_does_not_create_supporting_context_noise(self) -> None:
        line = review_renderer.coverage_summary_line(
            self.coverage(
                state="incomplete",
                changed_paths_with_diff=2,
                changed_paths_with_source_reads=1,
                unavailable=1,
                unavailable_paths=["frontend/app/routes/page.svelte"],
            )
        )

        self.assertIn("Review context incomplete", line)
        self.assertIn(
            "textual diff content was inspected for 2 of 3 registered changed paths",
            line,
        )
        self.assertIn("Additional source context was read from 1 changed path", line)
        self.assertIn("not a clean result", line)
        self.assertNotIn("large PR", line)
        self.assertNotIn("risk-ranked", line)
        self.assertNotIn("supporting context skipped", line.lower())

    def test_visible_reviewer_checks_truncate_at_word_boundary(self) -> None:
        long_checks = (
            "Checked the base config and frontend client before accepting the "
            "finding. "
            + "extra detail " * 80
        )

        rendered = review_renderer.render_review_markdown(
            repository="eneo/platform",
            pr_number=17,
            head_sha="a" * 40,
            findings=[self.finding(disproof_checks=long_checks)],
            closed=[],
            still_present=[],
            partially_resolved=[],
            new_refs=["F1"],
            needs_recheck=[],
            review_number=1,
        )

        self.assertIn(
            "**Reviewer checks:** Checked the base config and frontend client",
            rendered,
        )
        self.assertIn("...", rendered)
        self.assertNotIn("extra deta...", rendered)

    def test_fix_brief_reviewer_checks_truncate_at_word_boundary(self) -> None:
        long_checks = (
            "Checked the base config and frontend client before accepting the "
            "finding. "
            + "extra detail " * 80
        )

        rendered = review_renderer.render_fix_brief(
            "eneo/platform",
            17,
            "a" * 40,
            [self.finding(disproof_checks=long_checks)],
        )

        self.assertIn(
            "Reviewer checks: Checked the base config and frontend client",
            rendered,
        )
        self.assertIn("...", rendered)
        self.assertNotIn("extra deta...", rendered)

    def test_review_number_heading_does_not_autolink_github_issue(self) -> None:
        rendered = review_renderer.render_review_markdown(
            repository="eneo/platform",
            pr_number=17,
            head_sha="a" * 40,
            findings=[],
            closed=[],
            still_present=[],
            partially_resolved=[],
            new_refs=[],
            needs_recheck=[],
            review_number=5,
        )

        self.assertIn("Review 5", rendered)
        self.assertNotIn("Review #5", rendered)

    def test_lifecycle_summary_review_number_does_not_autolink_github_issue(self) -> None:
        summary = review_renderer.lifecycle_summary(
            findings=[],
            closed=[],
            still_present=[],
            partially_resolved=[],
            new_refs=["F2"],
            needs_recheck=[],
            previous_review_number=5,
        )

        self.assertIn("Compared with Review 5", summary)
        self.assertNotIn("Review #5", summary)


if __name__ == "__main__":
    unittest.main()
