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
            "### F1 · Critical (P0): Tenant scope omitted",
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

    def test_pr_file_rejects_base_side_for_added_file_before_network(self):
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
        self.assertIn("added file has no base side", result["error"])
        reader.assert_not_called()

    def test_pr_file_rejects_head_side_for_deleted_file_before_network(self):
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
        self.assertIn("deleted file has no head side", result["error"])
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
