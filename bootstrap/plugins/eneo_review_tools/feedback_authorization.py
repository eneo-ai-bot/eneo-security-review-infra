"""Deterministic actor authorization for review feedback commands."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any

try:
    from .memory_validation import ReviewMemoryError
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from memory_validation import ReviewMemoryError

FEEDBACK_ALLOWED_ACTOR_IDS_ENV = "ENEO_FEEDBACK_ALLOWED_ACTOR_IDS"

__all__ = (
    "FEEDBACK_ALLOWED_ACTOR_IDS_ENV",
    "AuthorizedFeedbackActor",
    "authorize_feedback_actor",
    "feedback_allowlist_version",
)


@dataclass(frozen=True)
class AuthorizedFeedbackActor:
    actor_user_id: str
    allowlist_version: str


def _parse_actor_allowlist(raw: str) -> frozenset[str]:
    values: set[str] = set()
    for item in re.split(r"[\s,]+", raw.strip()):
        if not item:
            continue
        if not re.fullmatch(r"[1-9][0-9]*", item):
            raise ReviewMemoryError("malformed actor id in feedback allowlist")
        values.add(item)
    return frozenset(values)


def feedback_allowlist_version(actor_ids: frozenset[str]) -> str:
    payload = ",".join(sorted(actor_ids))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def authorize_feedback_actor(
    actor_user_id: Any,
    *,
    allowed_actor_ids: str | None = None,
) -> AuthorizedFeedbackActor | None:
    raw = (
        os.environ.get(FEEDBACK_ALLOWED_ACTOR_IDS_ENV, "")
        if allowed_actor_ids is None
        else allowed_actor_ids
    )
    allowlist = _parse_actor_allowlist(raw)
    if not allowlist:
        return None
    if type(actor_user_id) is not int or actor_user_id < 1:
        return None
    normalized = str(actor_user_id)
    if normalized not in allowlist:
        return None
    return AuthorizedFeedbackActor(
        actor_user_id=normalized,
        allowlist_version=feedback_allowlist_version(allowlist),
    )
