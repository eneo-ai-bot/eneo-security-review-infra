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
        self.assertIn("Keep each finding compact", canonical)

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
        metadata = "`backend/src/intric/jobs/service.py:142` · security"
        heading = "### F1 - High / P1: Tenant context is dropped before the background job"
        self.assertIn("`path:line` · category", canonical)
        self.assertIn("`### F1 - High / P1: Title`", canonical)
        self.assertNotIn("<emoji>", canonical)
        self.assertIn(heading, read("examples/comments/example-review.md"))
        self.assertIn(heading, read("GUIDE.md"))
        self.assertIn(metadata, read("examples/comments/example-review.md"))
        self.assertIn(metadata, read("GUIDE.md"))
        self.assertNotIn("· **High / P1 important**", read("examples/comments/example-review.md"))
        self.assertNotIn("· **High / P1 important**", read("GUIDE.md"))
        self.assertNotIn("High confidence", read("examples/comments/example-review.md"))
        self.assertNotIn("High confidence", read("GUIDE.md"))

    def test_examples_show_all_findings_review_shape(self):
        for relative in ["examples/comments/example-review.md", "GUIDE.md"]:
            body = read(relative)
            with self.subTest(relative=relative):
                self.assertIn(
                    "I found 2 findings: 1 High / P1 and 1 Medium / P2.",
                    body,
                )
                self.assertNotIn("| Severity | Category | Location | Finding | ID |", body)
                self.assertIn(
                    "### F2 - Medium / P2: Regression test misses", body
                )
                self.assertNotIn("<summary>Medium / P2", body)
                self.assertIn("Copyable fix brief for a coding agent", body)
                self.assertIn("```text\nTask:", body)
                self.assertIn("Findings:", body)
                self.assertIn("F1 - High / P1", body)
                self.assertIn("F2 - Medium / P2", body)
                self.assertIn("Re-check every finding against the current PR head", body)

    def test_repeated_reviews_reexamine_prior_findings(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        skill = read("bootstrap/skills/eneo-pr-review/SKILL.md")
        self.assertIn("re-check each prior unresolved finding", skill)
        self.assertIn("`repeat_review_findings`", skill)
        self.assertIn("same-path history", skill)
        self.assertIn("Repeated reviews should not vary findings for novelty", canonical)
        self.assertIn("Treat the previous", canonical)
        self.assertIn("unresolved findings as review candidates", canonical)
        self.assertIn("resolution pass", skill)
        self.assertIn("compact safety sweep", skill)
        self.assertIn("may come", canonical)
        self.assertIn("from other pull requests", canonical)
        self.assertIn("reuse its exact `rule_id`, `symbol`, and `anchor`", skill)

    def test_skeptical_gate_pins_falsification_and_quality_rules(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        skill = read("bootstrap/skills/eneo-pr-review/SKILL.md")
        self.assertIn("cheapest falsifier", canonical)
        self.assertIn("challenge each candidate under AGENTS.md", skill)
        self.assertIn("would have passed before this change", canonical)
        self.assertIn("asserts mocks or implementation details", canonical)
        self.assertIn("safe local", skill)
        self.assertIn("fix; call out careful or risky remediation", skill)
        self.assertIn("why it exists", skill)
        self.assertIn("reason no longer applies", skill)

    def test_runtime_contract_forbids_merge_gate_language(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        self.assertIn(
            "never call the PR `safe to merge`, `approved`, or `GREEN_LIGHT`",
            canonical,
        )
        self.assertIn("Do not call findings `blocking` or `merge-blocking`", canonical)

    def test_comment_summary_replaces_metadata_table(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        self.assertIn("names the non-zero severity counts", canonical)
        self.assertIn("Do not include a top-level per-finding table", canonical)
        self.assertIn("Long paths and memory", canonical)
        self.assertNotIn("summary table listing every finding", canonical)

    def test_all_surviving_findings_are_publishable(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        self.assertIn("**Medium / P2**", canonical)
        self.assertIn("**Low / P3**", canonical)
        self.assertIn("Publish every unsuppressed, evidence-backed, independent root-cause finding", canonical)
        self.assertIn("Do not omit a verified lower-priority", canonical)
        self.assertIn("Render every published finding as a normal expanded `###` section", canonical)
        self.assertIn("Lower severity controls priority and ordering", canonical)
        self.assertIn("not\n  visibility", canonical)
        self.assertIn("This is the only collapsed section for active findings", canonical)
        self.assertIn("one complete brief in a single `text` fenced code block", canonical)
        self.assertIn("include every published finding", canonical)

    def test_machine_metadata_is_hidden_from_reading_path(self):
        canonical = read("bootstrap/workspace/AGENTS.md")
        tools = read("bootstrap/plugins/eneo_review_tools/tools.py")
        for body in [
            canonical,
            read("examples/comments/example-review.md"),
            read("GUIDE.md"),
        ]:
            with self.subTest(body=body[:30]):
                self.assertNotIn("quiet footer", body)
                self.assertNotIn("<sub>Eneo two-pass review", body)
        self.assertIn("Keep machine identifiers out of the developer reading path", canonical)
        self.assertIn("hidden metadata", canonical)
        self.assertIn("only in hidden review metadata", tools)

    def test_feedback_and_learning_are_human_governed(self):
        guide = read("GUIDE.md")
        readme = read("README.md")
        for body in [guide, readme]:
            with self.subTest(body=body[:30]):
                self.assertIn("@review false-positive F2 <reason>", body)
                self.assertIn("@review feedback", body)
                self.assertIn("missed", body)
        self.assertIn("ADRs are context, not immunity", guide)
        self.assertIn("automatically rewrite reviewer policy", guide)

    def test_plugin_manifest_lists_registered_tools(self):
        manifest = read("bootstrap/plugins/eneo_review_tools/plugin.yaml")
        registered = set(
            re.findall(r'name="(eneo_[a-z0-9_]+)"', read("bootstrap/plugins/eneo_review_tools/__init__.py"))
        )
        provided = set(re.findall(r"^\s+- (eneo_[a-z0-9_]+)$", manifest, re.MULTILINE))
        self.assertEqual(provided, registered)


if __name__ == "__main__":
    unittest.main()
