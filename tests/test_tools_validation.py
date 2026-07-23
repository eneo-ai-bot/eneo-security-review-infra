from __future__ import annotations

import email.message
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PACKAGE_ROOT))

from eneo_review_tools import memory_db, review_publisher, schemas, tools  # noqa: E402
import eneo_review_tools  # noqa: E402


class _FakeResponse:
    """Minimal context-manager stand-in for urlopen's response object."""

    def __init__(self, body: bytes = b"{}", headers: dict | None = None):
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, _n: int = -1) -> bytes:
        return self._body


class _FakeRegistry:
    def __init__(self):
        self.tools = {}

    def register_tool(self, *, name, toolset, schema, handler):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
        }


class _FakeGitHub:
    def current_user_login(self):
        return "eneo-ai-bot"

    def get_pull_request(self, repository, pr_number):
        del repository, pr_number
        return review_publisher.PullRequestState(
            state="open",
            draft=False,
            base_sha="b" * 40,
            head_sha="a" * 40,
        )

    def list_issue_comments(self, repository, issue_number, *, max_pages=3):
        del repository, issue_number, max_pages
        return []

    def update_issue_comment(self, repository, comment_id, body):
        del repository, body
        return review_publisher.IssueComment(
            comment_id=comment_id,
            body="updated",
            author_login="eneo-ai-bot",
        )

    def create_issue_comment(self, repository, issue_number, body):
        del repository, issue_number
        return review_publisher.IssueComment(
            comment_id=123,
            body=body,
            author_login="eneo-ai-bot",
        )

    def delete_issue_comment(self, repository, comment_id):
        del repository, comment_id


class ToolValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "memory.sqlite3")
        self.env = {
            "ENEO_ALLOWED_REPOSITORIES": "eneo/platform",
            "ENEO_REVIEW_DB": self.db,
        }
        memory_db.connect(self.db).close()
        self.finding = {
            "rule_id": "correctness.boolean-default",
            "category": "correctness",
            "path": "backend/changed.py",
            "line": 10,
            "symbol": "handler",
            "anchor": "feature default",
            "title": "Boolean default remains disabled",
            "severity": "Critical",
            "publication_score": 9,
            "confidence": 0.9,
            "evidence": "Concrete evidence.",
            "disproof_checks": "Checked the guard.",
            "impact": "The feature remains unavailable.",
            "smallest_fix": "Restore the enabled default.",
            "introduced_by_diff": True,
        }

    def tearDown(self):
        self.temp.cleanup()

    def start_run(self, *, head_sha: str = "a" * 40, base_sha: str = "b" * 40) -> int:
        connection = memory_db.connect_existing(self.db)
        try:
            run = memory_db.start_run(
                connection,
                "eneo/platform",
                1,
                base_sha=base_sha,
                head_sha=head_sha,
            )
            return int(run["id"])
        finally:
            connection.close()

    def select_optional_suggestions(
        self,
        findings: list[dict[str, object]],
        *,
        patch_text: str,
        head_text: str,
        suppressed_indices: frozenset[int] = frozenset(),
    ) -> tuple[int, dict[int, str]]:
        run_id = self.start_run()
        connection = memory_db.connect_existing(self.db)
        try:
            recorded = memory_db.record_findings(
                connection,
                "eneo/platform",
                1,
                "a" * 40,
                findings,
                review_run_id=run_id,
                base_sha="b" * 40,
                context_hashes={"backend/changed.py": "c" * 40},
            )
        finally:
            connection.close()
        for index in suppressed_indices:
            recorded[index]["suppressed"] = True
        pull = {
            "head": {
                "sha": "a" * 40,
                "repo": {"full_name": "eneo/platform"},
            }
        }
        changed = {
            "path": "backend/changed.py",
            "patch": patch_text,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_file_at_revision", return_value=head_text.encode()),
        ):
            return tools._record_optional_suggestions(
                repository="eneo/platform",
                pr_number=1,
                head_sha="a" * 40,
                pull=pull,
                findings=findings,
                recorded=recorded,
                changed_by_path={"backend/changed.py": changed},
            )

    def test_empty_allowlist_denies_by_default(self):
        with patch.dict(os.environ, {"ENEO_ALLOWED_REPOSITORIES": ""}, clear=False):
            result = json.loads(
                tools.review_begin({"repository": "eneo/platform", "pr_number": 1})
            )
        self.assertIn("deny by default", result["error"])

    def test_schema_severities_come_from_memory_owner(self):
        severity_schema = schemas.ENEO_REVIEW_MEMORY_RECORD["parameters"]["properties"][
            "findings"
        ]["items"]["properties"]["severity"]
        self.assertEqual(severity_schema["enum"], sorted(memory_db.SEVERITIES))

    def test_schema_finding_text_limits_come_from_validation_owner(self):
        finding_properties = schemas.ENEO_REVIEW_MEMORY_RECORD["parameters"][
            "properties"
        ]["findings"]["items"]["properties"]

        for field, maximum in memory_db.FINDING_TEXT_LIMITS.items():
            with self.subTest(field=field):
                self.assertEqual(finding_properties[field]["maxLength"], maximum)

    def test_schema_exposes_bounded_optional_atomic_suggestion(self):
        properties = schemas.ENEO_REVIEW_MEMORY_RECORD["parameters"]["properties"][
            "findings"
        ]["items"]["properties"]
        suggestion = properties["suggestion"]

        self.assertFalse(suggestion["additionalProperties"])
        self.assertEqual(
            set(suggestion["required"]),
            {"start_line", "end_line", "expected_text", "replacement_text"},
        )
        self.assertEqual(
            suggestion["properties"]["replacement_text"]["maxLength"],
            memory_db.MAX_SUGGESTION_TEXT_CHARS,
        )

    def test_schema_describes_demonstrated_paths_and_complete_remediation(self):
        finding_properties = schemas.ENEO_REVIEW_MEMORY_RECORD["parameters"][
            "properties"
        ]["findings"]["items"]["properties"]

        evidence_contract = finding_properties["evidence"]["description"]
        remediation_contract = finding_properties["smallest_fix"]["description"]

        self.assertIn("primary executed failure path", evidence_contract)
        self.assertIn(
            "fallback or secondary path unless it is independently traced",
            evidence_contract,
        )
        self.assertIn("every proven sibling lifecycle path", remediation_contract)
        self.assertIn("One lowest-risk owner-aligned remediation", remediation_contract)
        self.assertIn(
            "real behavior boundary implicated by the finding",
            remediation_contract,
        )
        self.assertIn(
            "actual downstream consumer rather than only a helper property",
            remediation_contract,
        )
        self.assertIn(
            "Offer alternatives only when an external contract requires a developer "
            "decision",
            remediation_contract,
        )

    def test_schema_prior_verdicts_come_from_memory_owner(self):
        deliver_verdict_schema = schemas.ENEO_REVIEW_DELIVER["parameters"]["properties"][
            "previous_verdicts"
        ]["items"]["properties"]["verdict"]
        self.assertEqual(
            deliver_verdict_schema["enum"],
            list(memory_db.PRIOR_FINDING_VERDICTS),
        )

    def test_plugin_registers_all_declared_tools(self):
        registry = _FakeRegistry()

        eneo_review_tools.register(registry)

        self.assertEqual(
            set(registry.tools),
            {
                "eneo_review_begin",
                "eneo_pr_diff",
                "eneo_pr_files",
                "eneo_pr_file",
                "eneo_review_memory_context",
                "eneo_review_memory_record",
                "eneo_review_deliver",
            },
        )
        for item in registry.tools.values():
            self.assertEqual(item["toolset"], "eneo_review")
            self.assertIsInstance(item["schema"], dict)
            self.assertTrue(callable(item["handler"]))

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
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
        ):
            run_id = self.start_run(head_sha="b" * 40)
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "b" * 40,
                        "run_id": run_id,
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
            "base": {"sha": "b" * 40},
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
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [finding],
                    }
                )
            )
        self.assertIn("changed pull-request file", result["error"])

    def test_record_partial_enumeration_still_records_enumerated_finding(self):
        # GitHub reports more changed files than were enumerated (e.g. a PR beyond the
        # ~3000-file files-API ceiling). A finding on an ENUMERATED file must still
        # record (honest-partial) rather than be hard-refused; coverage stays
        # incomplete (surfaced by the renderer banner) but is never silently dropped.
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 2,
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
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [self.finding],
                    }
                )
            )
        self.assertNotIn("error", result)
        self.assertEqual(result["recorded"][0]["context_hash"], "c" * 40)

    def test_record_uses_trusted_blob_hash(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
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
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
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
            "base": {"sha": "b" * 40},
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
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [self.finding],
                    }
                )
            )
        self.assertEqual(result["recorded"][0]["context_hash"], "a" * 40)

    def test_record_validates_and_persists_atomic_suggestion(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {
                "sha": "a" * 40,
                "repo": {"full_name": "eneo/platform"},
            },
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        changed = {
            "path": "backend/changed.py",
            "context_hash": "c" * 40,
            "context_hash_source": "blob",
            "patch": "@@ -9,2 +9,2 @@\n context\n-old = None\n+enabled = False",
        }
        finding = dict(
            self.finding,
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "enabled = False",
                "replacement_text": "enabled = True",
            },
        )
        head = "\n".join([*(f"line {number}" for number in range(1, 10)), "enabled = False"])
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(tools, "_changed_files", return_value=[changed]),
            patch.object(tools, "_file_at_revision", return_value=head.encode()),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [finding],
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["suggestions_recorded"], 1)
        self.assertEqual(result["recorded"][0]["suggestion"], {"status": "recorded"})
        connection = memory_db.connect_existing(self.db)
        try:
            row = connection.execute("SELECT * FROM review_suggestions").fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(row["start_line"], 10)
        self.assertEqual(row["replacement_text"], "enabled = True")

    def test_high_risk_finding_never_persists_atomic_suggestion(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {
                "sha": "a" * 40,
                "repo": {"full_name": "eneo/platform"},
            },
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        changed = {
            "path": "backend/changed.py",
            "context_hash": "c" * 40,
            "context_hash_source": "blob",
            "patch": "@@ -9,2 +9,2 @@\n context\n-old = None\n+enabled = False",
        }
        finding = dict(
            self.finding,
            rule_id="tenant.missing-scope",
            category="security",
            title="Tenant scope omitted",
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "enabled = False",
                "replacement_text": "enabled = True",
            },
        )
        head = "\n".join(
            [*(f"line {number}" for number in range(1, 10)), "enabled = False"]
        )
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(tools, "_changed_files", return_value=[changed]),
            patch.object(tools, "_file_at_revision", return_value=head.encode()) as read,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [finding],
                    }
                )
            )

        self.assertEqual(result["suggestions_recorded"], 0)
        self.assertEqual(
            result["recorded"][0]["suggestion"]["reason"],
            "suggestion_high_risk_category",
        )
        read.assert_not_called()

    def test_invalid_optional_suggestion_does_not_drop_finding(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {
                "sha": "a" * 40,
                "repo": {"full_name": "eneo/platform"},
            },
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        changed = {
            "path": "backend/changed.py",
            "context_hash": "c" * 40,
            "context_hash_source": "blob",
            "patch": "@@ -9,2 +9,2 @@\n context\n-old = None\n+enabled = False",
        }
        finding = dict(
            self.finding,
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "not the head text",
                "replacement_text": "enabled = True",
            },
        )
        head = "\n".join([*(f"line {number}" for number in range(1, 10)), "enabled = False"])
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
            patch.object(tools, "_changed_files", return_value=[changed]),
            patch.object(tools, "_file_at_revision", return_value=head.encode()),
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [finding],
                    }
                )
            )

        self.assertNotIn("error", result)
        self.assertEqual(result["suggestions_recorded"], 0)
        self.assertEqual(result["recorded"][0]["suggestion"]["status"], "omitted")
        self.assertEqual(
            result["recorded"][0]["suggestion"]["reason"],
            "suggestion_expected_text_mismatch",
        )
        connection = memory_db.connect_existing(self.db)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM finding_observations").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_suggestions").fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_overlapping_candidates_keep_higher_priority_patch(self):
        medium = dict(
            self.finding,
            rule_id="correctness.medium-default",
            severity="Medium",
            publication_score=8,
            symbol="medium_default",
            anchor="medium default",
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "enabled = False",
                "replacement_text": "enabled = maybe",
            },
        )
        high = dict(
            self.finding,
            rule_id="correctness.high-default",
            severity="High",
            publication_score=9,
            symbol="high_default",
            anchor="high default",
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "enabled = False",
                "replacement_text": "enabled = True",
            },
        )

        count, statuses = self.select_optional_suggestions(
            [medium, high],
            patch_text="@@ -9,2 +9,2 @@\n context\n-old = None\n+enabled = False",
            head_text="\n".join(
                [*(f"line {number}" for number in range(1, 10)), "enabled = False"]
            ),
        )

        self.assertEqual(count, 1)
        self.assertEqual(statuses[1], "recorded")
        self.assertEqual(statuses[0], "suggestion_overlaps_higher_priority_patch")
        connection = memory_db.connect_existing(self.db)
        try:
            row = connection.execute(
                "SELECT fingerprint, replacement_text FROM review_suggestions"
            ).fetchone()
            high_fingerprint = connection.execute(
                "SELECT fingerprint FROM findings WHERE rule_id = ?",
                ("correctness.high-default",),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(row["fingerprint"], high_fingerprint)
        self.assertEqual(row["replacement_text"], "enabled = True")

    def test_same_head_canonical_patch_precedes_new_overlap_selection(self):
        head_text = "\n".join(f"value_{line} = 0" for line in range(1, 21))
        patch_text = "@@ -0,0 +1,20 @@\n" + "\n".join(
            f"+value_{line} = 0" for line in range(1, 21)
        )
        first_finding = dict(
            self.finding,
            rule_id="correctness.canonical-owner",
            line=10,
            symbol="canonical_owner",
            anchor="canonical owner",
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "value_10 = 0",
                "replacement_text": "value_10 = 1",
            },
        )

        first_count, first_statuses = self.select_optional_suggestions(
            [first_finding], patch_text=patch_text, head_text=head_text
        )
        self.assertEqual(first_count, 1)
        self.assertEqual(first_statuses[0], "recorded")

        connection = memory_db.connect_existing(self.db)
        try:
            first_run_id = int(
                connection.execute(
                    "SELECT id FROM review_runs WHERE status = 'running'"
                ).fetchone()[0]
            )
            memory_db.complete_run(
                connection,
                first_run_id,
                repository="eneo/platform",
                pr_number=1,
                status="generated",
                findings_count=1,
            )
        finally:
            connection.close()

        repeated_finding = dict(
            first_finding,
            line=20,
            suggestion={
                "start_line": 20,
                "end_line": 20,
                "expected_text": "value_20 = 0",
                "replacement_text": "value_20 = 1",
            },
        )
        newly_overlapping = dict(
            self.finding,
            rule_id="correctness.new-overlap",
            severity="High",
            line=10,
            symbol="new_overlap",
            anchor="new overlap",
            suggestion={
                "start_line": 10,
                "end_line": 10,
                "expected_text": "value_10 = 0",
                "replacement_text": "value_10 = 2",
            },
        )

        second_count, second_statuses = self.select_optional_suggestions(
            [repeated_finding, newly_overlapping],
            patch_text=patch_text,
            head_text=head_text,
        )

        self.assertEqual(second_count, 1)
        self.assertEqual(second_statuses[0], "recorded")
        self.assertEqual(
            second_statuses[1], "suggestion_overlaps_higher_priority_patch"
        )
        connection = memory_db.connect_existing(self.db)
        try:
            second_run_id = int(
                connection.execute(
                    "SELECT id FROM review_runs WHERE status = 'running'"
                ).fetchone()[0]
            )
            rows = connection.execute(
                """
                SELECT start_line, end_line, replacement_text
                FROM review_suggestions
                WHERE review_run_id = ?
                """,
                (second_run_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start_line"], 10)
        self.assertEqual(rows[0]["end_line"], 10)
        self.assertEqual(rows[0]["replacement_text"], "value_10 = 1")

    def test_atomic_suggestion_review_is_capped_at_twelve(self):
        findings: list[dict[str, object]] = []
        for line in range(1, 14):
            findings.append(
                dict(
                    self.finding,
                    rule_id=f"correctness.atomic-{line:02d}",
                    line=line,
                    symbol=f"atomic_{line}",
                    anchor=f"atomic line {line}",
                    severity="Medium",
                    publication_score=8,
                    suggestion={
                        "start_line": line,
                        "end_line": line,
                        "expected_text": f"value_{line} = 0",
                        "replacement_text": f"value_{line} = 1",
                    },
                )
            )
        patch_text = "@@ -0,0 +1,13 @@\n" + "\n".join(
            f"+value_{line} = 0" for line in range(1, 14)
        )
        head_text = "\n".join(f"value_{line} = 0" for line in range(1, 14))

        count, statuses = self.select_optional_suggestions(
            findings, patch_text=patch_text, head_text=head_text
        )

        self.assertEqual(count, memory_db.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW)
        self.assertEqual(
            sum(reason == "suggestion_review_limit" for reason in statuses.values()),
            1,
        )
        connection = memory_db.connect_existing(self.db)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM review_suggestions").fetchone()[0],
                memory_db.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW,
            )
        finally:
            connection.close()

    def test_suppressed_candidates_do_not_consume_the_atomic_patch_limit(self):
        findings: list[dict[str, object]] = []
        for line in range(1, 14):
            findings.append(
                dict(
                    self.finding,
                    rule_id=f"correctness.suppression-{line:02d}",
                    line=line,
                    symbol=f"suppression_{line}",
                    anchor=f"suppression line {line}",
                    severity="High" if line <= 12 else "Medium",
                    suggestion={
                        "start_line": line,
                        "end_line": line,
                        "expected_text": f"value_{line} = 0",
                        "replacement_text": f"value_{line} = 1",
                    },
                )
            )
        patch_text = "@@ -0,0 +1,13 @@\n" + "\n".join(
            f"+value_{line} = 0" for line in range(1, 14)
        )
        head_text = "\n".join(f"value_{line} = 0" for line in range(1, 14))

        count, statuses = self.select_optional_suggestions(
            findings,
            patch_text=patch_text,
            head_text=head_text,
            suppressed_indices=frozenset(range(12)),
        )

        self.assertEqual(count, 1)
        self.assertTrue(
            all(statuses[index] == "suggestion_finding_suppressed" for index in range(12))
        )
        self.assertEqual(statuses[12], "recorded")

    def test_atomic_patch_limit_stops_additional_head_file_reads(self):
        findings: list[dict[str, object]] = []
        changed_by_path: dict[str, dict[str, object]] = {}
        context_hashes: dict[str, str] = {}
        for index in range(1, 14):
            path = f"src/atomic_{index:02d}.py"
            findings.append(
                dict(
                    self.finding,
                    rule_id=f"correctness.atomic-file-{index:02d}",
                    path=path,
                    line=1,
                    symbol=f"atomic_file_{index}",
                    anchor=f"atomic file {index}",
                    severity="Medium",
                    suggestion={
                        "start_line": 1,
                        "end_line": 1,
                        "expected_text": "value = 0",
                        "replacement_text": "value = 1",
                    },
                )
            )
            changed_by_path[path] = {
                "path": path,
                "patch": "@@ -0,0 +1 @@\n+value = 0",
            }
            context_hashes[path] = "c" * 40
        run_id = self.start_run()
        connection = memory_db.connect_existing(self.db)
        try:
            recorded = memory_db.record_findings(
                connection,
                "eneo/platform",
                1,
                "a" * 40,
                findings,
                review_run_id=run_id,
                base_sha="b" * 40,
                context_hashes=context_hashes,
            )
        finally:
            connection.close()
        pull = {
            "head": {
                "sha": "a" * 40,
                "repo": {"full_name": "eneo/platform"},
            }
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(
                tools,
                "_file_at_revision",
                return_value=b"value = 0",
            ) as read,
        ):
            count, statuses = tools._record_optional_suggestions(
                repository="eneo/platform",
                pr_number=1,
                head_sha="a" * 40,
                pull=pull,
                findings=findings,
                recorded=recorded,
                changed_by_path=changed_by_path,
            )

        self.assertEqual(count, memory_db.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW)
        self.assertEqual(read.call_count, memory_db.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW)
        self.assertEqual(statuses[12], "suggestion_review_limit")

    def test_deliver_rejects_stale_head_sha(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=pull),
        ):
            run_id = self.start_run(head_sha="b" * 40)
            result = json.loads(
                tools.review_deliver(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "b" * 40,
                        "run_id": run_id,
                    }
                )
            )
        self.assertIn("does not match", result["error"])

    def test_pr_diff_rejects_changed_base_snapshot_before_network(self):
        initial = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
            "changed_files": 1,
        }
        moved_base = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "c" * 40},
            "changed_files": 1,
        }
        with patch.dict(os.environ, self.env, clear=False):
            with patch.object(tools, "_pr", return_value=initial):
                run_id = self.start_run()
            with (
                patch.object(tools, "_pr", return_value=moved_base),
                patch.object(tools, "_request") as requester,
            ):
                result = json.loads(
                    tools.pr_diff(
                        {
                            "repository": "eneo/platform",
                            "pr_number": 1,
                            "run_id": run_id,
                        }
                    )
                )
        self.assertIn("base SHA changed", result["error"])
        requester.assert_not_called()

    def test_pr_file_rejects_changed_head_snapshot_before_reading_file(self):
        initial = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40, "repo": {"full_name": "eneo/platform"}},
            "base": {"sha": "b" * 40, "repo": {"full_name": "eneo/platform"}},
            "changed_files": 1,
        }
        moved_head = {
            "state": "open",
            "draft": False,
            "head": {"sha": "c" * 40, "repo": {"full_name": "eneo/platform"}},
            "base": {"sha": "b" * 40, "repo": {"full_name": "eneo/platform"}},
            "changed_files": 1,
        }
        with patch.dict(os.environ, self.env, clear=False):
            with patch.object(tools, "_pr", return_value=initial):
                run_id = self.start_run()
            with (
                patch.object(tools, "_pr", return_value=moved_head),
                patch.object(tools, "_file_at_revision") as reader,
            ):
                result = json.loads(
                    tools.pr_file(
                        {
                            "repository": "eneo/platform",
                            "pr_number": 1,
                            "path": "backend/changed.py",
                            "run_id": run_id,
                        }
                    )
                )
        self.assertIn("head SHA changed", result["error"])
        reader.assert_not_called()

    def test_deliver_finalizes_and_publishes_recorded_findings(self):
        pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"sha": "b" * 40},
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
            patch.object(review_publisher, "_default_gateway", return_value=_FakeGitHub()),
        ):
            run_id = self.start_run()
            record_result = json.loads(
                tools.review_memory_record(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                        "findings": [self.finding],
                    }
                )
            )
            deliver_result = json.loads(
                tools.review_deliver(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "head_sha": "a" * 40,
                        "run_id": run_id,
                    }
                )
            )

        self.assertEqual(deliver_result["findings_count"], 1)
        self.assertTrue(deliver_result["published"])
        with closing(memory_db.connect_existing(self.db)) as connection:
            publication = memory_db.list_publications(
                connection, repository="eneo/platform", pr_number=1
            )[0]
            rendered = connection.execute(
                "SELECT rendered_markdown FROM review_publications WHERE id = ?",
                (publication["id"],),
            ).fetchone()["rendered_markdown"]
        self.assertIn(
            "### F1 · Critical (P0): Boolean default remains disabled",
            rendered,
        )
        self.assertIn("Copyable fix brief for a coding agent", rendered)
        self.assertNotIn(
            record_result["recorded"][0]["fingerprint"],
            rendered.split("<!--", 1)[0],
        )

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
            patch.object(tools, "_changed_files", return_value=[]),
            patch.object(tools, "_file_at_revision", return_value=b"line one\n") as reader,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file(
                    {
                        "repository": "eneo/platform",
                        "pr_number": 1,
                        "path": "backend/app.py",
                        "side": "head",
                        "run_id": run_id,
                    }
                )
            )
        reader.assert_called_once_with("contributor/platform-fork", "backend/app.py", "a" * 40)
        self.assertEqual(result["source_repository"], "contributor/platform-fork")

    # --- read-failure fix: transport retry, stable not-found, path/side contract ---

    def _http_error(self, code: int):
        return urllib.error.HTTPError("https://api.github.com/x", code, "err", email.message.Message(), None)

    def _pull(self):
        return {
            "head": {"sha": "a" * 40, "repo": {"full_name": "eneo/platform"}},
            "base": {"sha": "b" * 40, "repo": {"full_name": "eneo/platform"}},
        }

    def test_request_retries_transient_5xx_then_succeeds(self):
        with (
            patch.object(tools.time, "sleep"),
            patch("urllib.request.urlopen", side_effect=[self._http_error(502), _FakeResponse(b'{"ok":true}')]) as opener,
        ):
            data, truncated, _ = tools._request("/repos/eneo/platform/pulls/1")
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(data, b'{"ok":true}')
        self.assertFalse(truncated)

    def test_request_does_not_retry_4xx(self):
        with patch("urllib.request.urlopen", side_effect=self._http_error(404)) as opener:
            with self.assertRaises(tools.NotFoundError):
                tools._request("/repos/eneo/platform/pulls/1")
        self.assertEqual(opener.call_count, 1)

    def test_request_does_not_retry_generic_urlerror(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")) as opener:
            with self.assertRaises(tools.ToolInputError):
                tools._request("/repos/eneo/platform/pulls/1")
        self.assertEqual(opener.call_count, 1)

    def test_file_not_found_message_is_stable_and_pathless(self):
        with patch.object(tools, "_request_json", side_effect=tools.NotFoundError("not found")):
            with self.assertRaises(tools.ToolInputError) as ctx:
                tools._file_at_revision("eneo/platform", "backend/guessed/path.py", "a" * 40)
        message = str(ctx.exception)
        self.assertIn("do not retry guessed paths", message)
        self.assertNotIn("backend/guessed/path.py", message)

    def test_pr_file_redirects_added_file_to_head_without_tool_failure(self):
        files = [{"path": "backend/new.py", "status": "added", "previous_path": None}]
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=files),
            patch.object(tools, "_file_at_revision") as reader,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file({"repository": "eneo/platform", "pr_number": 1, "path": "backend/new.py", "side": "base", "run_id": run_id})
            )
        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "side_unavailable")
        self.assertEqual(result["valid_side"], "head")
        self.assertIn("Do not retry side: base", result["next_action"])
        reader.assert_not_called()

    def test_pr_file_redirects_deleted_file_to_base_without_tool_failure(self):
        files = [{"path": "backend/gone.py", "status": "removed", "previous_path": None}]
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=files),
            patch.object(tools, "_file_at_revision") as reader,
        ):
            run_id = self.start_run()
            result = json.loads(
                tools.pr_file({"repository": "eneo/platform", "pr_number": 1, "path": "backend/gone.py", "side": "head", "run_id": run_id})
            )
        self.assertNotIn("error", result)
        self.assertEqual(result["file_state"], "side_unavailable")
        self.assertEqual(result["valid_side"], "base")
        self.assertIn("Do not retry side: head", result["next_action"])
        reader.assert_not_called()

    def test_pr_file_base_side_of_renamed_file_uses_previous_path(self):
        files = [{"path": "backend/new_name.py", "status": "renamed", "previous_path": "backend/old_name.py"}]
        with (
            patch.dict(os.environ, self.env, clear=False),
            patch.object(tools, "_pr", return_value=self._pull()),
            patch.object(tools, "_changed_files", return_value=files),
            patch.object(tools, "_file_at_revision", return_value=b"prior\n") as reader,
        ):
            run_id = self.start_run()
            json.loads(
                tools.pr_file({"repository": "eneo/platform", "pr_number": 1, "path": "backend/new_name.py", "side": "base", "run_id": run_id})
            )
        reader.assert_called_once_with("eneo/platform", "backend/old_name.py", "b" * 40)


if __name__ == "__main__":
    unittest.main()
