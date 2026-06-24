from __future__ import annotations

import hashlib
import hmac
import os
from contextlib import closing
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

from eneo_review_tools import feedback_bridge, memory_db  # noqa: E402


class FakeGitHub:
    def __init__(
        self,
        *,
        body: str,
        actor_id: int = 12345,
        association: str = "OWNER",
        pr_state: str = "open",
        fail_reaction: bool = False,
    ) -> None:
        self.body = body
        self.actor_id = actor_id
        self.association = association
        self.pr_state = pr_state
        self.fail_reaction = fail_reaction
        self.reaction_attempts: list[tuple[int, feedback_bridge.Reaction]] = []
        self.created_reactions: set[tuple[int, feedback_bridge.Reaction]] = set()
        self.comments: list[str] = []

    def get_issue_comment(
        self, repository: str, comment_id: int
    ) -> feedback_bridge.IssueComment:
        return feedback_bridge.IssueComment(
            comment_id=comment_id,
            body=self.body,
            html_url=f"https://github.test/{repository}/pull/17#issuecomment-{comment_id}",
            issue_url=f"https://api.github.test/repos/{repository}/issues/17",
            actor_id=self.actor_id,
            actor_login="alice",
            author_association=self.association,
        )

    def get_pull_request(
        self, repository: str, pr_number: int
    ) -> feedback_bridge.PullRequest:
        return feedback_bridge.PullRequest(number=pr_number, state=self.pr_state)

    def create_issue_comment_reaction(
        self, repository: str, comment_id: int, content: feedback_bridge.Reaction
    ) -> bool:
        del repository
        if self.fail_reaction:
            raise feedback_bridge.GitHubError("reaction failed")
        self.reaction_attempts.append((comment_id, content))
        key = (comment_id, content)
        if key in self.created_reactions:
            return False
        self.created_reactions.add(key)
        return True

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> None:
        del repository, issue_number
        self.comments.append(body)


class FeedbackBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "memory.sqlite3")
        self.finding = {
            "rule_id": "tenant.missing-scope",
            "category": "security",
            "path": "backend/api/documents.py",
            "line": 42,
            "symbol": "create_document",
            "anchor": "POST /v1/documents",
            "title": "Document creation omits tenant scope",
            "severity": "High",
            "publication_score": 9,
            "confidence": 0.93,
            "evidence": "The changed query writes a caller-controlled tenant id.",
            "disproof_checks": "Checked the dependency and repository layer.",
            "impact": "Cross-tenant write.",
            "smallest_fix": "Bind tenant_id from context.",
            "introduced_by_diff": True,
        }
        with closing(memory_db.connect(self.db)) as connection:
            memory_db.record_findings(
                connection,
                "eneo/platform",
                17,
                "a" * 40,
                [self.finding],
                context_hashes={self.finding["path"]: "d" * 40},
            )
            memory_db.finalize_review(connection, "eneo/platform", 17, "a" * 40)
        self.config = feedback_bridge.BridgeConfig(
            secret="secret",
            token="token",
            allowed_repositories=frozenset({"eneo/platform"}),
            allowed_actor_ids="12345",
            database_path=self.db,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def payload(self, *, repository: str = "eneo/platform", comment_id: int = 500) -> dict[str, object]:
        return {
            "repository": {"full_name": repository},
            "pull_request": {"number": 17},
            "request": {"comment_id": comment_id},
            "requester": {"login": "spoofed-login"},
        }

    def count_rows(self, table: str) -> int:
        with closing(memory_db.connect(self.db)) as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_signature_verification_uses_hmac_sha256(self) -> None:
        body = b'{"request":{"comment_id":500}}'
        signature = "sha256=" + hmac.new(
            b"secret", body, hashlib.sha256
        ).hexdigest()

        self.assertTrue(feedback_bridge.verify_signature(body, signature, "secret"))
        self.assertFalse(feedback_bridge.verify_signature(body + b" ", signature, "secret"))
        self.assertFalse(feedback_bridge.verify_signature(body, "bad", "secret"))

    def test_config_requires_write_capable_github_token(self) -> None:
        environment = {
            "ENEO_FEEDBACK_WEBHOOK_SECRET": "secret",
            "ENEO_ALLOWED_REPOSITORIES": "eneo/platform",
            "GITHUB_READ_TOKEN": "read-only-token",
        }

        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(SystemExit, "GH_TOKEN is required"):
                feedback_bridge.load_config()

        environment["GH_TOKEN"] = "write-token"
        with patch.dict(os.environ, environment, clear=True):
            config = feedback_bridge.load_config()

        self.assertEqual(config.token, "write-token")

    def test_false_positive_records_and_confirms_with_success_reaction_only(self) -> None:
        github = FakeGitHub(
            body="/review false-positive F1 because the repository scopes tenant_id."
        )

        response = feedback_bridge.process_feedback(
            payload=self.payload(),
            config=self.config,
            github=github,
        )

        self.assertEqual(response.status, "recorded")
        self.assertEqual(github.reaction_attempts, [(500, "+1")])
        self.assertEqual(github.comments, [])
        self.assertEqual(self.count_rows("decisions"), 1)
        self.assertEqual(self.count_rows("decision_audit"), 1)

    def test_replay_uses_original_failure_outcome_not_success_reaction(self) -> None:
        with closing(memory_db.connect(self.db)) as connection:
            connection.execute("UPDATE publication_findings SET context_hash = ''")
            connection.commit()
        github = FakeGitHub(
            body="/review false-positive F1 because this was already reviewed."
        )

        first = feedback_bridge.process_feedback(
            payload=self.payload(),
            config=self.config,
            github=github,
        )
        second = feedback_bridge.process_feedback(
            payload=self.payload(),
            config=self.config,
            github=github,
        )

        self.assertEqual(first.status, "error_feedback")
        self.assertEqual(second.status, "error_feedback")
        self.assertEqual(github.reaction_attempts, [(500, "confused"), (500, "confused")])
        self.assertNotIn((500, "+1"), github.reaction_attempts)
        self.assertEqual(len(github.comments), 1)
        self.assertIn("trusted file context", github.comments[0])

    def test_intentional_command_is_unsupported_without_writing_decision(self) -> None:
        github = FakeGitHub(
            body="/review intentional F1 ADR-0042 because this is intended."
        )

        response = feedback_bridge.process_feedback(
            payload=self.payload(),
            config=self.config,
            github=github,
        )

        self.assertEqual(response.status, "error_feedback")
        self.assertEqual(github.reaction_attempts, [(500, "confused")])
        self.assertIn("not available from PR comments", github.comments[0])
        self.assertEqual(self.count_rows("decisions"), 0)
        self.assertEqual(self.count_rows("processed_feedback_events"), 0)

    def test_placeholder_command_gets_idempotent_help_without_decision(self) -> None:
        github = FakeGitHub(
            body="/review false-positive F1 because <what code disproves it>"
        )

        feedback_bridge.process_feedback(
            payload=self.payload(),
            config=self.config,
            github=github,
        )
        feedback_bridge.process_feedback(
            payload=self.payload(),
            config=self.config,
            github=github,
        )

        self.assertEqual(github.reaction_attempts, [(500, "confused"), (500, "confused")])
        self.assertEqual(len(github.comments), 1)
        self.assertIn("replace placeholder text", github.comments[0])
        self.assertEqual(self.count_rows("decisions"), 0)

    def test_unauthorized_actor_gets_no_public_response_or_event_row(self) -> None:
        github = FakeGitHub(
            body="/review false-positive F1 because the guard exists.",
            actor_id=999,
        )

        response = feedback_bridge.process_feedback(
            payload=self.payload(),
            config=self.config,
            github=github,
        )

        self.assertEqual(response.status, "unauthorized")
        self.assertEqual(github.reaction_attempts, [])
        self.assertEqual(github.comments, [])
        self.assertEqual(self.count_rows("processed_feedback_events"), 0)
        self.assertEqual(self.count_rows("decisions"), 0)

    def test_recorded_feedback_is_not_rolled_back_when_confirmation_fails(self) -> None:
        github = FakeGitHub(
            body="/review false-positive F1 because the repository scopes tenant_id.",
            fail_reaction=True,
        )

        with self.assertRaisesRegex(feedback_bridge.GitHubError, "reaction failed"):
            feedback_bridge.process_feedback(
                payload=self.payload(),
                config=self.config,
                github=github,
            )

        self.assertEqual(self.count_rows("decisions"), 1)
        self.assertEqual(self.count_rows("decision_audit"), 1)
        self.assertEqual(self.count_rows("processed_feedback_events"), 1)


if __name__ == "__main__":
    unittest.main()
