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
                unavailable=1,
                unavailable_paths=["frontend/app/routes/page.svelte"],
            )
        )

        self.assertIn("Review context incomplete", line)
        self.assertIn("not a clean result", line)
        self.assertNotIn("supporting context skipped", line.lower())

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
