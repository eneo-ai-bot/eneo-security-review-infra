from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FeedbackWorkflowTests(unittest.TestCase):
    def test_workflow_routes_exact_review_and_prefixed_feedback_commands(self) -> None:
        # This pins the checked-in GitHub Actions snippet; behavior lives in the
        # deterministic feedback bridge tests.
        workflow = (ROOT / "examples/github/ai-review-request.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn('trigger in {"@review", "/review"}', workflow)
        self.assertIn('mode = "review"', workflow)
        self.assertIn('re.match(r"^[@/]review\\s+", trigger, flags=re.IGNORECASE)', workflow)
        self.assertIn('mode = "feedback"', workflow)
        self.assertIn("HERMES_REVIEW_FEEDBACK_URL", workflow)
        self.assertIn("HERMES_REVIEW_FEEDBACK_SECRET", workflow)
        self.assertIn('delivery_id = f"{event[\'comment\'][\'id\']}:feedback"', workflow)
        self.assertIn('event["comment"]["user"].get("type") == "Bot"', workflow)


if __name__ == "__main__":
    unittest.main()
