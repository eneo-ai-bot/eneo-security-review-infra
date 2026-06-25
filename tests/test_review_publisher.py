from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

from eneo_review_tools import memory_db, review_publisher  # noqa: E402


class FakeGitHub:
    def __init__(
        self,
        *,
        base_sha: str = "b" * 40,
        head_sha: str = "a" * 40,
        comments: list[review_publisher.IssueComment] | None = None,
    ) -> None:
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.comments = list(comments or [])
        self.created: list[str] = []
        self.updated: list[tuple[int, str]] = []
        self.next_comment_id = 1000

    def get_pull_request(
        self, repository: str, pr_number: int
    ) -> review_publisher.PullRequestState:
        del repository, pr_number
        return review_publisher.PullRequestState(
            state="open",
            draft=False,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
        )

    def get_issue_comment(
        self, repository: str, comment_id: int
    ) -> review_publisher.IssueComment | None:
        del repository
        for comment in self.comments:
            if comment.comment_id == comment_id:
                return comment
        return None

    def list_issue_comments(
        self, repository: str, issue_number: int
    ) -> list[review_publisher.IssueComment]:
        del repository, issue_number
        return list(self.comments)

    def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> review_publisher.IssueComment:
        del repository
        updated = review_publisher.IssueComment(comment_id=comment_id, body=body)
        self.comments = [
            updated if item.comment_id == comment_id else item for item in self.comments
        ]
        self.updated.append((comment_id, body))
        return updated

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> review_publisher.IssueComment:
        del repository, issue_number
        self.next_comment_id += 1
        created = review_publisher.IssueComment(
            comment_id=self.next_comment_id, body=body
        )
        self.comments.append(created)
        self.created.append(body)
        return created


class ReviewPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = memory_db.connect(str(Path(self.temp.name) / "memory.sqlite3"))
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

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def generate(self, *, base_sha: str = "b" * 40, head_sha: str = "a" * 40):
        memory_db.record_findings(
            self.connection,
            "eneo/platform",
            17,
            head_sha,
            [self.finding],
            base_sha=base_sha,
            context_hashes={self.finding["path"]: "d" * 40},
        )
        run = memory_db.start_run(
            self.connection,
            "eneo/platform",
            17,
            base_sha=base_sha,
            head_sha=head_sha,
        )
        publication = memory_db.finalize_review(
            self.connection,
            "eneo/platform",
            17,
            head_sha,
            review_run_id=int(run["id"]),
        )
        return run, publication

    def test_generation_does_not_supersede_current_posted_review(self) -> None:
        first_run, first = self.generate()
        posted = review_publisher.publish_review(
            self.connection,
            publication_id=int(first["publication_id"]),
            review_run_id=int(first_run["id"]),
            github=FakeGitHub(),
        )
        self.assertTrue(posted["published"])

        second_run, second = self.generate(head_sha="c" * 40)

        rows = self.connection.execute(
            "SELECT id, delivery_status, superseded_at FROM review_publications ORDER BY id"
        ).fetchall()
        self.assertEqual(rows[0]["delivery_status"], "posted")
        self.assertIsNone(rows[0]["superseded_at"])
        self.assertEqual(rows[1]["delivery_status"], "generated")
        self.assertIsNone(rows[1]["superseded_at"])
        current = memory_db.resolve_current_review_state(
            self.connection, repository="eneo/platform", pr_number=17
        )
        self.assertEqual(current.publication_id, int(first["publication_id"]))
        self.assertEqual(int(second_run["id"]), int(second["review_run_id"]))

    def test_publish_updates_previous_canonical_comment_after_success(self) -> None:
        first_run, first = self.generate()
        github = FakeGitHub()
        first_publish = review_publisher.publish_review(
            self.connection,
            publication_id=int(first["publication_id"]),
            review_run_id=int(first_run["id"]),
            github=github,
        )
        second_run, second = self.generate(head_sha="c" * 40)
        github.head_sha = "c" * 40

        second_publish = review_publisher.publish_review(
            self.connection,
            publication_id=int(second["publication_id"]),
            review_run_id=int(second_run["id"]),
            github=github,
        )

        self.assertEqual(first_publish["comment_id"], second_publish["comment_id"])
        self.assertEqual(len(github.created), 1)
        self.assertEqual(len(github.updated), 1)
        previous = self.connection.execute(
            "SELECT delivery_status, superseded_at FROM review_publications WHERE id = ?",
            (int(first["publication_id"]),),
        ).fetchone()
        self.assertEqual(previous["delivery_status"], "posted")
        self.assertIsNotNone(previous["superseded_at"])

    def test_stale_base_fails_without_superseding_previous_posted_review(self) -> None:
        first_run, first = self.generate()
        review_publisher.publish_review(
            self.connection,
            publication_id=int(first["publication_id"]),
            review_run_id=int(first_run["id"]),
            github=FakeGitHub(),
        )
        second_run, second = self.generate(head_sha="c" * 40)

        result = review_publisher.publish_review(
            self.connection,
            publication_id=int(second["publication_id"]),
            review_run_id=int(second_run["id"]),
            github=FakeGitHub(base_sha="e" * 40, head_sha="c" * 40),
        )

        self.assertFalse(result["published"])
        self.assertEqual(result["delivery_status"], "stale")
        self.assertEqual(result["failure_code"], "base_sha_changed")
        current = memory_db.resolve_current_review_state(
            self.connection, repository="eneo/platform", pr_number=17
        )
        self.assertEqual(current.publication_id, int(first["publication_id"]))

    def test_oversize_body_fails_without_truncating_or_superseding(self) -> None:
        run, publication = self.generate()

        result = review_publisher.publish_review(
            self.connection,
            publication_id=int(publication["publication_id"]),
            review_run_id=int(run["id"]),
            github=FakeGitHub(),
            max_comment_bytes=1000,
        )

        self.assertFalse(result["published"])
        self.assertEqual(result["failure_code"], "body_too_large")
        row = self.connection.execute(
            "SELECT delivery_status, rendered_markdown FROM review_publications WHERE id = ?",
            (int(publication["publication_id"]),),
        ).fetchone()
        self.assertEqual(row["delivery_status"], "publish_failed")
        self.assertEqual(row["rendered_markdown"], publication["markdown"])

    def test_hash_mismatch_prevents_publication(self) -> None:
        run, publication = self.generate()
        self.connection.execute(
            "UPDATE review_publications SET rendered_markdown = rendered_markdown || 'tampered'",
        )
        self.connection.commit()

        with self.assertRaisesRegex(memory_db.ReviewMemoryError, "hash mismatch"):
            review_publisher.publish_review(
                self.connection,
                publication_id=int(publication["publication_id"]),
                review_run_id=int(run["id"]),
                github=FakeGitHub(),
            )

    def test_existing_marker_recovers_lost_create_response(self) -> None:
        run, publication = self.generate()
        marker = memory_db.publication_marker(str(publication["publication_key"]))
        github = FakeGitHub(
            comments=[
                review_publisher.IssueComment(
                    comment_id=88,
                    body=f"{publication['markdown']}\n<!-- {marker} -->",
                )
            ]
        )

        result = review_publisher.publish_review(
            self.connection,
            publication_id=int(publication["publication_id"]),
            review_run_id=int(run["id"]),
            github=github,
        )

        self.assertTrue(result["published"])
        self.assertTrue(result["recovered"])
        self.assertEqual(result["comment_id"], 88)
        self.assertEqual(github.created, [])


if __name__ == "__main__":
    unittest.main()
