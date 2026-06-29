"""Canonical run-level failure codes — one source of truth for `review_runs.failure_code`.

These are stable, machine-grep-able codes written to the durable run row and surfaced by
the operator CLI (`eneo-review-memory runs --failed`). Keeping them here stops the codes
from drifting across the lifecycle, delivery, and reaper modules that set them.
"""

from __future__ import annotations

from typing import Final

# A review run was abandoned/failed without a more specific code (best-effort failer).
REVIEW_FAILED: Final = "review_failed"
# The reaper failed a run whose heartbeat stopped past the stale cutoff.
STALE_TIMEOUT: Final = "stale_timeout"
# A forced re-review superseded an in-flight run for the same PR.
SUPERSEDED_BY_FORCE: Final = "superseded_by_force"
# A duplicate active run was retired during a schema/lifecycle migration.
SUPERSEDED_DUPLICATE_MIGRATION: Final = "superseded_duplicate_migration"
# review_deliver raised a known ToolInputError/ReviewMemoryError before publishing.
REVIEW_DELIVER_ERROR: Final = "review_deliver_error"
# review_deliver raised an unexpected error before publishing.
UNEXPECTED_REVIEW_DELIVER_FAILURE: Final = "unexpected_review_deliver_failure"

# The complete set, for validation/telemetry callers that want to enumerate codes.
ALL: Final = frozenset(
    {
        REVIEW_FAILED,
        STALE_TIMEOUT,
        SUPERSEDED_BY_FORCE,
        SUPERSEDED_DUPLICATE_MIGRATION,
        REVIEW_DELIVER_ERROR,
        UNEXPECTED_REVIEW_DELIVER_FAILURE,
    }
)
