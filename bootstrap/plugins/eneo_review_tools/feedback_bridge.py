"""Deterministic GitHub feedback bridge core."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
import hashlib
import hmac
import json
import os
import sqlite3
from typing import Literal, Protocol, cast
import urllib.error
import urllib.parse
import urllib.request

try:
    from .feedback_authorization import parse_feedback_actor_allowlist
    from .feedback_contract import usage_lines
    from .review_identity import (
        FEEDBACK_COMMAND_NOT_RECOGNIZED,
        FEEDBACK_NO_CURRENT_REVIEW,
        FEEDBACK_NOT_CURRENT_REVIEW,
        FEEDBACK_STALE_CONTEXT,
        FEEDBACK_UNSUPPORTED_COMMAND,
    )
    from .memory_feedback import (
        FeedbackStatus,
        feedback_event,
        record_review_feedback_comment,
    )
    from .memory_schema import connect_existing, verify_database_ready
    from .memory_validation import ReviewMemoryError
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from feedback_authorization import parse_feedback_actor_allowlist
    from feedback_contract import usage_lines
    from review_identity import (  # type: ignore[no-redef]
        FEEDBACK_COMMAND_NOT_RECOGNIZED,
        FEEDBACK_NO_CURRENT_REVIEW,
        FEEDBACK_NOT_CURRENT_REVIEW,
        FEEDBACK_STALE_CONTEXT,
        FEEDBACK_UNSUPPORTED_COMMAND,
    )
    from memory_feedback import (
        FeedbackStatus,
        feedback_event,
        record_review_feedback_comment,
    )
    from memory_schema import connect_existing, verify_database_ready
    from memory_validation import ReviewMemoryError

Reaction = Literal["+1", "confused"]

TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
DEFAULT_PATH = "/webhooks/eneo-review-feedback"
DEFAULT_PORT = 8645
MAX_BODY_BYTES = 64 * 1024
GITHUB_API = "https://api.github.com"
FEEDBACK_TOKEN_ENV = "ENEO_FEEDBACK_GH_TOKEN"


class BridgeError(Exception):
    status_code: int = 400


class UnauthorizedFeedback(BridgeError):
    status_code = 200


class GitHubError(BridgeError):
    status_code = 502


class GitHubNotFound(BridgeError):
    status_code = 200


class GitHubClient(Protocol):
    def get_issue_comment(self, repository: str, comment_id: int) -> "IssueComment": ...

    def get_pull_request(self, repository: str, pr_number: int) -> "PullRequest": ...

    def create_issue_comment_reaction(
        self, repository: str, comment_id: int, content: Reaction
    ) -> bool: ...

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> None: ...


JsonObject = dict[str, object]


class IssueComment:
    def __init__(
        self,
        *,
        comment_id: int,
        body: str,
        html_url: str,
        issue_url: str,
        actor_id: int,
        actor_login: str,
        author_association: str,
    ) -> None:
        self.comment_id = comment_id
        self.body = body
        self.html_url = html_url
        self.issue_url = issue_url
        self.actor_id = actor_id
        self.actor_login = actor_login
        self.author_association = author_association


class PullRequest:
    def __init__(self, *, number: int, state: str) -> None:
        self.number = number
        self.state = state


class BridgeConfig:
    def __init__(
        self,
        *,
        secret: str,
        token: str,
        allowed_repositories: frozenset[str],
        allowed_actor_ids: frozenset[str],
        database_path: str | None,
    ) -> None:
        self.secret = secret
        self.token = token
        self.allowed_repositories = allowed_repositories
        self.allowed_actor_ids = allowed_actor_ids
        self.database_path = database_path


class BridgeResponse:
    def __init__(self, *, status: str, public_response: bool = False) -> None:
        self.status = status
        self.public_response = public_response

    def to_json(self) -> bytes:
        return json.dumps(
            {"status": self.status, "public_response": self.public_response},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


def positive_int(value: object, field: str) -> int:
    if type(value) is not int or value < 1:
        raise BridgeError(f"{field} must be a positive integer")
    return value


def json_object(value: object, field: str) -> JsonObject:
    if not isinstance(value, Mapping):
        raise BridgeError(f"{field} must be an object")
    raw = cast(Mapping[object, object], value)
    return {key: item for key, item in raw.items() if isinstance(key, str)}


def repository_name(value: object) -> str:
    text = str(value or "").strip()
    if "/" not in text or text.startswith("/") or text.endswith("/"):
        raise BridgeError("repository must be owner/name")
    return text


def parse_repository_allowlist(raw: str) -> frozenset[str]:
    repositories = {
        repository_name(item).casefold()
        for item in raw.replace("\n", ",").split(",")
        if item.strip()
    }
    if not repositories:
        raise SystemExit("ENEO_ALLOWED_REPOSITORIES is empty; deny by default")
    return frozenset(repositories)


def feedback_github_token() -> str:
    token = os.environ.get(FEEDBACK_TOKEN_ENV, "").strip()
    if token:
        return token
    if os.environ.get("GH_TOKEN", "").strip():
        raise SystemExit(
            f"{FEEDBACK_TOKEN_ENV} is required; legacy GH_TOKEN is intentionally ignored"
        )
    raise SystemExit(f"{FEEDBACK_TOKEN_ENV} is required")


def load_config() -> BridgeConfig:
    secret = os.environ.get("ENEO_FEEDBACK_WEBHOOK_SECRET", "").strip()
    if not secret:
        raise SystemExit("ENEO_FEEDBACK_WEBHOOK_SECRET is required")
    token = feedback_github_token()
    allowed_actor_ids = parse_feedback_actor_allowlist(
        os.environ.get("ENEO_FEEDBACK_ALLOWED_ACTOR_IDS", "")
    )
    if not allowed_actor_ids:
        raise SystemExit("ENEO_FEEDBACK_ALLOWED_ACTOR_IDS is empty; deny by default")
    return BridgeConfig(
        secret=secret,
        token=token,
        allowed_repositories=parse_repository_allowlist(
            os.environ.get("ENEO_ALLOWED_REPOSITORIES", "")
        ),
        allowed_actor_ids=allowed_actor_ids,
        database_path=os.environ.get("ENEO_REVIEW_DB") or None,
    )


def ready_check(config: BridgeConfig) -> dict[str, object]:
    verify_database_ready(config.database_path)
    return {"status": "ready"}


def event_lookup(payload: object) -> tuple[str, int, int]:
    root = json_object(payload, "payload")
    repo = json_object(root.get("repository"), "repository")
    pull = json_object(root.get("pull_request"), "pull_request")
    request = json_object(root.get("request"), "request")
    return (
        repository_name(repo.get("full_name")),
        positive_int(pull.get("number"), "pull_request.number"),
        positive_int(request.get("comment_id"), "request.comment_id"),
    )


def issue_number_from_url(repository: str, issue_url: str) -> int:
    parsed = urllib.parse.urlparse(issue_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 5 or parts[-2] != "issues":
        raise BridgeError("comment does not point at a GitHub issue")
    actual_repository = f"{parts[-4]}/{parts[-3]}"
    if actual_repository.casefold() != repository.casefold():
        raise BridgeError("comment repository does not match the request")
    try:
        return positive_int(int(parts[-1]), "issue number")
    except ValueError as exc:
        raise BridgeError("issue number must be an integer") from exc


def object_id(value: object, field: str) -> int:
    if type(value) is not int:
        raise GitHubError(f"GitHub returned invalid {field}")
    return value


def object_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise GitHubError(f"GitHub returned invalid {field}")
    return value


def verify_signature(body: bytes, signature: str, secret: str) -> bool:
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


class GitHubApiClient:
    def __init__(self, token: str) -> None:
        self._token = token

    def _request_json(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        body: JsonObject | None = None,
    ) -> tuple[object, int]:
        if not endpoint.startswith("/") or "//" in endpoint:
            raise GitHubError("invalid GitHub API endpoint")
        data = None
        if body is not None:
            data = json.dumps(body, separators=(",", ":"), sort_keys=True).encode(
                "utf-8"
            )
        request = urllib.request.Request(
            f"{GITHUB_API}{endpoint}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "Hermes-PR-Review-Feedback-Bridge/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise GitHubError("GitHub response exceeded safe size limit")
                if not raw:
                    return {}, int(response.status)
                return json.loads(raw.decode("utf-8")), int(response.status)
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            if exc.code == 404:
                raise GitHubNotFound("GitHub comment or pull request was not found") from exc
            raise GitHubError(f"GitHub request failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise GitHubError("GitHub could not be reached") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitHubError("GitHub returned invalid JSON") from exc

    def get_issue_comment(self, repository: str, comment_id: int) -> IssueComment:
        owner_repo = urllib.parse.quote(repository, safe="/")
        value, _ = self._request_json(
            f"/repos/{owner_repo}/issues/comments/{comment_id}"
        )
        root = json_object(value, "issue comment")
        user = json_object(root.get("user"), "comment.user")
        return IssueComment(
            comment_id=object_id(root.get("id"), "comment id"),
            body=object_text(root.get("body"), "comment body"),
            html_url=object_text(root.get("html_url"), "comment html_url"),
            issue_url=object_text(root.get("issue_url"), "comment issue_url"),
            actor_id=object_id(user.get("id"), "actor id"),
            actor_login=object_text(user.get("login"), "actor login"),
            author_association=object_text(
                root.get("author_association"), "author association"
            ),
        )

    def get_pull_request(self, repository: str, pr_number: int) -> PullRequest:
        owner_repo = urllib.parse.quote(repository, safe="/")
        value, _ = self._request_json(f"/repos/{owner_repo}/pulls/{pr_number}")
        root = json_object(value, "pull request")
        return PullRequest(
            number=object_id(root.get("number"), "pull request number"),
            state=object_text(root.get("state"), "pull request state"),
        )

    def create_issue_comment_reaction(
        self, repository: str, comment_id: int, content: Reaction
    ) -> bool:
        owner_repo = urllib.parse.quote(repository, safe="/")
        _, status = self._request_json(
            f"/repos/{owner_repo}/issues/comments/{comment_id}/reactions",
            method="POST",
            body={"content": content},
        )
        return status == 201

    def create_issue_comment(
        self, repository: str, issue_number: int, body: str
    ) -> None:
        owner_repo = urllib.parse.quote(repository, safe="/")
        self._request_json(
            f"/repos/{owner_repo}/issues/{issue_number}/comments",
            method="POST",
            body={"body": body},
        )


def help_message(detail: str = "") -> str:
    lines = [FEEDBACK_COMMAND_NOT_RECOGNIZED]
    if detail:
        lines.extend(["", detail])
    lines.extend(["", *usage_lines()])
    return "\n".join(lines)


def status_message(status: FeedbackStatus) -> str:
    if status == "no_mapping":
        return FEEDBACK_NO_CURRENT_REVIEW
    if status == "not_current":
        return FEEDBACK_NOT_CURRENT_REVIEW
    if status == "stale":
        return FEEDBACK_STALE_CONTEXT
    if status == "unsupported":
        return FEEDBACK_UNSUPPORTED_COMMAND
    return help_message()


def confirm_error(
    github: GitHubClient,
    *,
    repository: str,
    issue_number: int,
    comment_id: int,
    message: str,
) -> BridgeResponse:
    created = github.create_issue_comment_reaction(
        repository, comment_id, "confused"
    )
    if created:
        github.create_issue_comment(repository, issue_number, message)
    return BridgeResponse(status="error_feedback", public_response=created)


def confirm_success(
    github: GitHubClient,
    *,
    repository: str,
    comment_id: int,
) -> BridgeResponse:
    github.create_issue_comment_reaction(repository, comment_id, "+1")
    return BridgeResponse(status="recorded", public_response=False)


def stored_replay_outcome(
    connection: sqlite3.Connection,
    event_id: str,
) -> FeedbackStatus:
    event = feedback_event(connection, event_id)
    outcome = str(event.get("outcome", "")) if event else ""
    if outcome in {
        "recorded",
        "no_mapping",
        "not_current",
        "stale",
    }:
        return cast(FeedbackStatus, outcome)
    return "ignored"


def confirm_status(
    status: FeedbackStatus,
    *,
    github: GitHubClient,
    repository: str,
    issue_number: int,
    comment_id: int,
) -> BridgeResponse:
    if status == "recorded":
        return confirm_success(github, repository=repository, comment_id=comment_id)
    if status == "unauthorized":
        return BridgeResponse(status="unauthorized", public_response=False)
    return confirm_error(
        github,
        repository=repository,
        issue_number=issue_number,
        comment_id=comment_id,
        message=status_message(status),
    )


def process_feedback(
    *,
    payload: object,
    config: BridgeConfig,
    github: GitHubClient,
) -> BridgeResponse:
    repository, pr_number, comment_id = event_lookup(payload)
    if repository.casefold() not in config.allowed_repositories:
        raise UnauthorizedFeedback("repository is not allowlisted")

    comment = github.get_issue_comment(repository, comment_id)
    if comment.comment_id != comment_id:
        raise BridgeError("GitHub returned the wrong comment")
    issue_number = issue_number_from_url(repository, comment.issue_url)
    if issue_number != pr_number:
        raise BridgeError("comment does not belong to the requested pull request")
    if comment.author_association not in TRUSTED_ASSOCIATIONS:
        raise UnauthorizedFeedback("comment author association is not trusted")

    try:
        pull_request = github.get_pull_request(repository, pr_number)
    except GitHubNotFound:
        return confirm_error(
            github,
            repository=repository,
            issue_number=issue_number,
            comment_id=comment_id,
            message="Feedback can only be recorded on an open pull request.",
        )
    if pull_request.number != pr_number:
        raise BridgeError("GitHub returned the wrong pull request")
    if pull_request.state != "open":
        return confirm_error(
            github,
            repository=repository,
            issue_number=issue_number,
            comment_id=comment_id,
            message="Feedback can only be recorded while the pull request is open.",
        )

    event_id = f"github:issue-comment:{comment_id}"
    with closing(connect_existing(config.database_path)) as connection:
        try:
            result = record_review_feedback_comment(
                connection,
                event_id=event_id,
                repository=repository,
                pr_number=pr_number,
                body=comment.body,
                actor_user_id=comment.actor_id,
                actor_login=comment.actor_login,
                author_association=comment.author_association,
                source_comment_id=comment.comment_id,
                source_comment_url=comment.html_url,
                allowed_actor_ids=config.allowed_actor_ids,
            )
        except ReviewMemoryError as exc:
            return confirm_error(
                github,
                repository=repository,
                issue_number=issue_number,
                comment_id=comment_id,
                message=help_message(str(exc)),
            )
        status = result.status
        if status == "replay":
            status = stored_replay_outcome(connection, event_id)

    return confirm_status(
        status,
        github=github,
        repository=repository,
        issue_number=issue_number,
        comment_id=comment_id,
    )


def decode_request_body(body: bytes) -> object:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("invalid JSON payload") from exc


def response_body(status: str, message: str = "") -> bytes:
    return json.dumps(
        {"status": status, "message": message},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
