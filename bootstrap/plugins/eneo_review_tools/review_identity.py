"""Visible review identity strings."""

# The publisher parses this heading when splitting and superseding persisted
# review comments, but identity matching uses the hidden publication marker.
# Keep it as a per-bundle constant, not runtime environment.
REVIEW_COMMENT_TITLE = "AI code & security review"
FIX_BRIEF_TASK = (
    "Fix every current finding on the latest PR head with the smallest safe, "
    "behavior-tested change."
)
FIX_BRIEF_PROJECT_CONSTRAINT = (
    "- Reuse the canonical owner or an existing project abstraction; do not create "
    "a parallel path."
)
CONTINUATION_LEAD = "Continued from the previous review comment."
FEEDBACK_COMMAND_NOT_RECOGNIZED = "AI review command not recognized."
FEEDBACK_NO_CURRENT_REVIEW = (
    "I could not find a current AI review for this PR. Run `/review` first, "
    "then comment with the latest F reference."
)
FEEDBACK_NOT_CURRENT_REVIEW = (
    "That finding reference is not current. Use the F number from the latest "
    "AI review comment."
)
FEEDBACK_STALE_CONTEXT = (
    "That finding cannot be recorded because its trusted file context is "
    "missing or stale. Run `/review` again, then retry with the latest F "
    "reference."
)
FEEDBACK_UNSUPPORTED_COMMAND = (
    "That feedback command is not available from PR comments yet. Intentional "
    "design and accepted-risk decisions need the governance CLI."
)
