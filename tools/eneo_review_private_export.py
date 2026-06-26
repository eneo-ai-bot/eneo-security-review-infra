"""Shared primitives for private bounded review-memory exports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def bounded_text(value: object, maximum: int) -> str:
    normalized = " ".join(("" if value is None else str(value)).split())
    if len(normalized) <= maximum:
        return normalized
    return normalized[: maximum - 3].rstrip() + "..."


def stable_json_hash(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def dumps_private_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"
