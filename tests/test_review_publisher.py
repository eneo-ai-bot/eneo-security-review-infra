from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

PLUGIN_PARENT = Path(__file__).resolve().parents[1] / "bootstrap" / "plugins"
sys.path.insert(0, str(PLUGIN_PARENT))

from eneo_review_tools import (  # noqa: E402
    memory_db,
    memory_publications,
    review_publisher,
    review_renderer,
)


class FakeHTTPResponse:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._body
        return self._body[:size]

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


class FakeGitHub:
    def __init__(
        self,
        *,
        base_sha: str = "b" * 40,
        head_sha: str = "a" * 40,
        bot_login: str = "eneo-ai-bot",
        comments: list[review_publisher.IssueComment] | None = None,
    ) -> None:
        self.base_sha = base_sha
        self.head_sha = head_sha
        self.bot_login = bot_login
        self.comments = list(comments or [])
        self.created: list[str] = []
        self.updated: list[tuple[int, str]] = []
        self.deleted: list[int] = []
        self.next_comment_id = 1000

    def current_user_login(self) -> str:
        return self.bot_login

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
        self, repository: str, issue_number: int, *, max_pages: int = 3
    ) -> list[review_publisher.IssueComment]:
        del repository, issue_number, max_pages
        return list(self.comments)

    def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> review_publisher.IssueComment:
        del repository
        updated = review_publisher.IssueComment(
            comment_id=comment_id, body=body, author_login=self.bot_login
        )
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
            comment_id=self.next_comment_id,
            body=body,
            author_login=self.bot_login,
        )
        self.comments.append(created)
        self.created.append(body)
        return created

    def delete_issue_comment(self, repository: str, comment_id: int) -> None:
        del repository
        self.comments = [
            comment for comment in self.comments if comment.comment_id != comment_id
        ]
        self.deleted.append(comment_id)


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

    def test_canonical_publication_marker_keeps_old_comment_bytes(self) -> None:
        publication_key = "sha256:" + ("0" * 64)
        marker = "eneo-review:canonical publication=sha256:" + ("0" * 64)
        html_marker = f"<!-- {marker} -->"

        self.assertEqual(memory_publications.publication_marker(publication_key), marker)
        self.assertEqual(
            memory_publications.publication_marker_html(publication_key),
            html_marker,
        )
        self.assertEqual(
            memory_publications.extract_publication_key(
                f"before\n{html_marker}\nafter"
            ),
            publication_key,
        )
        self.assertIsNone(
            memory_publications.extract_publication_key(
                "<!-- review-agent:canonical publication=sha256:"
                + ("0" * 64)
                + " -->"
            )
        )

    def generate(
        self,
        *,
        base_sha: str = "b" * 40,
        head_sha: str = "a" * 40,
        connection=None,
    ):
        connection = connection or self.connection
        run = memory_db.start_run(
            connection,
            "eneo/platform",
            17,
            base_sha=base_sha,
            head_sha=head_sha,
        )
        memory_db.record_findings(
            connection,
            "eneo/platform",
            17,
            head_sha,
            [self.finding],
            review_run_id=int(run["id"]),
            base_sha=base_sha,
            context_hashes={self.finding["path"]: "d" * 40},
        )
        publication = memory_db.finalize_review(
            connection,
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

    def test_same_snapshot_start_returns_already_reviewed(self) -> None:
        first_run, first = self.generate()
        posted = review_publisher.publish_review(
            self.connection,
            publication_id=int(first["publication_id"]),
            review_run_id=int(first_run["id"]),
            github=FakeGitHub(),
        )

        duplicate = memory_db.start_run(
            self.connection,
            "eneo/platform",
            17,
            base_sha="b" * 40,
            head_sha="a" * 40,
        )

        self.assertEqual(duplicate["status"], "already_reviewed")
        self.assertEqual(duplicate["comment_id"], posted["comment_id"])
        self.assertEqual(duplicate["review_number"], 1)

    def test_failed_publication_does_not_consume_visible_review_number(self) -> None:
        first_run, first = self.generate()
        failed = review_publisher.publish_review(
            self.connection,
            publication_id=int(first["publication_id"]),
            review_run_id=int(first_run["id"]),
            github=FakeGitHub(),
            max_comment_bytes=100,
        )
        self.assertFalse(failed["published"])
        self.assertEqual(first["review_number"], 1)

        second_run, second = self.generate(head_sha="c" * 40)
        github = FakeGitHub(head_sha="c" * 40)
        posted = review_publisher.publish_review(
            self.connection,
            publication_id=int(second["publication_id"]),
            review_run_id=int(second_run["id"]),
            github=github,
        )

        self.assertTrue(posted["published"])
        self.assertEqual(second["review_number"], 1)
        self.assertIn("Review 1", github.created[0])

    def test_publish_creates_new_round_and_marks_previous_historical(self) -> None:
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

        self.assertNotEqual(first_publish["comment_id"], second_publish["comment_id"])
        self.assertEqual(len(github.created), 2)
        self.assertEqual(len(github.updated), 1)
        self.assertEqual(github.updated[0][0], first_publish["comment_id"])
        self.assertIn("Review 1 · Superseded", github.updated[0][1])
        self.assertIn("Superseded by [Review 2]", github.updated[0][1])
        self.assertNotIn("Give feedback on this review", github.updated[0][1])
        previous = self.connection.execute(
            """
            SELECT delivery_status, superseded_at, superseded_by_publication_id,
                   supersession_rendered_at, supersession_failure_code
            FROM review_publications WHERE id = ?
            """,
            (int(first["publication_id"]),),
        ).fetchone()
        self.assertEqual(previous["delivery_status"], "posted")
        self.assertIsNotNone(previous["superseded_at"])
        self.assertEqual(
            previous["superseded_by_publication_id"], int(second["publication_id"])
        )
        self.assertIsNotNone(previous["supersession_rendered_at"])
        self.assertEqual(previous["supersession_failure_code"], "")

    def test_superseded_comment_truncates_to_existing_comment_footprint(self) -> None:
        first_run, first = self.generate()
        github = FakeGitHub()
        first_publish = review_publisher.publish_review(
            self.connection,
            publication_id=int(first["publication_id"]),
            review_run_id=int(first_run["id"]),
            github=github,
        )
        publication_key = str(first["publication_key"])
        blocks = [
            review_renderer.ReviewBlock(
                kind="header",
                markdown=(
                    "## Eneo AI code & security review\n\n"
                    "Stored pre-rename review."
                ),
            )
        ]
        for index in range(10):
            blocks.append(
                review_renderer.ReviewBlock(
                    kind="finding",
                    markdown=(
                        f"### F{index + 1} · High (P1): Dense old finding {index + 1}\n"
                        "`backend/a.py:10` · security\n\n"
                        + ("Dense historical evidence. " * 80)
                    ),
                )
            )
        blocks.append(
            review_renderer.ReviewBlock(
                kind="metadata",
                markdown=f"<!-- {memory_db.publication_marker(publication_key)} -->",
            )
        )
        old_body = review_renderer.review_markdown_from_blocks(tuple(blocks))
        self.connection.execute(
            """
            UPDATE review_publications
            SET rendered_markdown = ?,
                rendered_blocks_json = ?,
                rendered_hash = ?
            WHERE id = ?
            """,
            (
                old_body,
                review_renderer.review_blocks_to_json(tuple(blocks)),
                hashlib.sha256(old_body.encode("utf-8")).hexdigest(),
                int(first["publication_id"]),
            ),
        )
        self.connection.commit()

        second_run, second = self.generate(head_sha="c" * 40)
        github.head_sha = "c" * 40
        second_publish = review_publisher.publish_review(
            self.connection,
            publication_id=int(second["publication_id"]),
            review_run_id=int(second_run["id"]),
            github=github,
            max_comment_bytes=1300,
        )

        self.assertTrue(second_publish["published"])
        self.assertTrue(second_publish["supersession_rendered"])
        updated_old = [
            body
            for comment_id, body in github.updated
            if comment_id == first_publish["comment_id"]
        ][0]
        self.assertLessEqual(len(updated_old.encode("utf-8")), 1300)
        self.assertIn("Historical details were shortened", updated_old)
        self.assertIn("part=1/1", updated_old)
        previous = self.connection.execute(
            """
            SELECT supersession_rendered_at, supersession_failure_code
            FROM review_publications WHERE id = ?
            """,
            (int(first["publication_id"]),),
        ).fetchone()
        self.assertIsNotNone(previous["supersession_rendered_at"])
        self.assertEqual(previous["supersession_failure_code"], "")

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

    def test_tiny_comment_budget_fails_without_truncating_or_superseding(self) -> None:
        run, publication = self.generate()

        result = review_publisher.publish_review(
            self.connection,
            publication_id=int(publication["publication_id"]),
            review_run_id=int(run["id"]),
            github=FakeGitHub(),
            max_comment_bytes=100,
        )

        self.assertFalse(result["published"])
        self.assertEqual(result["failure_code"], "body_too_large")
        row = self.connection.execute(
            "SELECT delivery_status, rendered_markdown FROM review_publications WHERE id = ?",
            (int(publication["publication_id"]),),
        ).fetchone()
        self.assertEqual(row["delivery_status"], "publish_failed")
        self.assertEqual(row["rendered_markdown"], publication["markdown"])

    def test_large_body_posts_deterministic_continuation_comments(self) -> None:
        run, publication = self.generate()
        github = FakeGitHub()

        result = review_publisher.publish_review(
            self.connection,
            publication_id=int(publication["publication_id"]),
            review_run_id=int(run["id"]),
            github=github,
            max_comment_bytes=1300,
        )

        self.assertTrue(result["published"])
        self.assertGreater(result["parts"], 1)
        comment_ids = memory_db.publication_comment_ids(
            self.connection, int(publication["publication_id"])
        )
        self.assertEqual(comment_ids, result["comment_ids"])
        self.assertEqual(comment_ids[0], result["comment_id"])
        for index, body in enumerate(github.created, start=1):
            self.assertLessEqual(len(body.encode("utf-8")), 1300)
            self.assertIn(f"part={index}/{result['parts']}", body)
        self.assertIn("AI code & security review - 1 of", github.created[0])
        self.assertIn("· Review 1", github.created[0])

    def test_split_keeps_findings_and_details_whole(self) -> None:
        publication_key = "sha256:" + ("1" * 64)
        blocks = (
            review_renderer.ReviewBlock(
                kind="header",
                markdown=(
                    "## AI code & security review\n\n"
                    "There are 2 current findings: 2 High (P1)."
                ),
            ),
            review_renderer.ReviewBlock(
                kind="finding",
                markdown=(
                    "### F1 · High (P1): First root cause\n"
                    "`backend/a.py:10` · security\n\n"
                    + ("First finding evidence. " * 20)
                    + "\nF1 body end."
                ),
            ),
            review_renderer.ReviewBlock(
                kind="finding",
                markdown=(
                    "### F2 · High (P1): Second root cause\n"
                    "`backend/b.py:20` · correctness\n\n"
                    + ("Second finding evidence. " * 20)
                    + "\nF2 body end."
                ),
            ),
            review_renderer.ReviewBlock(
                kind="fix_brief",
                markdown=(
                    "<details>\n"
                    "<summary>Copyable fix brief for a coding agent</summary>\n\n"
                    "```text\n"
                    + ("Fix brief line. " * 20)
                    + "\n```\n\n"
                    "</details>"
                ),
            ),
            review_renderer.ReviewBlock(
                kind="metadata",
                markdown=(
                    "<!--\n"
                    "eneo-review:\n"
                    "head=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                    "F1=first\n"
                    "F2=second\n"
                    "-->"
                ),
            ),
            review_renderer.ReviewBlock(
                kind="metadata",
                markdown=memory_publications.publication_marker_html(publication_key),
            ),
        )
        body = review_renderer.review_markdown_from_blocks(blocks)

        parts = review_publisher.split_publication_body(
            body,
            publication_key=publication_key,
            max_comment_bytes=1250,
            rendered_blocks_json=review_renderer.review_blocks_to_json(blocks),
        )

        self.assertGreater(len(parts), 1)
        self.assertTrue(
            any("### F1" in part.body and "F1 body end." in part.body for part in parts)
        )
        self.assertTrue(
            any("### F2" in part.body and "F2 body end." in part.body for part in parts)
        )
        details_parts = [part.body for part in parts if "<details>" in part.body]
        self.assertEqual(len(details_parts), 1)
        self.assertIn("</details>", details_parts[0])
        self.assertIn("```text", details_parts[0])
        self.assertIn("\n```\n", details_parts[0])

    def test_split_fails_instead_of_cutting_oversized_finding(self) -> None:
        publication_key = "sha256:" + ("2" * 64)
        body = (
            "## AI code & security review\n\n"
            "There is 1 current finding: 1 High (P1).\n\n"
            "### F1 · High (P1): Oversized root cause\n"
            "`backend/a.py:10` · security\n\n"
            + ("A" * 2000)
            + "\n\n"
            + memory_publications.publication_marker_html(publication_key)
            + "\n"
        )

        with self.assertRaisesRegex(
            review_publisher.GitHubPublicationError, "body_too_large"
        ):
            review_publisher.split_publication_body(
                body,
                publication_key=publication_key,
                max_comment_bytes=1250,
            )

    def test_new_round_does_not_reuse_or_delete_previous_parts(self) -> None:
        first_run, first = self.generate()
        github = FakeGitHub()
        first_result = review_publisher.publish_review(
            self.connection,
            publication_id=int(first["publication_id"]),
            review_run_id=int(first_run["id"]),
            github=github,
            max_comment_bytes=1300,
        )
        self.assertGreater(first_result["parts"], 1)
        first_comment_ids = list(first_result["comment_ids"])

        second_run, second = self.generate(head_sha="c" * 40)
        github.head_sha = "c" * 40
        second_result = review_publisher.publish_review(
            self.connection,
            publication_id=int(second["publication_id"]),
            review_run_id=int(second_run["id"]),
            github=github,
            max_comment_bytes=60000,
        )

        self.assertTrue(second_result["published"])
        self.assertEqual(second_result["parts"], 1)
        self.assertNotIn(second_result["comment_id"], first_comment_ids)
        self.assertEqual(github.deleted, [])
        self.assertGreaterEqual(len(github.updated), len(first_comment_ids))
        self.assertIn("Superseded", github.updated[0][1])
        self.assertEqual(
            memory_db.publication_comment_ids(
                self.connection, int(second["publication_id"])
            ),
            [second_result["comment_id"]],
        )

    def test_retry_with_larger_budget_deletes_stale_current_parts(self) -> None:
        run, publication = self.generate()
        split_parts = review_publisher.split_publication_body(
            str(publication["markdown"]),
            publication_key=str(publication["publication_key"]),
            max_comment_bytes=1300,
            rendered_blocks_json=str(publication["rendered_blocks_json"]),
        )
        self.assertGreater(len(split_parts), 1)
        original_ids = [900 + part.part_number for part in split_parts]
        github = FakeGitHub(
            comments=[
                review_publisher.IssueComment(
                    comment_id=comment_id,
                    body=part.body,
                    author_login="eneo-ai-bot",
                )
                for comment_id, part in zip(original_ids, split_parts, strict=True)
            ]
        )

        result = review_publisher.publish_review(
            self.connection,
            publication_id=int(publication["publication_id"]),
            review_run_id=int(run["id"]),
            github=github,
            max_comment_bytes=60000,
        )

        self.assertTrue(result["published"])
        self.assertTrue(result["recovered"])
        self.assertEqual(result["parts"], 1)
        self.assertEqual(result["comment_id"], original_ids[0])
        self.assertEqual(github.deleted, original_ids[1:])

    def test_stateless_publish_does_not_fallback_to_previous_parts(self) -> None:
        first_run, first = self.generate()
        github = FakeGitHub()
        first_result = review_publisher.publish_review(
            self.connection,
            publication_id=int(first["publication_id"]),
            review_run_id=int(first_run["id"]),
            github=github,
            max_comment_bytes=1300,
        )
        first_comment_ids = list(first_result["comment_ids"])
        self.assertGreater(len(first_comment_ids), 1)

        fresh_connection = memory_db.connect(str(Path(self.temp.name) / "fresh.sqlite3"))
        try:
            second_run, second = self.generate(
                head_sha="c" * 40,
                connection=fresh_connection,
            )
            github.head_sha = "c" * 40
            second_result = review_publisher.publish_review(
                fresh_connection,
                publication_id=int(second["publication_id"]),
                review_run_id=int(second_run["id"]),
                github=github,
                max_comment_bytes=60000,
            )
        finally:
            fresh_connection.close()

        self.assertTrue(second_result["published"])
        self.assertEqual(second_result["parts"], 1)
        self.assertNotIn(second_result["comment_id"], first_comment_ids)
        self.assertEqual(github.deleted, [])
        self.assertEqual(github.updated, [])

    def test_non_bot_marker_comment_is_not_reused(self) -> None:
        run, publication = self.generate()
        github = FakeGitHub(
            comments=[
                review_publisher.IssueComment(
                    comment_id=77,
                    body=str(publication["markdown"]),
                    author_login="alice",
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
        self.assertNotEqual(result["comment_id"], 77)
        self.assertEqual(github.updated, [])
        self.assertEqual(github.deleted, [])
        self.assertEqual(len(github.created), 1)

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
                    author_login="eneo-ai-bot",
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

    def test_http_gateway_uses_read_token_for_pr_and_write_token_for_comment(
        self,
    ) -> None:
        seen: list[tuple[str, str, str]] = []

        def fake_urlopen(
            request: urllib.request.Request, timeout: int
        ) -> FakeHTTPResponse:
            del timeout
            seen.append(
                (
                    request.get_method(),
                    request.full_url,
                    request.get_header("Authorization", ""),
                )
            )
            if request.get_method() == "GET":
                return FakeHTTPResponse(
                    {
                        "state": "open",
                        "draft": False,
                        "base": {"sha": "b" * 40},
                        "head": {"sha": "a" * 40},
                    }
                )
            return FakeHTTPResponse(
                {
                    "id": 123,
                    "body": "review",
                    "user": {"login": "eneo-ai-bot"},
                }
            )

        gateway = review_publisher.GitHubIssueCommentGateway(
            "write-token", read_token="read-token"
        )
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            gateway.get_pull_request("eneo-ai/eneo", 240)
            gateway.create_issue_comment("eneo-ai/eneo", 240, "review")

        self.assertEqual(seen[0][0], "GET")
        self.assertEqual(seen[0][2], "Bearer read-token")
        self.assertEqual(seen[1][0], "POST")
        self.assertEqual(seen[1][2], "Bearer write-token")

    def test_http_gateway_falls_back_to_write_token_when_read_token_is_forbidden(
        self,
    ) -> None:
        authorizations: list[str] = []

        def fake_urlopen(
            request: urllib.request.Request, timeout: int
        ) -> FakeHTTPResponse:
            del timeout
            authorization = request.get_header("Authorization", "")
            authorizations.append(authorization)
            if authorization == "Bearer read-token":
                raise urllib.error.HTTPError(
                    request.full_url,
                    403,
                    "Resource not accessible by personal access token",
                    {},
                    None,
                )
            return FakeHTTPResponse(
                {
                    "state": "open",
                    "draft": False,
                    "base": {"sha": "b" * 40},
                    "head": {"sha": "a" * 40},
                }
            )

        gateway = review_publisher.GitHubIssueCommentGateway(
            "write-token", read_token="read-token"
        )
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            pull = gateway.get_pull_request("eneo-ai/eneo", 240)

        self.assertEqual(pull.state, "open")
        self.assertEqual(authorizations, ["Bearer read-token", "Bearer write-token"])

    def test_http_gateway_reports_endpoint_specific_write_403(self) -> None:
        def fake_urlopen(
            request: urllib.request.Request, timeout: int
        ) -> FakeHTTPResponse:
            del timeout
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "Resource not accessible by personal access token",
                {},
                None,
            )

        gateway = review_publisher.GitHubIssueCommentGateway("write-token")
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(review_publisher.GitHubPublicationError) as error:
                gateway.create_issue_comment("eneo-ai/eneo", 240, "review")

        self.assertEqual(error.exception.code, "github_403_create_issue_comment")


if __name__ == "__main__":
    unittest.main()
