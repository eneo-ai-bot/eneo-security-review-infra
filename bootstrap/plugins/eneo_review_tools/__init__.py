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
        name="eneo_review_begin",
        toolset="eneo_review",
        schema=getattr(schemas, "ENEO_REVIEW_BEGIN"),
        handler=getattr(tools, "review_begin"),
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
        name="eneo_review_deliver",
        toolset="eneo_review",
        schema=getattr(schemas, "ENEO_REVIEW_DELIVER"),
        handler=getattr(tools, "review_deliver"),
    )
