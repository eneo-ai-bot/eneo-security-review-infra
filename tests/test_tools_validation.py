from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from eneo_review_tools import tools  # noqa: E402


class ToolValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "memory.sqlite3")
        self.env = {
            "ENEO_ALLOWED_REPOSITORIES": "eneo/platform",
            "ENEO_REVIEW_DB": self.db,
        }
        self.finding = {
            "rule_id": "tenant.missing-scope",
            "category": "security",
            "path": "backend/changed.py",
            "line": 10,
            "symbol": "handler",
            "anchor": "POST /documents",
            "title": "Tenant scope omitted",
            "severity": "Critical",
            "publication_score": 9,
            "confidence": 0.9,
            "evidence": "Concrete evidence.",
            "disproof_checks": "Checked the guard.",
            "impact": "Cross-tenant write.",
            "smallest_fix": "Bind tenant from context.",
            "introduced_by_diff": True,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_allowlist_denies_by_default(self):
        with patch.dict(os.environ, {"ENEO_ALLOWED_REPOSITORIES": ""}, clear=False):
            result = json.loads(tools.pr_overview({"repository": "eneo/platform", "pr_number": 1}))
        self.assertIn("deny by default", result["error"])

    def test_non_allowlisted_repository_is_denied_before_network(self):
        with patch.dict(os.environ, self.env, clear=False):
            result = json.loads(tools.pr_diff({"repository": "other/project", "pr_number": 1}))
        self.assertEqual(result["error"], "repository is not allowlisted")

    def test_invalid_path_is_denied_before_network(self):
        with patch.dict(os.environ, self.env, clear=False):
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "path": "../../etc/passwd",
                    }
                )
            )
        self.assertIn("traversal", result["error"])

    def test_record_rejects_stale_head_sha(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "changed_files": 1,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
        ):
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "b" * 40,
                        "findings": [],
                    }
                )
            )
        self.assertIn("does not match", result["error"])

    def test_record_rejects_finding_outside_changed_files(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "changed_files": 1,
        }
        finding = dict(self.finding, path="backend/unchanged.py")
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(
                tools,
                "_changed_files",
                return_value=[
                    {
                        "path": "backend/changed.py",
                        "context_hash": "c" * 40,
                        "context_hash_source": "blob",
                    }
                ],
            ),
        ):
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "findings": [finding],
                    }
                )
            )
        self.assertIn("changed pull-request file", result["error"])

    def test_record_uses_trusted_blob_hash(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "changed_files": 1,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(
                tools,
                "_changed_files",
                return_value=[
                    {
                        "path": "backend/changed.py",
                        "context_hash": "c" * 40,
                        "context_hash_source": "blob",
                    }
                ],
            ),
        ):
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "findings": [self.finding],
                    }
                )
            )
        self.assertEqual(result["recorded"][0]["context_hash"], "c" * 40)
        self.assertFalse(result["recorded"][0]["suppressed"])

    def test_record_falls_back_to_exact_head_when_blob_hash_is_missing(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "changed_files": 1,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(
                tools,
                "_changed_files",
                return_value=[
                    {
                        "path": "backend/changed.py",
                        "context_hash": "c" * 64,
                        "context_hash_source": "patch",
                    }
                ],
            ),
        ):
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "findings": [self.finding],
                    }
                )
            )
        self.assertEqual(result["recorded"][0]["context_hash"], "a" * 40)

    def test_pr_file_reads_head_from_fork_repository(self):
        pull = {
            "head": {
                "sha": "a" * 40,
                "repo": {"full_name": "contributor/platform-fork"},
            },
            "base": {
                "sha": "b" * 40,
                "repo": {"full_name": "eneo/platform"},
            },
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(tools, "_file_at_revision", return_value=b"line one\n") as reader,
        ):
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "path": "backend/app.py",
                        "side": "head",
                    }
                )
            )
        reader.assert_called_once_with("contributor/platform-fork", "backend/app.py", "a" * 40)
        self.assertEqual(result["source_repository"], "contributor/platform-fork")


if __name__ == "__main__":
    unittest.main()
