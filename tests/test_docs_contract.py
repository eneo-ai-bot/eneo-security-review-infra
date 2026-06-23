from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class DocsContractTests(unittest.TestCase):
    def test_visible_word_budget_has_one_owner(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        self.assertIn("Maximum about 450 visible prose words", canonical)

        duplicate_budget = re.compile(r"\b\d+\s+visible\s+\w*\s*words\b")
        for relative in [
            "README.md",
            "GUIDE.md",
            "bootstrap/skills/eneo-pr-review/SKILL.md",
        ]:
            with self.subTest(relative=relative):
                self.assertIsNone(duplicate_budget.search(read(relative)))

    def test_visible_examples_use_category_and_severity(self):
        metadata = "`backend/src/intric/jobs/service.py:142` · security · **High / P1 important**"
        self.assertIn(metadata, read("examples/comments/example-review.md"))
        self.assertIn(metadata, read("GUIDE.md"))
        self.assertNotIn("High confidence", read("examples/comments/example-review.md"))
        self.assertNotIn("High confidence", read("GUIDE.md"))

    def test_lower_priority_findings_have_anti_noise_rule(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        self.assertIn("Medium / P2 useful improvement", canonical)
        self.assertIn("Low / P3 minor but actionable", canonical)
        self.assertIn("Critical or High finding survives", canonical)
        self.assertIn("publish at most one Medium or Low", canonical)


if __name__ == "__main__":
    unittest.main()
