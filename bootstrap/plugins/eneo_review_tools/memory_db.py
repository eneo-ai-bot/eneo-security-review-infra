"""Compatibility facade for the split review-memory implementation."""

from __future__ import annotations

try:
    from .feedback_contract import *  # noqa: F403
    from .feedback_authorization import *  # noqa: F403
    from .feedback_commands import *  # noqa: F403
    from .memory_validation import *  # noqa: F403
    from .memory_schema import *  # noqa: F403
    from .memory_identity import *  # noqa: F403
    from .memory_decisions import *  # noqa: F403
    from .memory_findings import *  # noqa: F403
    from .memory_publications import *  # noqa: F403
    from .memory_feedback import *  # noqa: F403
    from .memory_reporting import *  # noqa: F403
    from .memory_runs import *  # noqa: F403
    from .memory_coach import *  # noqa: F403
except ImportError:  # pragma: no cover - supports direct module imports in tests.
    from feedback_contract import *  # noqa: F403
    from feedback_authorization import *  # noqa: F403
    from feedback_commands import *  # noqa: F403
    from memory_validation import *  # noqa: F403
    from memory_schema import *  # noqa: F403
    from memory_identity import *  # noqa: F403
    from memory_decisions import *  # noqa: F403
    from memory_findings import *  # noqa: F403
    from memory_publications import *  # noqa: F403
    from memory_feedback import *  # noqa: F403
    from memory_reporting import *  # noqa: F403
    from memory_runs import *  # noqa: F403
    from memory_coach import *  # noqa: F403
