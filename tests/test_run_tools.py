from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

PLUGINS = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGINS))

from eneo_review_tools import memory_db, review_publisher, tools  # noqa: E402


class FakeGitHub:
    def __init__(
        self,
        *,
        base_sha="b" * 40,
        head_sha="a" * 40,
    ):
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.created = []
        self.next_comment_id = 1000

    def current_user_login(self):
        return "eneo-ai-bot"

    def get_pull_request(self, repository, pr_number):
        del repository, pr_number
        return review_publisher.PullRequestState(
            state="open",
            draft=False,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
        )

    def list_issue_comments(self, repository, issue_number):
        del repository, issue_number
        return []

    def update_issue_comment(self, repository, comment_id, body):
        del repository
        return review_publisher.IssueComment(
            comment_id=comment_id,
            body=body,
            author_login="eneo-ai-bot",
        )

    def create_issue_comment(self, repository, issue_number, body):
        del repository, issue_number
        self.next_comment_id += 1
        self.created.append(body)
        return review_publisher.IssueComment(
            comment_id=self.next_comment_id,
            body=body,
            author_login="eneo-ai-bot",
        )

    def delete_issue_comment(self, repository, comment_id):
        del repository, comment_id


class RunToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self._env = dict(os.environ)
        os.environ["ENEO_REVIEW_DB"] = str(Path(self.temp.name) / "memory.sqlite3")
        os.environ["ENEO_ALLOWED_REPOSITORIES"] = "eneo-ai/eneo"
        memory_db.connect(os.environ["ENEO_REVIEW_DB"]).close()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self.temp.cleanup()

    def call(self, handler, args):
        return json.loads(handler(args))

    def start(self, pr=498, sha="a" * 40):
        return self.call(
            tools.review_run_start,
            {"repository": "eneo-ai/eneo", "pr_number": pr, "head_sha": sha},
        )

    def test_start_then_complete(self):
        start = self.start()
        self.assertEqual(start["status"], "running")
        self.assertIn("run_id", start)
        done = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 498, "run_id": start["run_id"],
             "status": "generated", "findings_count": 2, "posted_comment_id": 123},
        )
        self.assertTrue(done["updated"])
        self.assertEqual(done["status"], "generated")
        with closing(memory_db.connect()) as connection:
            self.assertEqual(memory_db.list_runs(connection)[0]["findings_count"], 2)

    def test_overlapping_runs_complete_by_id(self):
        a = self.start(pr=7, sha="a" * 40)
        b = self.start(pr=7, sha="b" * 40)
        self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 7, "run_id": a["run_id"], "status": "generated", "findings_count": 1, "posted_comment_id": 123},
        )
        with closing(memory_db.connect()) as connection:
            runs = {r["id"]: r for r in memory_db.list_runs(connection)}
        self.assertEqual(runs[a["run_id"]]["status"], "generated")
        self.assertEqual(runs[b["run_id"]]["status"], "running")

    def test_missing_run_id_rejected(self):
        result = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 1, "status": "generated"},
        )
        self.assertIn("error", result)
        self.assertIn("run_id", result["error"])

    def test_unknown_run_id_is_noop(self):
        result = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 1, "run_id": 999, "status": "generated", "posted_comment_id": 123},
        )
        self.assertFalse(result["updated"])

    def test_non_allowlisted_repo_rejected(self):
        result = self.call(
            tools.review_run_start,
            {"repository": "evil/repo", "pr_number": 1, "head_sha": "a" * 40},
        )
        self.assertIn("error", result)
        self.assertIn("allowlisted", result["error"])

    def test_bad_head_sha_rejected(self):
        result = self.call(
            tools.review_run_start,
            {"repository": "eneo-ai/eneo", "pr_number": 1, "head_sha": "not-a-sha"},
        )
        self.assertIn("error", result)
        self.assertIn("head_sha", result["error"])

    def test_bad_findings_count_is_input_error(self):
        start = self.start(pr=3)
        result = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 3, "run_id": start["run_id"],
             "status": "generated", "findings_count": "lots"},
        )
        self.assertIn("error", result)

    def test_done_status_aliases_to_generated_for_older_prompts(self):
        start = self.start(pr=5)
        result = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 5, "run_id": start["run_id"], "status": "done", "posted_comment_id": 123},
        )
        self.assertTrue(result["updated"])
        self.assertEqual(result["status"], "generated")

    def test_generated_completion_requires_posted_comment(self):
        start = self.start(pr=6)
        result = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 6, "run_id": start["run_id"], "status": "generated"},
        )
        self.assertIn("error", result)
        self.assertIn("posted_comment_id is required", result["error"])

    def test_bad_status_rejected(self):
        start = self.start(pr=4)
        result = self.call(
            tools.review_run_complete,
            {"repository": "eneo-ai/eneo", "pr_number": 4, "run_id": start["run_id"], "status": "suppressed"},
        )
        self.assertIn("error", result)

    def finding(self):
        return {
            "rule_id": "tenant.missing-scope",
            "category": "security",
            "path": "backend/api.py",
            "line": 42,
            "symbol": "handler",
            "anchor": "POST /api",
            "title": "Tenant scope omitted",
            "severity": "High",
            "publication_score": 9,
            "confidence": 0.9,
            "evidence": "Concrete evidence.",
            "disproof_checks": "Checked the guard.",
            "impact": "Cross-tenant write.",
            "smallest_fix": "Bind tenant from context.",
            "introduced_by_diff": True,
        }

    def pull(self, *, base_sha="b" * 40, head_sha="a" * 40):
        return {
            "state": "open",
            "draft": False,
            "head": {"sha": head_sha},
            "base": {"sha": base_sha},
        }

    def prepare_recorded_review(self, *, pr=9, base_sha="b" * 40, head_sha="a" * 40):
        finding = self.finding()
        with closing(memory_db.connect()) as connection:
            memory_db.record_findings(
                connection,
                "eneo-ai/eneo",
                pr,
                head_sha,
                [finding],
                base_sha=base_sha,
                context_hashes={finding["path"]: "d" * 40},
            )
            run = memory_db.start_run(
                connection,
                "eneo-ai/eneo",
                pr,
                base_sha=base_sha,
                head_sha=head_sha,
            )
        return int(run["id"])

    def test_deliver_publishes_and_completes_run(self):
        run_id = self.prepare_recorded_review()
        github = FakeGitHub()
        with (
            patch.object(tools, "_pr", return_value=self.pull()),
            patch.object(review_publisher, "_default_gateway", return_value=github),
        ):
            result = self.call(
                tools.review_deliver,
                {
                    "repository": "eneo-ai/eneo",
                    "pr_number": 9,
                    "head_sha": "a" * 40,
                    "run_id": run_id,
                },
            )

        self.assertTrue(result["published"])
        self.assertEqual(result["stage"], "delivered")
        with closing(memory_db.connect()) as connection:
            run = memory_db.list_runs(connection, repository="eneo-ai/eneo")[0]
            publication = memory_db.list_publications(connection, repository="eneo-ai/eneo", pr_number=9)[0]
        self.assertEqual(run["status"], "generated")
        self.assertEqual(run["posted_comment_id"], result["comment_id"])
        self.assertEqual(publication["delivery_status"], "posted")
        self.assertEqual(publication["comment_id"], result["comment_id"])

    def test_deliver_records_publish_failure_and_failed_run(self):
        run_id = self.prepare_recorded_review(pr=10)
        github = FakeGitHub(base_sha="c" * 40)
        with (
            patch.object(tools, "_pr", return_value=self.pull()),
            patch.object(review_publisher, "_default_gateway", return_value=github),
        ):
            result = self.call(
                tools.review_deliver,
                {
                    "repository": "eneo-ai/eneo",
                    "pr_number": 10,
                    "head_sha": "a" * 40,
                    "run_id": run_id,
                },
            )

        self.assertFalse(result["published"])
        self.assertEqual(result["delivery_status"], "stale")
        self.assertEqual(result["failure_code"], "base_sha_changed")
        with closing(memory_db.connect()) as connection:
            run = memory_db.list_runs(connection, repository="eneo-ai/eneo")[0]
            publication = memory_db.list_publications(connection, repository="eneo-ai/eneo", pr_number=10)[0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(publication["delivery_status"], "stale")
        self.assertEqual(publication["failure_code"], "base_sha_changed")


if __name__ == "__main__":
    unittest.main()
