"""domain/pr_review — inbound free-text PR comment classification + batching.

Tracks one `pr_comments` row per inbound comment yaaos sees; classifies via
`core/llm`, batches classified-and-waiting comments into one run per ticket.
Fix verification is commit-driven, not a separate pipeline — the incremental
review's verdicts resolve/re-flag findings for free.
"""

from app.domain.pipelines import register_comment_findings_provider, register_run_terminal_hook
from app.domain.pr_review.service import (
    AFTER_RUN_TERMINAL as _AFTER_RUN_TERMINAL,
)
from app.domain.pr_review.service import _comment_finding_ids_for_run as _comment_finding_ids_for_run
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

# Coexistence + cycle-avoidance bridges — see `domain/pipelines.engine`'s
# `register_run_terminal_hook` / `register_comment_findings_provider`
# docstrings.
register_run_terminal_hook(_AFTER_RUN_TERMINAL)
register_comment_findings_provider(_comment_finding_ids_for_run)
