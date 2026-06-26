"""Deterministic GitHub publication for generated PR reviews."""

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
from typing import Any, Literal, Protocol, cast

try:
    from . import memory_publications
    from .memory_validation import ReviewMemoryError
    from .review_renderer import (
        ReviewBlock,
        review_blocks_from_json,
        review_markdown_from_blocks,
    )
    from .review_identity import CONTINUATION_LEAD, REVIEW_COMMENT_TITLE
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    import memory_publications  # type: ignore[no-redef]
    from memory_validation import ReviewMemoryError
    from review_renderer import (  # type: ignore[no-redef]
        ReviewBlock,
        review_blocks_from_json,
        review_markdown_from_blocks,
    )
    from review_identity import (  # type: ignore[no-redef]
        CONTINUATION_LEAD,
        REVIEW_COMMENT_TITLE,
    )

_API_ROOT = "https://api.github.com"
_MAX_ATTEMPTS = 3
_RETRYABLE_STATUS = frozenset({502, 503, 504})
_READ_TOKEN_FALLBACK_STATUS = frozenset({401, 403, 404})
DEFAULT_MAX_COMMENT_BYTES = 60_000
_HISTORICAL_TRUNCATION_NOTICE = (
    "_Historical details were shortened to fit GitHub comment limits; "
    "the full review text remains in review memory._\n\n"
)
GitHubAuthPurpose = Literal["read", "write"]


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
class PublicationPart:
    part_number: int
    body: str


class GitHubPublicationGateway(Protocol):
    def current_user_login(self) -> str: ...

    def get_pull_request(self, repository: str, pr_number: int) -> PullRequestState: ...

    def list_issue_comments(
        self, repository: str, issue_number: int
    ) -> list[IssueComment]: ...

    def update_issue_comment(
        self, repository: str, comment_id: int, body: str
    ) -> IssueComment: ...

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> IssueComment: ...

    def delete_issue_comment(self, repository: str, comment_id: int) -> None: ...


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


def _github_failure_code(status: int, operation: str) -> str:
    suffix = f"_{operation}" if operation else ""
    if status in {401, 403, 404}:
        return f"github_{status}{suffix}"
    return f"github_http_{status}{suffix}"


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
        raise GitHubPublicationError("github_unreachable")

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
            "User-Agent": "Eneo-Hermes-Review-Publisher/1.0",
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
                if exc.code in _RETRYABLE_STATUS and attempt + 1 < _MAX_ATTEMPTS:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise GitHubPublicationError(
                    _github_failure_code(exc.code, operation),
                    status=exc.code,
                    operation=operation,
                ) from exc
            except urllib.error.URLError as exc:
                raise GitHubPublicationError("github_unreachable") from exc
            if len(data) > max_bytes:
                raise GitHubPublicationError("github_response_too_large")
            if method == "DELETE" and not data:
                return {}
            try:
                return json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GitHubPublicationError("github_invalid_json") from exc
        raise GitHubPublicationError("github_unreachable")

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
        self, repository: str, issue_number: int
    ) -> list[IssueComment]:
        comments: list[IssueComment] = []
        for page in range(1, 4):
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


def _extract_publication_key(body: str) -> str | None:
    token = "eneo-review:canonical publication="
    token_index = body.find(token)
    if token_index < 0:
        return None
    remainder = body[token_index + len(token) :]
    parts = remainder.split()
    if not parts:
        return None
    key = parts[0].rstrip(" -\"'>")
    return key if key.startswith("sha256:") else None


def _comments_by_author(
    comments: list[IssueComment], author_login: str
) -> list[IssueComment]:
    expected = author_login.casefold()
    return [
        comment
        for comment in comments
        if comment.author_login and comment.author_login.casefold() == expected
    ]


def _canonical_html_marker(publication_key: str) -> str:
    return f"<!-- {memory_publications.publication_marker(publication_key)} -->"


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
    marker = _canonical_html_marker(publication_key)
    if rendered_blocks_json:
        try:
            blocks = review_blocks_from_json(rendered_blocks_json, fallback_markdown=body)
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


def _continuation_prefix(part_number: int, total_parts: int) -> str:
    if part_number == 1:
        return ""
    return (
        f"## {REVIEW_COMMENT_TITLE} - {part_number} of {total_parts}\n\n"
        f"{CONTINUATION_LEAD}\n\n"
    )


def _with_part_heading(body: str, part_number: int, total_parts: int) -> str:
    if part_number != 1:
        return _continuation_prefix(part_number, total_parts) + body
    heading = f"## {REVIEW_COMMENT_TITLE}"
    replacement = f"## {REVIEW_COMMENT_TITLE} - 1 of {total_parts}"
    return body.replace(heading, replacement, 1) if body.startswith(heading) else body


def split_publication_body(
    body: str,
    *,
    publication_key: str,
    max_comment_bytes: int,
    rendered_blocks_json: str = "",
) -> list[PublicationPart]:
    if _body_size(body) <= max_comment_bytes:
        return [PublicationPart(part_number=1, body=body)]

    reserved = _body_size(
        _continuation_prefix(9999, 9999)
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
        part_body = _with_part_heading(chunk.rstrip(), index, total_parts)
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
        if _extract_publication_key(comment.body) != publication_key:
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
        and "eneo-review:canonical publication=" in indexed[comment_id].body
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
                else github.update_issue_comment(repository, target.comment_id, part.body)
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
    marker = _canonical_html_marker(publication["publication_key"])
    blocks_json = publication["rendered_blocks_json"]
    if blocks_json:
        blocks = review_blocks_from_json(
            blocks_json, fallback_markdown=publication["rendered_markdown"]
        )
        content = [
            block.markdown.replace(marker, "").strip()
            for block in blocks
            if block.kind not in {"feedback_help", "metadata"}
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
        code = exc.code if isinstance(exc, GitHubPublicationError) else "supersession_failed"
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
    budget = max_comment_bytes if max_comment_bytes is not None else _max_comment_bytes()
    try:
        parts = split_publication_body(
            body,
            publication_key=publication["publication_key"],
            max_comment_bytes=budget,
            rendered_blocks_json=publication["rendered_blocks_json"],
        )
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
            "body_bytes": _body_size(body),
            "max_comment_bytes": budget,
        }

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

        comments = _comments_by_author(
            github.list_issue_comments(
                publication["repository"], publication["pr_number"]
            ),
            github.current_user_login(),
        )
        current_parts = _publication_comments(comments, publication["publication_key"])
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
            return {
                "published": True,
                "publication_id": publication_id,
                "comment_id": comment_ids[0],
                "comment_ids": comment_ids,
                "parts": len(comment_ids),
                "delivery_status": posted["delivery_status"],
                "recovered": True,
            }

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
        return result
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
