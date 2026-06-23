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
        canonical = read("bootstrap/workspace/AGENTS.md")
        metadata = "`backend/src/intric/jobs/service.py:142` · security · **High / P1 important**"
        self.assertIn("`path:line` · category · **Severity**", canonical)
        self.assertNotIn("<emoji>", canonical)
        self.assertIn(metadata, read("examples/comments/example-review.md"))
        self.assertIn(metadata, read("GUIDE.md"))
        self.assertNotIn("High confidence", read("examples/comments/example-review.md"))
        self.assertNotIn("High confidence", read("GUIDE.md"))

    def test_repeated_reviews_reexamine_prior_findings(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        skill = read("bootstrap/skills/eneo-pr-review/SKILL.md")
        self.assertIn("Re-examine", skill)
        self.assertIn("unsuppressed `recent_findings`", skill)
        self.assertIn("Repeated reviews should not vary findings for novelty", canonical)
        self.assertIn("higher-severity new findings still take priority", canonical)
        self.assertIn("reuse its exact `rule_id`, `symbol`, and `anchor`", skill)

    def test_skeptical_gate_pins_falsification_and_quality_rules(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        skill = read("bootstrap/skills/eneo-pr-review/SKILL.md")
        self.assertIn("cheapest falsifier", canonical)
        self.assertIn("cheapest falsifier", skill)
        self.assertIn("would have passed before this change", canonical)
        self.assertIn("asserts mocks or implementation details", canonical)
        self.assertIn("safe local fix", skill)
        self.assertIn("why it exists", skill)
        self.assertIn("reason no longer applies", skill)

    def test_runtime_contract_forbids_merge_gate_language(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        self.assertIn(
            "never call the PR `safe to merge`, `approved`, or `GREEN_LIGHT`",
            canonical,
        )

    def test_lower_priority_findings_have_anti_noise_rule(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        self.assertIn("Medium / P2 useful improvement", canonical)
        self.assertIn("Low / P3 minor but actionable", canonical)
        self.assertIn("Critical or High finding survives", canonical)
        self.assertIn("publish at most one Medium or Low", canonical)

    def test_plugin_manifest_lists_registered_tools(self):
        manifest = read("bootstrap/plugins/eneo_review_tools/plugin.yaml")
        registered = set(
            re.findall(r'name="(eneo_[a-z0-9_]+)"', read("bootstrap/plugins/eneo_review_tools/__init__.py"))
        )
        provided = set(re.findall(r"^\s+- (eneo_[a-z0-9_]+)$", manifest, re.MULTILINE))
        self.assertEqual(provided, registered)


if __name__ == "__main__":
    unittest.main()
