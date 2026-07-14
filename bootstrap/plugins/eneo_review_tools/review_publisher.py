"""Deterministic GitHub publication for generated PR reviews."""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping, Sequence
import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Literal, Protocol, cast

try:
    from . import memory_publications, memory_runs, memory_suggestions
    from .memory_validation import ReviewMemoryError, isoformat, utc_now
    from .review_renderer import (
        ReviewBlock,
        review_blocks_from_json,
        review_blocks_to_json,
        review_markdown_from_blocks,
    )
    from .review_identity import CONTINUATION_LEAD, REVIEW_COMMENT_TITLE
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    import memory_publications  # type: ignore[no-redef]
    import memory_runs  # type: ignore[no-redef]
    import memory_suggestions  # type: ignore[no-redef]
    from memory_validation import ReviewMemoryError, isoformat, utc_now
    from review_renderer import (  # type: ignore[no-redef]
        ReviewBlock,
        review_blocks_from_json,
        review_blocks_to_json,
        review_markdown_from_blocks,
    )
    from review_identity import (  # type: ignore[no-redef]
        CONTINUATION_LEAD,
        REVIEW_COMMENT_TITLE,
    )

_API_ROOT = "https://api.github.com"
_MAX_ATTEMPTS = 3
_RETRYABLE_STATUS = frozenset({502, 503, 504})
_RETRYABLE_METHODS = frozenset({"GET", "PATCH"})
_READ_TOKEN_FALLBACK_STATUS = frozenset({401, 403, 404})
_AMBIGUOUS_REVIEW_CREATE_CODES = frozenset(
    {
        "github_unreachable",
        "github_response_too_large",
        "github_invalid_json",
        "github_bad_review_response",
    }
)
DEFAULT_MAX_COMMENT_BYTES = 60_000
_SUGGESTION_RECOVERY_SCAN_PAGES = 10
_HISTORICAL_TRUNCATION_NOTICE = (
    "_Historical details were shortened to fit GitHub comment limits; "
    "the full review text remains in review memory._\n\n"
)
GitHubAuthPurpose = Literal["read", "write"]
ReviewCommentSide = Literal["LEFT", "RIGHT"]


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
    author_login: str = ""


@dataclass(frozen=True)
class InlineReviewComment:
    """One line or contiguous line range in a pull-request review."""

    path: str
    body: str
    line: int
    side: ReviewCommentSide
    start_line: int | None = None
    start_side: ReviewCommentSide | None = None


@dataclass(frozen=True)
class PullRequestReview:
    review_id: int
    body: str
    author_login: str
    commit_id: str
    state: str


@dataclass(frozen=True)
class PullRequestReviewComment:
    comment_id: int
    review_id: int
    body: str
    author_login: str
    path: str
    commit_id: str
    line: int | None
    side: ReviewCommentSide | None
    start_line: int | None
    start_side: ReviewCommentSide | None


@dataclass(frozen=True)
class PublicationPart:
    part_number: int
    body: str


class GitHubPublicationGateway(Protocol):
    def current_user_login(self) -> str: ...

    def get_pull_request(self, repository: str, pr_number: int) -> PullRequestState: ...

    def list_issue_comments(
        self, repository: str, issue_number: int, *, max_pages: int = 3
    ) -> list[IssueComment]: ...

    def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> IssueComment: ...

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> IssueComment: ...

    def delete_issue_comment(self, repository: str, comment_id: int) -> None: ...

    def create_pull_request_review(
        self,
        repository: str,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: Sequence[InlineReviewComment],
    ) -> PullRequestReview: ...

    def list_pull_request_review_comments(
        self, repository: str, pr_number: int, *, max_pages: int = 3
    ) -> list[PullRequestReviewComment]: ...


class GitHubPublicationError(RuntimeError):
    def __init__(
        self, code: str, *, status: int | None = None, operation: str = ""
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.operation = operation


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


def _positive_int(value: object, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise GitHubPublicationError(code)
    return value


def _nonempty_string(value: object, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GitHubPublicationError(code)
    return value


def _optional_positive_int(value: object, code: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, code)


def _optional_review_side(value: object, code: str) -> ReviewCommentSide | None:
    if value is None:
        return None
    if value not in {"LEFT", "RIGHT"}:
        raise GitHubPublicationError(code)
    return cast(ReviewCommentSide, value)


def _github_failure_code(status: int, operation: str) -> str:
    suffix = f"_{operation}" if operation else ""
    if status in {401, 403, 404}:
        return f"github_{status}{suffix}"
    return f"github_http_{status}{suffix}"


def _ambiguous_review_create_failure(error: GitHubPublicationError) -> bool:
    return error.operation == "create_pull_request_review" and (
        error.status in _RETRYABLE_STATUS
        or error.code in _AMBIGUOUS_REVIEW_CREATE_CODES
    )


def _inline_review_comment_payload(
    comment: InlineReviewComment,
) -> dict[str, object]:
    if not comment.path.strip() or comment.path != comment.path.strip():
        raise GitHubPublicationError("invalid_review_comment_path")
    if not comment.body.strip():
        raise GitHubPublicationError("invalid_review_comment_body")
    line = _positive_int(comment.line, "invalid_review_comment_line")
    if comment.side not in {"LEFT", "RIGHT"}:
        raise GitHubPublicationError("invalid_review_comment_side")

    payload: dict[str, object] = {
        "path": comment.path,
        "body": comment.body,
        "line": line,
        "side": comment.side,
    }
    if comment.start_line is None:
        if comment.start_side is not None:
            raise GitHubPublicationError("invalid_review_comment_range")
        return payload

    start_line = _positive_int(comment.start_line, "invalid_review_comment_start_line")
    if start_line >= line or comment.start_side not in {"LEFT", "RIGHT"}:
        raise GitHubPublicationError("invalid_review_comment_range")
    payload["start_line"] = start_line
    payload["start_side"] = comment.start_side
    return payload


def _review_comment_from_json(value: object) -> PullRequestReviewComment:
    code = "github_bad_review_comments_response"
    root = _json_object(value, code)
    user = _json_object(root.get("user"), code)
    return PullRequestReviewComment(
        comment_id=_positive_int(root.get("id"), code),
        review_id=_positive_int(root.get("pull_request_review_id"), code),
        body=_nonempty_string(root.get("body"), code),
        author_login=_nonempty_string(user.get("login"), code),
        path=_nonempty_string(root.get("path"), code),
        commit_id=_nonempty_string(root.get("commit_id"), code),
        line=_optional_positive_int(root.get("line"), code),
        side=_optional_review_side(root.get("side"), code),
        start_line=_optional_positive_int(root.get("start_line"), code),
        start_side=_optional_review_side(root.get("start_side"), code),
    )


class GitHubIssueCommentGateway:
    def __init__(self, token: str, *, read_token: str = "") -> None:
        token = token.strip()
        if not token:
            raise GitHubPublicationError("missing_publish_token")
        self._token = token
        self._read_token = read_token.strip()
        self._current_user_login: str | None = None

    def _tokens_for(self, auth: GitHubAuthPurpose) -> tuple[str, ...]:
        if auth == "read" and self._read_token and self._read_token != self._token:
            return (self._read_token, self._token)
        return (self._token,)

    def _request_json(
        self,
        method: str,
        endpoint: str,
        *,
        payload: dict[str, object] | None = None,
        max_bytes: int = 2_000_000,
        auth: GitHubAuthPurpose = "write",
        operation: str = "",
    ) -> Any:
        if not endpoint.startswith("/") or "//" in endpoint:
            raise GitHubPublicationError("invalid_github_endpoint")
        tokens = self._tokens_for(auth)
        last_error: GitHubPublicationError | None = None
        for index, token in enumerate(tokens):
            try:
                return self._request_json_with_token(
                    method,
                    endpoint,
                    token=token,
                    payload=payload,
                    max_bytes=max_bytes,
                    operation=operation,
                )
            except GitHubPublicationError as exc:
                last_error = exc
                has_fallback = index + 1 < len(tokens)
                if has_fallback and exc.status in _READ_TOKEN_FALLBACK_STATUS:
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise GitHubPublicationError("github_unreachable", operation=operation)

    def _request_json_with_token(
        self,
        method: str,
        endpoint: str,
        *,
        token: str,
        payload: dict[str, object] | None,
        max_bytes: int,
        operation: str,
    ) -> Any:
        body = None
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Hermes-PR-Review-Publisher/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
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
                if (
                    method in _RETRYABLE_METHODS
                    and exc.code in _RETRYABLE_STATUS
                    and attempt + 1 < _MAX_ATTEMPTS
                ):
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise GitHubPublicationError(
                    _github_failure_code(exc.code, operation),
                    status=exc.code,
                    operation=operation,
                ) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                raise GitHubPublicationError(
                    "github_unreachable", operation=operation
                ) from exc
            if len(data) > max_bytes:
                raise GitHubPublicationError(
                    "github_response_too_large", operation=operation
                )
            if method == "DELETE" and not data:
                return {}
            try:
                return json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GitHubPublicationError(
                    "github_invalid_json", operation=operation
                ) from exc
        raise GitHubPublicationError("github_unreachable", operation=operation)

    def get_pull_request(self, repository: str, pr_number: int) -> PullRequestState:
        root = _json_object(
            self._request_json(
                "GET",
                f"/repos/{_owner_repo(repository)}/pulls/{pr_number}",
                auth="read",
                operation="get_pull_request",
            ),
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

    def current_user_login(self) -> str:
        if self._current_user_login is None:
            root = _json_object(
                self._request_json(
                    "GET",
                    "/user",
                    auth="write",
                    operation="get_authenticated_user",
                ),
                "github_bad_user_response",
            )
            login = str(root.get("login", "")).strip()
            if not login:
                raise GitHubPublicationError("github_bad_user_response")
            self._current_user_login = login
        return self._current_user_login

    def list_issue_comments(
        self, repository: str, issue_number: int, *, max_pages: int = 3
    ) -> list[IssueComment]:
        comments: list[IssueComment] = []
        for page in range(1, max_pages + 1):
            page_items = _json_list(
                self._request_json(
                    "GET",
                    f"/repos/{_owner_repo(repository)}/issues/{issue_number}/comments"
                    f"?per_page=100&page={page}",
                    auth="read",
                    operation="list_issue_comments",
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
                            author_login=str(
                                _json_object(
                                    comment.get("user"), "github_bad_comments_response"
                                ).get("login", "")
                            ),
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
                auth="write",
                operation="update_issue_comment",
            ),
            "github_bad_comment_response",
        )
        user = _json_object(root.get("user"), "github_bad_comment_response")
        return IssueComment(
            comment_id=int(root.get("id", 0)),
            body=str(root.get("body", "")),
            author_login=str(user.get("login", "")),
        )

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> IssueComment:
        root = _json_object(
            self._request_json(
                "POST",
                f"/repos/{_owner_repo(repository)}/issues/{issue_number}/comments",
                payload={"body": body},
                auth="write",
                operation="create_issue_comment",
            ),
            "github_bad_comment_response",
        )
        user = _json_object(root.get("user"), "github_bad_comment_response")
        return IssueComment(
            comment_id=int(root.get("id", 0)),
            body=str(root.get("body", "")),
            author_login=str(user.get("login", "")),
        )

    def delete_issue_comment(self, repository: str, comment_id: int) -> None:
        self._request_json(
            "DELETE",
            f"/repos/{_owner_repo(repository)}/issues/comments/{comment_id}",
            max_bytes=0,
            auth="write",
            operation="delete_issue_comment",
        )

    def create_pull_request_review(
        self,
        repository: str,
        pr_number: int,
        *,
        commit_id: str,
        body: str,
        comments: Sequence[InlineReviewComment],
    ) -> PullRequestReview:
        if not commit_id.strip() or commit_id != commit_id.strip():
            raise GitHubPublicationError("invalid_review_commit_id")
        if not body.strip():
            raise GitHubPublicationError("invalid_review_body")
        if not comments:
            raise GitHubPublicationError("review_comments_required")
        comment_payloads = [
            _inline_review_comment_payload(comment) for comment in comments
        ]
        operation = "create_pull_request_review"
        try:
            root = _json_object(
                self._request_json(
                    "POST",
                    f"/repos/{_owner_repo(repository)}/pulls/{pr_number}/reviews",
                    payload={
                        "commit_id": commit_id,
                        "body": body,
                        "event": "COMMENT",
                        "comments": comment_payloads,
                    },
                    auth="write",
                    operation=operation,
                ),
                "github_bad_review_response",
            )
            user = _json_object(root.get("user"), "github_bad_review_response")
            response_body = _nonempty_string(
                root.get("body"), "github_bad_review_response"
            )
            response_commit_id = _nonempty_string(
                root.get("commit_id"), "github_bad_review_response"
            )
            if (
                response_body != body
                or response_commit_id.casefold() != commit_id.casefold()
            ):
                raise GitHubPublicationError("github_bad_review_response")
            return PullRequestReview(
                review_id=_positive_int(root.get("id"), "github_bad_review_response"),
                body=response_body,
                author_login=_nonempty_string(
                    user.get("login"), "github_bad_review_response"
                ),
                commit_id=response_commit_id,
                state=_nonempty_string(root.get("state"), "github_bad_review_response"),
            )
        except GitHubPublicationError as exc:
            if exc.operation:
                raise
            raise GitHubPublicationError(
                exc.code, status=exc.status, operation=operation
            ) from exc

    def list_pull_request_review_comments(
        self, repository: str, pr_number: int, *, max_pages: int = 3
    ) -> list[PullRequestReviewComment]:
        comments: list[PullRequestReviewComment] = []
        for page in range(1, max_pages + 1):
            page_items = _json_list(
                self._request_json(
                    "GET",
                    f"/repos/{_owner_repo(repository)}/pulls/{pr_number}/comments"
                    f"?per_page=100&page={page}&sort=created&direction=desc",
                    auth="read",
                    operation="list_pull_request_review_comments",
                ),
                "github_bad_review_comments_response",
            )
            comments.extend(_review_comment_from_json(item) for item in page_items)
            if len(page_items) < 100:
                break
        return comments


def _max_comment_bytes() -> int:
    raw = os.environ.get("ENEO_REVIEW_PUBLISH_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_COMMENT_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ReviewMemoryError(
            "ENEO_REVIEW_PUBLISH_MAX_BYTES must be an integer"
        ) from exc
    return max(1_000, min(value, 65_000))


def _default_gateway() -> GitHubIssueCommentGateway:
    return GitHubIssueCommentGateway(
        os.environ.get("ENEO_REVIEW_PUBLISH_GH_TOKEN", "").strip(),
        read_token=os.environ.get("GITHUB_READ_TOKEN", "").strip(),
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


def _comments_by_author(
    comments: list[IssueComment], author_login: str
) -> list[IssueComment]:
    expected = author_login.casefold()
    return [
        comment
        for comment in comments
        if comment.author_login and comment.author_login.casefold() == expected
    ]


def _part_marker(publication_key: str, part_number: int, total_parts: int) -> str:
    return (
        f"{memory_publications.publication_marker(publication_key)} "
        f"part={part_number}/{total_parts}"
    )


def _body_size(body: str) -> int:
    return len(body.encode("utf-8"))


def _pack_blocks(blocks: list[str], max_bytes: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for block in blocks:
        if _body_size(block) > max_bytes:
            raise GitHubPublicationError("body_too_large")
        candidate = current + block
        if current and _body_size(candidate) > max_bytes:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _publication_blocks(
    body: str, *, rendered_blocks_json: str, publication_key: str
) -> list[str]:
    marker = memory_publications.publication_marker_html(publication_key)
    if rendered_blocks_json:
        try:
            blocks = review_blocks_from_json(
                rendered_blocks_json, fallback_markdown=body
            )
        except ValueError as exc:
            raise GitHubPublicationError("rendered_blocks_invalid") from exc
        if review_markdown_from_blocks(blocks) != body:
            raise GitHubPublicationError("rendered_blocks_mismatch")
    else:
        blocks = (ReviewBlock(kind="header", markdown=body),)

    content_blocks: list[str] = []
    for block in blocks:
        markdown = block.markdown.replace(marker, "").strip()
        if markdown:
            content_blocks.append(markdown + "\n")
    return content_blocks


def _publication_heading(body: str) -> str:
    default = f"## {REVIEW_COMMENT_TITLE}"
    first_line = body.splitlines()[0].strip() if body.strip() else ""
    return first_line if first_line.startswith(default) else default


def _part_heading(heading: str, part_number: int, total_parts: int) -> str:
    return f"{heading} · Part {part_number} of {total_parts}"


def _continuation_prefix(heading: str, part_number: int, total_parts: int) -> str:
    if part_number == 1:
        return ""
    return (
        f"{_part_heading(heading, part_number, total_parts)}\n\n{CONTINUATION_LEAD}\n\n"
    )


def _with_part_heading(
    body: str, heading: str, part_number: int, total_parts: int
) -> str:
    if part_number != 1:
        return _continuation_prefix(heading, part_number, total_parts) + body
    replacement = _part_heading(heading, part_number, total_parts)
    return replacement + body[len(heading) :] if body.startswith(heading) else body


def split_publication_body(
    body: str,
    *,
    publication_key: str,
    max_comment_bytes: int,
    rendered_blocks_json: str = "",
) -> list[PublicationPart]:
    if _body_size(body) <= max_comment_bytes:
        return [PublicationPart(part_number=1, body=body)]

    heading = _publication_heading(body)
    reserved = _body_size(
        _continuation_prefix(heading, 9999, 9999)
        + "\n\n<!-- "
        + _part_marker(publication_key, 9999, 9999)
        + " -->\n"
    )
    content_budget = max_comment_bytes - reserved
    if content_budget < 200:
        raise GitHubPublicationError("body_too_large")

    blocks = _publication_blocks(
        body,
        rendered_blocks_json=rendered_blocks_json,
        publication_key=publication_key,
    )
    chunks = _pack_blocks(blocks, content_budget)
    total_parts = len(chunks)
    parts: list[PublicationPart] = []
    for index, chunk in enumerate(chunks, start=1):
        part_body = _with_part_heading(chunk.rstrip(), heading, index, total_parts)
        part_body = f"{part_body.rstrip()}\n\n<!-- {_part_marker(publication_key, index, total_parts)} -->\n"
        if _body_size(part_body) > max_comment_bytes:
            raise GitHubPublicationError("body_too_large")
        parts.append(PublicationPart(part_number=index, body=part_body))
    return parts


def _publication_comments(
    comments: list[IssueComment], publication_key: str
) -> dict[int, IssueComment]:
    found: dict[int, IssueComment] = {}
    for comment in comments:
        if memory_publications.extract_publication_key(comment.body) != publication_key:
            continue
        marker = memory_publications.publication_marker(publication_key)
        marker_index = comment.body.find(marker)
        part_number = 1
        part_token = " part="
        part_index = comment.body.find(part_token, marker_index)
        if part_index >= 0:
            raw_part = comment.body[part_index + len(part_token) :].split("/", 1)[0]
            try:
                part_number = int(raw_part)
            except ValueError:
                part_number = 1
        if part_number >= 1:
            found[part_number] = comment
    return found


def _comments_by_id(
    comments: list[IssueComment], comment_ids: list[int]
) -> list[IssueComment]:
    indexed = {comment.comment_id: comment for comment in comments}
    return [
        indexed[comment_id]
        for comment_id in comment_ids
        if comment_id in indexed
        and memory_publications.extract_publication_key(indexed[comment_id].body)
        is not None
    ]


def _comment_url(repository: str, pr_number: int, comment_id: int) -> str:
    return f"https://github.com/{repository}/pull/{pr_number}#issuecomment-{comment_id}"


def _publish_parts(
    *,
    github: GitHubPublicationGateway,
    repository: str,
    pr_number: int,
    parts: list[PublicationPart],
    existing_parts: dict[int, IssueComment],
) -> list[int]:
    posted_ids: list[int] = []
    for part in parts:
        target = existing_parts.get(part.part_number)
        if target is not None:
            comment = (
                target
                if target.body == part.body
                else github.update_issue_comment(
                    repository, target.comment_id, part.body
                )
            )
        else:
            comment = github.create_issue_comment(repository, pr_number, part.body)
        posted_ids.append(comment.comment_id)

    stale_candidates = [
        comment
        for part_number, comment in existing_parts.items()
        if part_number > len(parts) and comment.comment_id not in posted_ids
    ]
    deleted_ids: set[int] = set()
    for comment in stale_candidates:
        if comment.comment_id in deleted_ids:
            continue
        github.delete_issue_comment(repository, comment.comment_id)
        deleted_ids.add(comment.comment_id)
    return posted_ids


def _historical_content_blocks(
    publication: memory_publications.PublicationForSupersession,
) -> list[str]:
    marker = memory_publications.publication_marker_html(publication["publication_key"])
    blocks_json = publication["rendered_blocks_json"]
    if blocks_json:
        blocks = review_blocks_from_json(
            blocks_json, fallback_markdown=publication["rendered_markdown"]
        )
        content = [
            block.markdown.replace(marker, "").strip()
            for block in blocks
            if block.kind
            not in {"suggestion_help", "fix_brief", "feedback_help", "metadata"}
        ]
    else:
        content = [publication["rendered_markdown"].replace(marker, "").strip()]
    return [f"{block}\n\n" for block in content if block]


def _historical_label(review_number: int | None) -> str:
    return f"Review {review_number}" if review_number is not None else "Previous review"


def _truncate_block_to_budget(block: str, max_bytes: int) -> tuple[str, bool]:
    if _body_size(block) <= max_bytes:
        return block, False
    suffix = "\n\n[truncated]\n\n"
    available = max_bytes - _body_size(suffix)
    if available < 100:
        return "", True
    encoded = block.encode("utf-8")[:available]
    return encoded.decode("utf-8", errors="ignore").rstrip() + suffix, True


def _fit_historical_chunks(
    content_blocks: list[str], *, content_budget: int, max_parts: int
) -> list[str]:
    if max_parts < 1:
        raise GitHubPublicationError("superseded_comment_missing")
    if _body_size(_HISTORICAL_TRUNCATION_NOTICE) > content_budget:
        raise GitHubPublicationError("superseded_body_too_large")

    retained = list(content_blocks)
    truncated = False
    while retained:
        candidate: list[str] = []
        block_truncated = False
        for block in retained:
            clipped, was_truncated = _truncate_block_to_budget(block, content_budget)
            if clipped:
                candidate.append(clipped)
            block_truncated = block_truncated or was_truncated
        if truncated or block_truncated:
            candidate.append(_HISTORICAL_TRUNCATION_NOTICE)
        try:
            chunks = _pack_blocks(candidate, content_budget)
        except GitHubPublicationError:
            chunks = []
        if chunks and len(chunks) <= max_parts:
            return chunks
        retained.pop()
        truncated = True

    return [_HISTORICAL_TRUNCATION_NOTICE]


def _historical_bodies(
    publication: memory_publications.PublicationForSupersession,
    *,
    max_comment_bytes: int,
    target_parts: int,
) -> list[PublicationPart]:
    old_label = _historical_label(publication["review_number"])
    new_label = _historical_label(publication["superseded_by_review_number"])
    new_url = _comment_url(
        publication["repository"],
        publication["pr_number"],
        publication["superseded_by_comment_id"],
    )
    summary = (
        f"{old_label} at `{publication['head_sha'][:8]}` · "
        f"{publication['current_findings_count']} findings"
    )
    content_blocks = _historical_content_blocks(publication)
    if not content_blocks:
        raise GitHubPublicationError("superseded_body_empty")

    reserved_template = (
        f"## {REVIEW_COMMENT_TITLE} · {old_label} · Superseded - 999 of 999\n\n"
        f"> [!NOTE]\n"
        f"> **Superseded by [{new_label}]({new_url}).**\n"
        f"> This review describes commit `{publication['head_sha'][:8]}` and is "
        "retained as historical context.\n\n"
        "<details>\n"
        f"<summary>{summary}</summary>\n\n"
        "</details>\n\n"
        f"<!-- {_part_marker(publication['publication_key'], 999, 999)} -->\n"
    )
    content_budget = max_comment_bytes - _body_size(reserved_template)
    if content_budget < 200:
        raise GitHubPublicationError("superseded_body_too_large")
    chunks = _fit_historical_chunks(
        content_blocks, content_budget=content_budget, max_parts=target_parts
    )
    parts: list[PublicationPart] = []
    for part_number, chunk in enumerate(chunks, start=1):
        heading = f"## {REVIEW_COMMENT_TITLE} · {old_label} · Superseded"
        if target_parts > 1:
            heading = f"{heading} - {part_number} of {target_parts}"
        body = (
            f"{heading}\n\n"
            f"> [!NOTE]\n"
            f"> **Superseded by [{new_label}]({new_url}).**\n"
            f"> This review describes commit `{publication['head_sha'][:8]}` and is "
            "retained as historical context.\n\n"
            "<details>\n"
            f"<summary>{summary}</summary>\n\n"
            f"{chunk.rstrip()}\n\n"
            "</details>\n\n"
            f"<!-- {_part_marker(publication['publication_key'], part_number, target_parts)} -->\n"
        )
        if _body_size(body) > max_comment_bytes:
            raise GitHubPublicationError("superseded_body_too_large")
        parts.append(PublicationPart(part_number=part_number, body=body))
    return parts


def _extra_superseded_body(
    publication: memory_publications.PublicationForSupersession,
    *,
    part_number: int,
    total_parts: int,
) -> str:
    old_label = _historical_label(publication["review_number"])
    new_label = _historical_label(publication["superseded_by_review_number"])
    new_url = _comment_url(
        publication["repository"],
        publication["pr_number"],
        publication["superseded_by_comment_id"],
    )
    return (
        f"## {REVIEW_COMMENT_TITLE} · {old_label} · Superseded\n\n"
        f"> [!NOTE]\n"
        f"> **Superseded by [{new_label}]({new_url}).**\n"
        "> This continuation comment is retained only to preserve the historical "
        "PR timeline.\n\n"
        f"<!-- {_part_marker(publication['publication_key'], part_number, total_parts)} -->\n"
    )


def _mark_supersession_failure(
    connection: sqlite3.Connection,
    publication_id: int,
    code: str,
) -> None:
    try:
        memory_publications.mark_supersession_rendered(
            connection, publication_id=publication_id, failure_code=code
        )
    except ReviewMemoryError:
        pass


def _render_superseded_publication(
    connection: sqlite3.Connection,
    *,
    github: GitHubPublicationGateway,
    comments: list[IssueComment],
    superseding_publication_id: int,
    max_comment_bytes: int,
) -> dict[str, object] | None:
    try:
        publication = memory_publications.publication_for_supersession(
            connection, superseding_publication_id
        )
    except ReviewMemoryError:
        return {
            "supersession_rendered": False,
            "supersession_failure_code": "supersession_lookup_failed",
        }
    if publication is None:
        return None
    targets = _comments_by_id(comments, publication["comment_ids"])
    if len(targets) != len(publication["comment_ids"]):
        _mark_supersession_failure(
            connection, publication["publication_id"], "superseded_comment_missing"
        )
        return {
            "superseded_publication_id": publication["publication_id"],
            "supersession_rendered": False,
            "supersession_failure_code": "superseded_comment_missing",
        }
    try:
        parts = _historical_bodies(
            publication,
            max_comment_bytes=max_comment_bytes,
            target_parts=len(targets),
        )
        if len(parts) > len(targets):
            raise GitHubPublicationError("superseded_body_needs_more_parts")
        for index, target in enumerate(targets):
            if index < len(parts):
                body = parts[index].body
            else:
                body = _extra_superseded_body(
                    publication,
                    part_number=index + 1,
                    total_parts=len(targets),
                )
            if target.body != body:
                github.update_issue_comment(
                    publication["repository"], target.comment_id, body
                )
    except (GitHubPublicationError, ValueError) as exc:
        code = (
            exc.code
            if isinstance(exc, GitHubPublicationError)
            else "supersession_failed"
        )
        _mark_supersession_failure(connection, publication["publication_id"], code)
        return {
            "superseded_publication_id": publication["publication_id"],
            "supersession_rendered": False,
            "supersession_failure_code": code,
        }
    memory_publications.mark_supersession_rendered(
        connection, publication_id=publication["publication_id"]
    )
    return {
        "superseded_publication_id": publication["publication_id"],
        "supersession_rendered": True,
    }


def _suggestion_review_body(review_number: int | None, count: int) -> str:
    label = f"Review {review_number}" if review_number is not None else "this review"
    patch_label = "patch" if count == 1 else "patches"
    return (
        f"## Optional atomic patches · {label}\n\n"
        f"GitHub grouped {count} proposed atomic {patch_label} here so each can be "
        "inspected in context. Apply a patch only after confirming it fits the "
        "surrounding invariants, or add selected suggestions to a batch. Run the "
        "relevant checks, push the result, then post `/review` again. Applying a "
        "patch does not itself mark the finding resolved."
    )


def _inline_suggestion_body(
    suggestion: memory_suggestions.PublicationSuggestion,
) -> str:
    replacement = suggestion["replacement_text"]
    return (
        f"**{suggestion['local_reference']} · Optional atomic patch**\n\n"
        "This is a small patch candidate intended to stand on its own. Confirm it "
        "fits the surrounding invariants and run the relevant checks after applying "
        "it.\n\n"
        "```suggestion\n"
        f"{replacement}\n"
        "```\n\n"
        f"{memory_suggestions.suggestion_marker(suggestion['suggestion_key'])}"
    )


def _inline_suggestion_comment(
    suggestion: memory_suggestions.PublicationSuggestion,
) -> InlineReviewComment:
    multiline = suggestion["start_line"] != suggestion["end_line"]
    return InlineReviewComment(
        path=suggestion["path"],
        body=_inline_suggestion_body(suggestion),
        line=suggestion["end_line"],
        side="RIGHT",
        start_line=suggestion["start_line"] if multiline else None,
        start_side="RIGHT" if multiline else None,
    )


def _recovered_suggestion_comments(
    comments: Sequence[PullRequestReviewComment],
    *,
    author_login: str,
    head_sha: str,
    suggestions: Sequence[memory_suggestions.PublicationSuggestion],
) -> dict[str, PullRequestReviewComment]:
    expected_author = author_login.casefold()
    expected = {item["suggestion_key"]: item for item in suggestions}
    recovered: dict[str, PullRequestReviewComment] = {}
    for comment in comments:
        if comment.author_login.casefold() != expected_author:
            continue
        key = memory_suggestions.extract_suggestion_key(comment.body)
        suggestion = expected.get(key or "")
        if suggestion is None or comment.commit_id.lower() != head_sha.lower():
            continue
        expected_start = (
            suggestion["start_line"]
            if suggestion["start_line"] != suggestion["end_line"]
            else None
        )
        if (
            comment.path != suggestion["path"]
            or comment.line != suggestion["end_line"]
            or comment.side != "RIGHT"
            or comment.start_line != expected_start
            or comment.start_side != ("RIGHT" if expected_start is not None else None)
        ):
            continue
        recovered.setdefault(suggestion["suggestion_key"], comment)
    return recovered


def _publish_suggestions(
    connection: sqlite3.Connection,
    *,
    publication: memory_publications.PublicationForPosting,
    github: GitHubPublicationGateway,
) -> dict[str, object]:
    suggestions = memory_suggestions.suggestions_for_publication(
        connection, publication["publication_id"]
    )[: memory_suggestions.MAX_ATOMIC_SUGGESTIONS_PER_REVIEW]
    if not suggestions:
        return {
            "suggestions_published": False,
            "suggestions_count": 0,
            "suggestion_delivery_status": "none",
        }

    claim = memory_suggestions.claim_suggestions_for_posting(
        connection, publication["publication_id"]
    )
    if claim["suggestion_delivery_status"] == "posted":
        return {
            "suggestions_published": True,
            "suggestions_count": len(suggestions),
            "suggestion_delivery_status": "posted",
            "suggestion_review_id": claim["suggestion_review_id"],
            "suggestions_idempotent": True,
        }
    if not claim["claimed"]:
        return {
            "suggestions_published": False,
            "suggestions_count": len(suggestions),
            "suggestion_delivery_status": claim["suggestion_delivery_status"],
            "suggestion_failure_code": claim["suggestion_failure_code"],
        }

    claim_started_at = claim["suggestion_posting_started_at"]
    try:
        if claim_started_at is None:
            raise ReviewMemoryError("suggestion delivery claim has no lease timestamp")
        author_login = github.current_user_login()
        review_comments = github.list_pull_request_review_comments(
            publication["repository"],
            publication["pr_number"],
            max_pages=_SUGGESTION_RECOVERY_SCAN_PAGES,
        )
        recovered = _recovered_suggestion_comments(
            review_comments,
            author_login=author_login,
            head_sha=publication["head_sha"],
            suggestions=suggestions,
        )
        missing = [
            item for item in suggestions if item["suggestion_key"] not in recovered
        ]
        if missing:
            stale_code = _verify_pr_target(
                publication,
                github.get_pull_request(
                    publication["repository"], publication["pr_number"]
                ),
            )
            if stale_code:
                memory_suggestions.mark_suggestions_failed(
                    connection,
                    publication_id=publication["publication_id"],
                    failure_code=stale_code,
                    stale=True,
                    claim_started_at=claim_started_at,
                )
                return {
                    "suggestions_published": False,
                    "suggestions_count": len(suggestions),
                    "suggestion_delivery_status": "stale",
                    "suggestion_failure_code": stale_code,
                }
            claim_started_at = memory_suggestions.renew_suggestion_claim(
                connection,
                publication_id=publication["publication_id"],
                claim_started_at=claim_started_at,
            )
            try:
                review = github.create_pull_request_review(
                    publication["repository"],
                    publication["pr_number"],
                    commit_id=publication["head_sha"],
                    body=_suggestion_review_body(
                        publication["review_number"], len(missing)
                    ),
                    comments=tuple(
                        _inline_suggestion_comment(item) for item in missing
                    ),
                )
                review_id = review.review_id
            except GitHubPublicationError as exc:
                if _ambiguous_review_create_failure(exc):
                    reconciled_comments = github.list_pull_request_review_comments(
                        publication["repository"],
                        publication["pr_number"],
                        max_pages=_SUGGESTION_RECOVERY_SCAN_PAGES,
                    )
                    reconciled = _recovered_suggestion_comments(
                        reconciled_comments,
                        author_login=author_login,
                        head_sha=publication["head_sha"],
                        suggestions=suggestions,
                    )
                    if len(reconciled) == len(suggestions):
                        review_id = max(
                            comment.review_id for comment in reconciled.values()
                        )
                    else:
                        raise
                else:
                    raise
        else:
            review_id = max(comment.review_id for comment in recovered.values())
        claim_started_at = memory_suggestions.renew_suggestion_claim(
            connection,
            publication_id=publication["publication_id"],
            claim_started_at=claim_started_at,
        )
        memory_suggestions.mark_suggestions_posted(
            connection,
            publication_id=publication["publication_id"],
            review_id=review_id,
            claim_started_at=claim_started_at,
        )
        return {
            "suggestions_published": True,
            "suggestions_count": len(suggestions),
            "suggestion_delivery_status": "posted",
            "suggestion_review_id": review_id,
            "suggestions_recovered": len(recovered),
            "suggestions_created": len(missing),
        }
    except (GitHubPublicationError, ReviewMemoryError) as exc:
        failure_code = (
            exc.code
            if isinstance(exc, GitHubPublicationError)
            else "suggestion_state_failed"
        )
        if claim_started_at is not None:
            try:
                memory_suggestions.mark_suggestions_failed(
                    connection,
                    publication_id=publication["publication_id"],
                    failure_code=failure_code,
                    claim_started_at=claim_started_at,
                )
            except ReviewMemoryError:
                delivery = memory_suggestions.suggestion_delivery_status(
                    connection, publication["publication_id"]
                )
                state = delivery["suggestion_delivery_status"]
                result: dict[str, object] = {
                    "suggestions_published": state == "posted",
                    "suggestions_count": len(suggestions),
                    "suggestion_delivery_status": state,
                }
                if delivery["suggestion_review_id"] is not None:
                    result["suggestion_review_id"] = delivery["suggestion_review_id"]
                if delivery["suggestion_failure_code"]:
                    result["suggestion_failure_code"] = delivery[
                        "suggestion_failure_code"
                    ]
                return result
        return {
            "suggestions_published": False,
            "suggestions_count": len(suggestions),
            "suggestion_delivery_status": "publish_failed",
            "suggestion_failure_code": failure_code,
        }


def _publication_parts_for_suggestion_state(
    publication: memory_publications.PublicationForPosting,
    *,
    suggestions_published: bool,
    max_comment_bytes: int,
) -> list[PublicationPart]:
    body = publication["rendered_markdown"]
    blocks_json = publication["rendered_blocks_json"]
    if blocks_json and not suggestions_published:
        try:
            blocks = review_blocks_from_json(blocks_json, fallback_markdown=body)
        except ValueError as exc:
            raise GitHubPublicationError("rendered_blocks_invalid") from exc
        filtered = tuple(block for block in blocks if block.kind != "suggestion_help")
        body = review_markdown_from_blocks(filtered)
        blocks_json = review_blocks_to_json(filtered)
    return split_publication_body(
        body,
        publication_key=publication["publication_key"],
        max_comment_bytes=max_comment_bytes,
        rendered_blocks_json=blocks_json,
    )


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
    already_posted = publication["delivery_status"] == "posted"
    budget = (
        max_comment_bytes if max_comment_bytes is not None else _max_comment_bytes()
    )

    try:
        github = github or _default_gateway()
        stale_code = _verify_pr_target(
            publication,
            github.get_pull_request(
                publication["repository"], publication["pr_number"]
            ),
        )
        if stale_code:
            suggestion_delivery = memory_suggestions.suggestion_delivery_status(
                connection, publication_id
            )
            if suggestion_delivery["suggestion_delivery_status"] in {
                "pending",
                "posting",
                "posted",
                "publish_failed",
            }:
                memory_suggestions.mark_suggestions_failed(
                    connection,
                    publication_id=publication_id,
                    failure_code=stale_code,
                    stale=True,
                )
            if already_posted:
                return {
                    "published": True,
                    "publication_id": publication["publication_id"],
                    "comment_id": publication["comment_id"],
                    "delivery_status": "posted",
                    "idempotent": True,
                    "suggestions_published": False,
                    "suggestion_delivery_status": "stale",
                    "suggestion_failure_code": stale_code,
                }
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

        suggestion_result = _publish_suggestions(
            connection, publication=publication, github=github
        )
        try:
            parts = _publication_parts_for_suggestion_state(
                publication,
                suggestions_published=bool(
                    suggestion_result.get("suggestions_published", False)
                ),
                max_comment_bytes=budget,
            )
        except GitHubPublicationError as exc:
            if already_posted:
                result: dict[str, object] = {
                    "published": True,
                    "publication_id": publication["publication_id"],
                    "comment_id": publication["comment_id"],
                    "delivery_status": "posted",
                    "idempotent": True,
                    "summary_refresh_failure_code": exc.code,
                }
                result.update(suggestion_result)
                return result
            memory_publications.mark_publication_failed(
                connection,
                publication_id=publication_id,
                review_run_id=review_run_id,
                failure_code=exc.code,
            )
            result = {
                "published": False,
                "publication_id": publication_id,
                "delivery_status": "publish_failed",
                "failure_code": exc.code,
                "body_bytes": _body_size(publication["rendered_markdown"]),
                "max_comment_bytes": budget,
            }
            result.update(suggestion_result)
            return result

        comments = _comments_by_author(
            github.list_issue_comments(
                publication["repository"], publication["pr_number"]
            ),
            github.current_user_login(),
        )
        current_parts = _publication_comments(comments, publication["publication_key"])
        stale_code = _verify_pr_target(
            publication,
            github.get_pull_request(
                publication["repository"], publication["pr_number"]
            ),
        )
        if stale_code:
            suggestion_delivery = memory_suggestions.suggestion_delivery_status(
                connection, publication_id
            )
            if suggestion_delivery["suggestion_delivery_status"] not in {
                "none",
                "stale",
            }:
                memory_suggestions.mark_suggestions_failed(
                    connection,
                    publication_id=publication_id,
                    failure_code=stale_code,
                    stale=True,
                )
            if already_posted:
                return {
                    "published": True,
                    "publication_id": publication["publication_id"],
                    "comment_id": publication["comment_id"],
                    "delivery_status": "posted",
                    "idempotent": True,
                    "suggestions_published": False,
                    "suggestion_delivery_status": "stale",
                    "suggestion_failure_code": stale_code,
                }
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
        if already_posted:
            comment_ids = (
                _publish_parts(
                    github=github,
                    repository=publication["repository"],
                    pr_number=publication["pr_number"],
                    parts=parts,
                    existing_parts=current_parts,
                )
                if current_parts
                else [int(publication["comment_id"] or 0)]
            )
            result = {
                "published": True,
                "publication_id": publication["publication_id"],
                "comment_id": publication["comment_id"],
                "comment_ids": [value for value in comment_ids if value > 0],
                "delivery_status": "posted",
                "idempotent": True,
                "summary_refreshed": bool(current_parts),
            }
            result.update(suggestion_result)
            return result

        if current_parts:
            comment_ids = _publish_parts(
                github=github,
                repository=publication["repository"],
                pr_number=publication["pr_number"],
                parts=parts,
                existing_parts=current_parts,
            )
            posted = memory_publications.mark_publication_posted(
                connection,
                publication_id=publication_id,
                review_run_id=review_run_id,
                comment_id=comment_ids[0],
                comment_ids=comment_ids,
            )
            _cleanup_prior_failure_status(
                connection, github, publication["repository"], publication["pr_number"]
            )
            result = {
                "published": True,
                "publication_id": publication_id,
                "comment_id": comment_ids[0],
                "comment_ids": comment_ids,
                "parts": len(comment_ids),
                "delivery_status": posted["delivery_status"],
                "recovered": True,
            }
            result.update(suggestion_result)
            return result

        comment_ids = _publish_parts(
            github=github,
            repository=publication["repository"],
            pr_number=publication["pr_number"],
            parts=parts,
            existing_parts={},
        )

        posted = memory_publications.mark_publication_posted(
            connection,
            publication_id=publication_id,
            review_run_id=review_run_id,
            comment_id=comment_ids[0],
            comment_ids=comment_ids,
        )
        supersession = _render_superseded_publication(
            connection,
            github=github,
            comments=comments,
            superseding_publication_id=publication_id,
            max_comment_bytes=budget,
        )
        _cleanup_prior_failure_status(
            connection, github, publication["repository"], publication["pr_number"]
        )
        result: dict[str, object] = {
            "published": True,
            "publication_id": publication_id,
            "comment_id": comment_ids[0],
            "comment_ids": comment_ids,
            "parts": len(comment_ids),
            "delivery_status": posted["delivery_status"],
            "recovered": False,
        }
        if supersession is not None:
            result.update(supersession)
        result.update(suggestion_result)
        return result
    except GitHubPublicationError as exc:
        if already_posted:
            return {
                "published": True,
                "publication_id": publication["publication_id"],
                "comment_id": publication["comment_id"],
                "delivery_status": "posted",
                "idempotent": True,
                "summary_refresh_failure_code": exc.code,
            }
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


def _cleanup_prior_failure_status(
    connection: sqlite3.Connection,
    github: GitHubPublicationGateway,
    repository: str,
    pr_number: int,
) -> None:
    """Remove any failure-status comments once a real review has posted for this PR.

    Stored-comment-id first; a comment that is already gone is tolerated (the stored id
    is cleared regardless so it is not retried). A deep marker scan then removes any
    failure-status comments that were posted but never had their id durably stored (e.g.
    a pre-migration/degraded post), which the stored-id sweep cannot see."""
    deleted: set[int] = set()
    for entry in memory_runs.failure_status_comments_for_pr(
        connection, repository, pr_number
    ):
        comment_id = int(entry["comment_id"])
        try:
            github.delete_issue_comment(repository, comment_id)
        except GitHubPublicationError:
            pass
        deleted.add(comment_id)
        memory_runs.clear_failure_status_comment(connection, entry["run_id"])
    for comment in _my_failure_status_comments(github, repository, pr_number):
        if comment.comment_id in deleted:
            continue
        try:
            github.delete_issue_comment(repository, comment.comment_id)
        except GitHubPublicationError:
            pass
        deleted.add(comment.comment_id)


_FAILURE_STATUS_TOKEN = "eneo-review:failure-status"
# The failure-status fallback (no stored comment id) must find a recent comment even on
# very noisy PRs. GitHub returns issue comments oldest-first, so a recently posted
# failure-status comment can sit far beyond the default 300-comment window; scan deeper
# (bounded) on this rare fallback path so we never duplicate or orphan one.
_FAILURE_STATUS_SCAN_PAGES = 50


def _failure_status_marker(run_id: int, head_sha: str) -> str:
    return f"<!-- {_FAILURE_STATUS_TOKEN} run={run_id} head={head_sha} -->"


def _my_failure_status_comments(
    gateway: GitHubPublicationGateway, repository: str, pr_number: int
) -> list[IssueComment]:
    """Deep-scan the bot's own failure-status comments (beyond the 300-comment cap)."""
    mine = _comments_by_author(
        gateway.list_issue_comments(
            repository, pr_number, max_pages=_FAILURE_STATUS_SCAN_PAGES
        ),
        gateway.current_user_login(),
    )
    return [comment for comment in mine if _FAILURE_STATUS_TOKEN in comment.body]


def _failure_status_body(
    run_id: int, head_sha: str, reason: str, failure_code: str
) -> str:
    return (
        f"## {REVIEW_COMMENT_TITLE} — could not be completed\n\n"
        "This automated review did not finish, so no findings were published.\n\n"
        f"- Reason: {reason}\n"
        f"- Status code: `{failure_code}`\n\n"
        "This is an automated status, not a review result; deterministic CI remains "
        "the merge gate. After correcting the cause, post `/review` again as a new "
        "top-level PR comment. If it fails again, share the status code with the "
        "reviewer operator.\n\n"
        f"{_failure_status_marker(run_id, head_sha)}\n"
    )


def _persist_failure_status(
    connection: sqlite3.Connection, run_id: int, comment_id: int
) -> str:
    posted_at = isoformat(utc_now())
    try:
        memory_runs.record_failure_status_comment(
            connection, run_id, comment_id=comment_id, posted_at=posted_at
        )
    except sqlite3.OperationalError:
        # Pre-migration database without the durable columns: the comment is posted;
        # we just cannot store its id. connect() normally migrates before serving.
        pass
    return posted_at


def publish_run_failure_status(
    connection: sqlite3.Connection,
    *,
    run_id: int,
    reason: str,
    failure_code: str,
    github: GitHubPublicationGateway | None = None,
) -> dict[str, object]:
    """Post (or idempotently update) a deterministic 'review could not complete' comment.

    Stored-comment-id first: if the run already has a failure_status_comment_id, PATCH it
    directly without listing comments (robust on PRs with hundreds of comments). Otherwise
    search the bot's own comments for the failure-status marker, else create a new comment.
    Works on terminal status='failed' rows. The body is deterministic code, never model
    text, satisfying the "no model-authored fallback comment" rule.
    """
    run = memory_runs.get_run(connection, run_id)
    if run is None:
        raise ReviewMemoryError("run_id is not a known review run")
    repository = str(run["repository"])
    pr_number = int(run["pr_number"])
    head_sha = str(run.get("head_sha") or "")
    stored_id = run.get("failure_status_comment_id")
    gateway = github or _default_gateway()
    body = _failure_status_body(int(run_id), head_sha, reason, failure_code)

    if isinstance(stored_id, int) and not isinstance(stored_id, bool) and stored_id > 0:
        comment = gateway.update_issue_comment(repository, stored_id, body)
    else:
        marker = _failure_status_marker(int(run_id), head_sha)
        existing = next(
            (
                comment
                for comment in _my_failure_status_comments(
                    gateway, repository, pr_number
                )
                if marker in comment.body
            ),
            None,
        )
        if existing is not None:
            comment = gateway.update_issue_comment(
                repository, existing.comment_id, body
            )
        else:
            comment = gateway.create_issue_comment(repository, pr_number, body)

    posted_at = _persist_failure_status(connection, int(run_id), comment.comment_id)
    return {
        "posted": True,
        "run_id": int(run_id),
        "comment_id": comment.comment_id,
        "failure_code": failure_code,
        "posted_at": posted_at,
    }
