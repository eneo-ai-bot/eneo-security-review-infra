"""Eneo bounded pull-request context and review-memory plugin."""

from __future__ import annotations

from importlib import import_module
from typing import Any, Callable, Protocol


class ToolRegistry(Protocol):
    def register_tool(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., str],
    ) -> None: ...


def register(ctx: ToolRegistry) -> None:
    # Keep package import light: static package imports here trip pyright's
    # import-cycle gate because schemas/tools import the memory facade.
    schemas = import_module(f"{__name__}.schemas")
    tools = import_module(f"{__name__}.tools")

    ctx.register_tool(
        name="eneo_pr_overview",
        toolset="eneo_review",
        schema=getattr(schemas, "ENEO_PR_OVERVIEW"),
        handler=getattr(tools, "pr_overview"),
    )
    ctx.register_tool(
        name="eneo_pr_diff",
        toolset="eneo_review",
        schema=getattr(schemas, "ENEO_PR_DIFF"),
        handler=getattr(tools, "pr_diff"),
    )
    ctx.register_tool(
        name="eneo_pr_file",
        toolset="eneo_review",
        schema=getattr(schemas, "ENEO_PR_FILE"),
        handler=getattr(tools, "pr_file"),
    )
    ctx.register_tool(
        name="eneo_review_memory_context",
        toolset="eneo_review",
        schema=getattr(schemas, "ENEO_REVIEW_MEMORY_CONTEXT"),
        handler=getattr(tools, "review_memory_context"),
    )
    ctx.register_tool(
        name="eneo_review_memory_record",
        toolset="eneo_review",
        schema=getattr(schemas, "ENEO_REVIEW_MEMORY_RECORD"),
        handler=getattr(tools, "review_memory_record"),
    )
    ctx.register_tool(
        name="eneo_review_run_start",
        toolset="eneo_review",
        schema=getattr(schemas, "ENEO_REVIEW_RUN_START"),
        handler=getattr(tools, "review_run_start"),
    )
    ctx.register_tool(
        name="eneo_review_finalize",
        toolset="eneo_review",
        schema=getattr(schemas, "ENEO_REVIEW_FINALIZE"),
        handler=getattr(tools, "review_finalize"),
    )
    ctx.register_tool(
        name="eneo_review_run_complete",
        toolset="eneo_review",
        schema=getattr(schemas, "ENEO_REVIEW_RUN_COMPLETE"),
        handler=getattr(tools, "review_run_complete"),
    )
