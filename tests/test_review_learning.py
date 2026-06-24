from __future__ import annotations

import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "bootstrap" / "plugins" / "eneo_review_tools"))

from eneo_review_learning import (
    DECISION_POLICIES,
    POSITIVE_DECISIONS,
    POSITIVE_FEEDBACK,
    QUALITY_POLICIES,
    build_learning_report,
    render_markdown,
)
from memory_validation import DECISIONS, REVIEW_FEEDBACK_CATEGORIES


def state_with(
    *,
    findings: list[dict[str, object]] | None = None,
    decisions: list[dict[str, object]] | None = None,
    feedback: list[dict[str, object]] | None = None,
    schema_version: int = 4,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "findings": findings or [],
        "decisions": decisions or [],
        "review_quality_feedback": feedback or [],
    }


def finding(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "fingerprint": "abcdef1234567890",
        "repository": "eneo-ai/eneo",
        "pr_number": 240,
        "rule_id": "migration.model-identity",
        "title": "All-tenant migration can choose the wrong model row",
        "path": "backend/src/intric/sysadmin/sysadmin_router.py",
        "severity": "High",
        "category": "security",
    }
    base.update(overrides)
    return base


class ReviewLearningReportTests(unittest.TestCase):
    def test_false_positive_decision_becomes_calibration_candidate(self) -> None:
        report = build_learning_report(
            state_with(
                findings=[finding()],
                decisions=[
                    {
                        "fingerprint": "abcdef1234567890",
                        "decision": "false_positive",
                        "reason": "Repository is already tenant-scoped before this query.",
                    }
                ],
            ),
            repository="eneo-ai/eneo",
        )

        self.assertEqual(len(report.decision_candidates), 1)
        candidate = report.decision_candidates[0]
        self.assertEqual(candidate.source_value, "false_positive")
        self.assertEqual(candidate.signal_strength, "strong")
        self.assertEqual(candidate.classification, "judgment_calibration")
        markdown = render_markdown(report)
        self.assertIn("## Decision candidates", markdown)
        self.assertIn("D1: All-tenant migration", markdown)
        self.assertIn("false_positive", markdown)

    def test_resolved_decision_is_positive_pattern_not_policy_candidate(self) -> None:
        report = build_learning_report(
            state_with(
                findings=[finding()],
                decisions=[
                    {
                        "fingerprint": "abcdef1234567890",
                        "decision": "resolved",
                        "reason": "Fixed with a regression test.",
                    }
                ],
            )
        )

        self.assertEqual(report.decision_candidates, ())
        self.assertEqual(len(report.positive_patterns), 1)
        self.assertEqual(report.positive_patterns[0].classification, "positive_pattern")

    def test_quality_feedback_is_separate_from_decision_candidates(self) -> None:
        report = build_learning_report(
            state_with(
                findings=[finding()],
                decisions=[
                    {
                        "fingerprint": "abcdef1234567890",
                        "decision": "false_positive",
                        "reason": "Existing guard disproves the claim.",
                    }
                ],
                feedback=[
                    {
                        "repository": "eneo-ai/eneo",
                        "pr_number": 240,
                        "local_reference": "F2",
                        "category": "missed_issue",
                        "reason": "The review missed a tenant-boundary regression.",
                    }
                ],
            )
        )

        self.assertEqual(len(report.decision_candidates), 1)
        self.assertEqual(len(report.quality_signals), 1)
        markdown = render_markdown(report)
        self.assertIn("### D1:", markdown)
        self.assertIn("### Q1:", markdown)
        self.assertLess(markdown.index("### D1:"), markdown.index("### Q1:"))

    def test_empty_export_does_not_fabricate_candidates(self) -> None:
        report = build_learning_report(state_with())

        self.assertEqual(report.decision_candidates, ())
        self.assertEqual(report.quality_signals, ())
        markdown = render_markdown(report)
        self.assertIn("No decision-derived learning candidates", markdown)
        self.assertIn("No review-quality signals", markdown)
        self.assertIn("Weak signals", markdown)

    def test_unknown_schema_version_fails_loudly(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported review-memory schema_version"):
            build_learning_report(state_with(schema_version=999))

    def test_learning_vocabularies_match_canonical_memory_values(self) -> None:
        handled_decisions = set(DECISION_POLICIES) | set(POSITIVE_DECISIONS)
        handled_feedback = set(QUALITY_POLICIES) | set(POSITIVE_FEEDBACK)

        self.assertEqual(handled_decisions, set(DECISIONS))
        self.assertEqual(handled_feedback, set(REVIEW_FEEDBACK_CATEGORIES))

    def test_unclassified_values_are_reported_not_silently_dropped(self) -> None:
        report = build_learning_report(
            state_with(
                findings=[finding()],
                decisions=[
                    {
                        "fingerprint": "abcdef1234567890",
                        "decision": "worsened",
                        "reason": "",
                    }
                ],
                feedback=[
                    {
                        "repository": "eneo-ai/eneo",
                        "pr_number": 240,
                        "category": "too_many_widgets",
                        "reason": "",
                    }
                ],
            )
        )
        markdown = render_markdown(report)

        self.assertIn("Unclassified decision values", markdown)
        self.assertIn("`worsened`", markdown)
        self.assertIn("Unclassified review-quality feedback values", markdown)
        self.assertIn("`too_many_widgets`", markdown)

    def test_empty_decision_reason_does_not_abort_report(self) -> None:
        report = build_learning_report(
            state_with(
                findings=[finding()],
                decisions=[
                    {
                        "fingerprint": "abcdef1234567890",
                        "decision": "false_positive",
                        "reason": "",
                    }
                ],
            )
        )

        self.assertEqual(len(report.decision_candidates), 1)


if __name__ == "__main__":
    unittest.main()
