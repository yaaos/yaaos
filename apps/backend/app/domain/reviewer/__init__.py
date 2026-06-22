"""domain/reviewer — workflow-engine reviews + canonical findings.

Entry points:

- `start_pr_review(ticket_id, *, org_id)` — start a `pr_review_v1` workflow
  execution. Called by `core/intake` when a PR becomes review-ready or
  when a `@yaaos review` comment is parsed.
- `cancel_workflows_for_ticket(ticket_id)` — cancel any non-terminal
  workflow_executions rows for the ticket.
- `publish_findings(...)` — convert `ReportedFindingShape`s from the coding-agent
  into persisted `Finding` rows via `domain/reviewer/publish.py`.
"""

from app.domain.reviewer import web  # noqa: F401
from app.domain.reviewer.aggregate import PRReviewAggregate
from app.domain.reviewer.events import (
    DomainEvent,
    FindingRaised,
    ReviewCompleted,
    ReviewFailed,
    ReviewRequested,
    ReviewStarted,
)
from app.domain.reviewer.lock import acquire_pr_lock
from app.domain.reviewer.mcp_wiring import prefix_broken_creds_warning
from app.domain.reviewer.publish import category_prefix, finding_handle, publish_findings
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.review_job import ReviewJob, ReviewJobInput
from app.domain.reviewer.service import (
    FindingView,
    _register_workflows,
    aggregate_findings_by_prs,
    cancel_workflows_for_ticket,
    dispatch_events,
    get_review,
    is_off_topic_message,
    is_yaaos_command,
    list_findings_for_pr,
    list_reviews_for_pr,
    refresh_ticket_findings_summary,
    start_pr_review,
)
from app.domain.reviewer.trigger import (
    Debounce,
    Run,
    Skip,
    SkipReason,
    TriggerDecision,
    TriggerInputs,
    decide_trigger,
    humanize_skip,
)
from app.domain.reviewer.types import (
    CodeReviewResponse,
    Confidence,
    Finding,
    ReportedFindingShape,
    Review,
    ReviewContext,
    ReviewScope,
    ReviewScopeKind,
    ReviewTrigger,
    Severity,
    TicketSnapshot,
)

__all__ = [
    "CodeReviewResponse",
    "Confidence",
    "Debounce",
    "DomainEvent",
    "Finding",
    "FindingRaised",
    "FindingView",
    "PRReviewAggregate",
    "ReportedFindingShape",
    "Review",
    "ReviewCompleted",
    "ReviewContext",
    "ReviewFailed",
    "ReviewJob",
    "ReviewJobInput",
    "ReviewRequested",
    "ReviewScope",
    "ReviewScopeKind",
    "ReviewStarted",
    "ReviewTrigger",
    "Run",
    "Severity",
    "Skip",
    "SkipReason",
    "SqlAlchemyAggregateRepository",
    "TicketSnapshot",
    "TriggerDecision",
    "TriggerInputs",
    "acquire_pr_lock",
    "aggregate_findings_by_prs",
    "cancel_workflows_for_ticket",
    "category_prefix",
    "decide_trigger",
    "dispatch_events",
    "finding_handle",
    "get_review",
    "humanize_skip",
    "is_off_topic_message",
    "is_yaaos_command",
    "list_findings_for_pr",
    "list_reviews_for_pr",
    "prefix_broken_creds_warning",
    "publish_findings",
    "refresh_ticket_findings_summary",
    "start_pr_review",
]


_register_workflows()
