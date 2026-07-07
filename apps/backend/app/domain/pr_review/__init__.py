"""domain/pr_review — inbound free-text PR comment classification + batching.

Tracks one `pr_comments` row per inbound comment yaaos sees; classifies via
`core/llm`, batches classified-and-waiting comments into one run per ticket.
Fix verification is commit-driven, not a separate pipeline — the incremental
review's verdicts resolve/re-flag findings for free.
"""

from app.domain.pr_review.service import (
    evaluate_auto_approval,
    handle_pr_comment,
    list_comments_for_run,
    maybe_start_batch_run,
)
from app.domain.pr_review.types import InboundComment, PRComment

__all__ = [
    "InboundComment",
    "PRComment",
    "evaluate_auto_approval",
    "handle_pr_comment",
    "list_comments_for_run",
    "maybe_start_batch_run",
]
