"""Deterministic GitHub publication for generated Eneo reviews."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol, cast

try:
    from . import memory_publications
    from .memory_validation import ReviewMemoryError
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    import memory_publications  # type: ignore[no-redef]
    from memory_validation import ReviewMemoryError

_API_ROOT = "https://api.github.com"
_MAX_ATTEMPTS = 3
_RETRYABLE_STATUS = frozenset({502, 503, 504})
DEFAULT_MAX_COMMENT_BYTES = 60_000


@dataclass(frozen=True)
class PullRequestState:
    state: str
    draft: bool
    base_sha: str
    head_sha: str


@dataclass(frozen=True)
class IssueComment:
    comment_id: int
    body: str


class GitHubPublicationGateway(Protocol):
    def get_pull_request(self, repository: str, pr_number: int) -> PullRequestState: ...

    def get_issue_comment(
        self, repository: str, comment_id: int
    ) -> IssueComment | None: ...

    def list_issue_comments(
        self, repository: str, issue_number: int
    ) -> list[IssueComment]: ...

    def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> IssueComment: ...

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> IssueComment: ...


class GitHubPublicationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _owner_repo(repository: str) -> str:
    return urllib.parse.quote(repository, safe="/")


def _json_object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubPublicationError(code)
    return cast(dict[str, Any], value)


def _json_list(value: Any, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise GitHubPublicationError(code)
    return cast(list[Any], value)


class GitHubIssueCommentGateway:
    def __init__(self, token: str) -> None:
        token = token.strip()
        if not token:
            raise GitHubPublicationError("missing_publish_token")
        self._token = token

    def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        payload: dict[str, object] | None = None,
        max_bytes: int = 2_000_000,
    ) -> Any:
        if not endpoint.startswith("/") or "//" in endpoint:
            raise GitHubPublicationError("invalid_github_endpoint")
        body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Eneo-Hermes-Review-Publisher/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self._token}",
        }
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{_API_ROOT}{endpoint}", data=body, headers=headers, method=method
        )
        for attempt in range(_MAX_ATTEMPTS):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    data = response.read(max_bytes + 1)
            except urllib.error.HTTPError as exc:
                if exc.code in _RETRYABLE_STATUS and attempt + 1 < _MAX_ATTEMPTS:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if exc.code == 404:
                    raise GitHubPublicationError("github_404") from exc
                if exc.code == 401:
                    raise GitHubPublicationError("github_401") from exc
                if exc.code == 403:
                    raise GitHubPublicationError("github_403") from exc
                raise GitHubPublicationError(f"github_http_{exc.code}") from exc
            except urllib.error.URLError as exc:
                raise GitHubPublicationError("github_unreachable") from exc
            if len(data) > max_bytes:
                raise GitHubPublicationError("github_response_too_large")
            try:
                return json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GitHubPublicationError("github_invalid_json") from exc
        raise GitHubPublicationError("github_unreachable")

    def get_pull_request(self, repository: str, pr_number: int) -> PullRequestState:
        root = _json_object(
            self._request_json("GET", f"/repos/{_owner_repo(repository)}/pulls/{pr_number}"),
            "github_bad_pr_response",
        )
        base = _json_object(root.get("base"), "github_bad_pr_response")
        head = _json_object(root.get("head"), "github_bad_pr_response")
        return PullRequestState(
            state=str(root.get("state", "")),
            draft=bool(root.get("draft")),
            base_sha=str(base.get("sha", "")).lower(),
            head_sha=str(head.get("sha", "")).lower(),
        )

    def get_issue_comment(
        self, repository: str, comment_id: int
    ) -> IssueComment | None:
        try:
            root = _json_object(
                self._request_json(
                    "GET",
                    f"/repos/{_owner_repo(repository)}/issues/comments/{comment_id}",
                ),
                "github_bad_comment_response",
            )
        except GitHubPublicationError as exc:
            if exc.code == "github_404":
                return None
            raise
        return IssueComment(comment_id=int(root.get("id", 0)), body=str(root.get("body", "")))

    def list_issue_comments(
        self, repository: str, issue_number: int
    ) -> list[IssueComment]:
        comments: list[IssueComment] = []
        for page in range(1, 4):
            page_items = _json_list(
                self._request_json(
                    "GET",
                    f"/repos/{_owner_repo(repository)}/issues/{issue_number}/comments"
                    f"?per_page=100&page={page}",
                ),
                "github_bad_comments_response",
            )
            for item in page_items:
                if isinstance(item, dict):
                    comment = cast(Mapping[str, object], item)
                    raw_id = comment.get("id")
                    comment_id = (
                        raw_id
                        if isinstance(raw_id, int) and not isinstance(raw_id, bool)
                        else 0
                    )
                    comments.append(
                        IssueComment(
                            comment_id=comment_id,
                            body=str(comment.get("body", "")),
                        )
                    )
            if len(page_items) < 100:
                break
        return comments

    def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> IssueComment:
        root = _json_object(
            self._request_json(
                "PATCH",
                f"/repos/{_owner_repo(repository)}/issues/comments/{comment_id}",
                payload={"body": body},
            ),
            "github_bad_comment_response",
        )
        return IssueComment(comment_id=int(root.get("id", 0)), body=str(root.get("body", "")))

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> IssueComment:
        root = _json_object(
            self._request_json(
                "POST",
                f"/repos/{_owner_repo(repository)}/issues/{issue_number}/comments",
                payload={"body": body},
            ),
            "github_bad_comment_response",
        )
        return IssueComment(comment_id=int(root.get("id", 0)), body=str(root.get("body", "")))


def _max_comment_bytes() -> int:
    raw = os.environ.get("ENEO_REVIEW_PUBLISH_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_COMMENT_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReviewMemoryError("ENEO_REVIEW_PUBLISH_MAX_BYTES must be an integer") from exc
    return max(1_000, min(value, 65_000))


def _default_gateway() -> GitHubIssueCommentGateway:
    return GitHubIssueCommentGateway(
        os.environ.get("ENEO_REVIEW_PUBLISH_GH_TOKEN", "").strip()
    )


def _verify_pr_target(
    publication: memory_publications.PublicationForPosting,
    pull: PullRequestState,
) -> str | None:
    if pull.state != "open":
        return "pr_not_open"
    if pull.draft:
        return "pr_is_draft"
    if not publication["base_sha"]:
        return "missing_base_sha"
    if pull.base_sha != publication["base_sha"]:
        return "base_sha_changed"
    if pull.head_sha != publication["head_sha"]:
        return "head_sha_changed"
    return None


def _find_comment_with_marker(
    comments: list[IssueComment], marker: str
) -> IssueComment | None:
    for comment in reversed(comments):
        if marker in comment.body:
            return comment
    return None


def _find_latest_canonical_comment(
    comments: list[IssueComment],
) -> IssueComment | None:
    for comment in reversed(comments):
        if "eneo-review:canonical publication=" in comment.body:
            return comment
    return None


def publish_review(
    connection: sqlite3.Connection,
    *,
    publication_id: int,
    review_run_id: int,
    github: GitHubPublicationGateway | None = None,
    max_comment_bytes: int | None = None,
) -> dict[str, object]:
    publication = memory_publications.claim_publication_for_posting(
        connection, publication_id=publication_id, review_run_id=review_run_id
    )
    if publication["delivery_status"] == "posted":
        return {
            "published": True,
            "publication_id": publication["publication_id"],
            "comment_id": publication["comment_id"],
            "delivery_status": "posted",
            "idempotent": True,
        }

    body = publication["rendered_markdown"]
    body_bytes = len(body.encode("utf-8"))
    budget = max_comment_bytes if max_comment_bytes is not None else _max_comment_bytes()
    if body_bytes > budget:
        memory_publications.mark_publication_failed(
            connection,
            publication_id=publication_id,
            review_run_id=review_run_id,
            failure_code="body_too_large",
        )
        return {
            "published": False,
            "publication_id": publication_id,
            "delivery_status": "publish_failed",
            "failure_code": "body_too_large",
            "body_bytes": body_bytes,
            "max_comment_bytes": budget,
        }

    marker = memory_publications.publication_marker(publication["publication_key"])
    try:
        github = github or _default_gateway()
        stale_code = _verify_pr_target(
            publication,
            github.get_pull_request(publication["repository"], publication["pr_number"]),
        )
        if stale_code:
            memory_publications.mark_publication_failed(
                connection,
                publication_id=publication_id,
                review_run_id=review_run_id,
                failure_code=stale_code,
                status="stale",
            )
            return {
                "published": False,
                "publication_id": publication_id,
                "delivery_status": "stale",
                "failure_code": stale_code,
            }

        comments = github.list_issue_comments(publication["repository"], publication["pr_number"])
        current = _find_comment_with_marker(comments, marker)
        if current is not None:
            if current.body != body:
                current = github.update_issue_comment(
                    publication["repository"], current.comment_id, body
                )
            posted = memory_publications.mark_publication_posted(
                connection,
                publication_id=publication_id,
                review_run_id=review_run_id,
                comment_id=current.comment_id,
            )
            return {
                "published": True,
                "publication_id": publication_id,
                "comment_id": current.comment_id,
                "delivery_status": posted["delivery_status"],
                "recovered": True,
            }

        target = None
        if publication["previous_comment_id"]:
            target = github.get_issue_comment(
                publication["repository"], publication["previous_comment_id"]
            )
            if target is not None and "eneo-review:canonical publication=" not in target.body:
                target = None
        if target is None:
            target = _find_latest_canonical_comment(comments)

        if target is not None:
            comment = (
                target
                if target.body == body
                else github.update_issue_comment(
                    publication["repository"], target.comment_id, body
                )
            )
        else:
            comment = github.create_issue_comment(
                publication["repository"], publication["pr_number"], body
            )

        posted = memory_publications.mark_publication_posted(
            connection,
            publication_id=publication_id,
            review_run_id=review_run_id,
            comment_id=comment.comment_id,
        )
        return {
            "published": True,
            "publication_id": publication_id,
            "comment_id": comment.comment_id,
            "delivery_status": posted["delivery_status"],
            "recovered": False,
        }
    except GitHubPublicationError as exc:
        memory_publications.mark_publication_failed(
            connection,
            publication_id=publication_id,
            review_run_id=review_run_id,
            failure_code=exc.code,
        )
        return {
            "published": False,
            "publication_id": publication_id,
            "delivery_status": "publish_failed",
            "failure_code": exc.code,
        }
