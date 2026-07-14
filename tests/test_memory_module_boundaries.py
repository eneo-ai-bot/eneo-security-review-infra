from __future__ import annotations

import ast
import importlib
import sys
import unittest
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
PLUGIN = PACKAGE_ROOT / "eneo_review_tools"
sys.path.insert(0, str(PACKAGE_ROOT))


OWNER_MODULES = [
    "feedback_authorization",
    "feedback_commands",
    "memory_validation",
    "memory_schema",
    "memory_migration",
    "memory_identity",
    "memory_decisions",
    "memory_findings",
    "memory_suggestions",
    "memory_verification",
    "memory_publications",
    "memory_feedback",
    "memory_reporting",
    "memory_runs",
    "memory_coach",
]
OFFLINE_OPERATOR_MODULES = {
    "eneo_review_learning",
    "eneo_review_coach",
    "eneo_review_coach_proposals",
    "eneo_review_replay",
    "eneo_review_export",
}


class MemoryModuleBoundaryTests(unittest.TestCase):
    def test_owner_modules_import_without_facade_cycles(self):
        for module in OWNER_MODULES:
            with self.subTest(module=module):
                importlib.import_module(f"eneo_review_tools.{module}")

    def test_owner_modules_do_not_import_memory_db_facade(self):
        for module in OWNER_MODULES:
            with self.subTest(module=module):
                source = (PLUGIN / f"{module}.py").read_text(encoding="utf-8")
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported = {alias.name for alias in node.names}
                        self.assertNotIn("memory_db", imported)
                        self.assertNotIn("eneo_review_tools.memory_db", imported)
                    elif isinstance(node, ast.ImportFrom):
                        imported = {alias.name for alias in node.names}
                        if node.level == 1 and node.module is None:
                            self.assertNotIn("memory_db", imported)
                        self.assertNotEqual(node.module, "memory_db")
                        self.assertNotEqual(node.module, "eneo_review_tools.memory_db")
                        self.assertFalse(
                            node.level == 1 and node.module == "memory_db"
                        )

    def test_public_plugin_does_not_import_offline_learning_modules(self):
        for path in sorted(PLUGIN.glob("*.py")):
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported = {alias.name.split(".")[0] for alias in node.names}
                        self.assertTrue(OFFLINE_OPERATOR_MODULES.isdisjoint(imported))
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported = node.module.split(".")[0]
                        self.assertNotIn(imported, OFFLINE_OPERATOR_MODULES)


if __name__ == "__main__":
    unittest.main()
