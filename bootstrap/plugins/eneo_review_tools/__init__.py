"""Eneo bounded pull-request context and review-memory plugin."""

from . import schemas, tools


def register(ctx):
    ctx.register_tool(
        name="eneo_pr_overview",
        toolset="eneo_review",
        schema=schemas.ENEO_PR_OVERVIEW,
        handler=tools.pr_overview,
    )
    ctx.register_tool(
        name="eneo_pr_diff",
        toolset="eneo_review",
        schema=schemas.ENEO_PR_DIFF,
        handler=tools.pr_diff,
    )
    ctx.register_tool(
        name="eneo_pr_file",
        toolset="eneo_review",
        schema=schemas.ENEO_PR_FILE,
        handler=tools.pr_file,
    )
    ctx.register_tool(
        name="eneo_review_memory_context",
        toolset="eneo_review",
        schema=schemas.ENEO_REVIEW_MEMORY_CONTEXT,
        handler=tools.review_memory_context,
    )
    ctx.register_tool(
        name="eneo_review_memory_record",
        toolset="eneo_review",
        schema=schemas.ENEO_REVIEW_MEMORY_RECORD,
        handler=tools.review_memory_record,
    )
