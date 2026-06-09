"""domain/reviewer — workflow-engine reviews + canonical findings.

Entry points:

- `start_pr_review(ticket_id, *, org_id)` — start a `pr_review_v1` workflow
  execution. Called by `domain/intake` when a PR becomes review-ready or
  when a `@yaaos review` comment is parsed.
- `cancel_workflows_for_ticket(ticket_id)` — cancel any non-terminal
  workflow_executions rows for the ticket.
- `publish_findings(...)` — convert `ReportedFinding`s from the coding-agent
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
    aggregate_findings_by_prs,
    dispatch_events,
    get_review,
    is_off_topic_message,
    is_yaaos_command,
    list_findings_for_pr,
    list_reviews_for_pr,
    refresh_ticket_findings_summary,
)
from app.domain.reviewer.terminal_hook import register_reviewer_terminal_hooks
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
    Confidence,
    Finding,
    Review,
    ReviewScope,
    ReviewScopeKind,
    ReviewTrigger,
    Severity,
)
from app.domain.tickets import get_workspace_ticket_context as _get_workspace_ticket_context

__all__ = [
    "Confidence",
    "Debounce",
    "DomainEvent",
    "Finding",
    "FindingRaised",
    "FindingView",
    "PRReviewAggregate",
    "Review",
    "ReviewCompleted",
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
    "TriggerDecision",
    "TriggerInputs",
    "acquire_pr_lock",
    "aggregate_findings_by_prs",
    "cancel_workflows_for_ticket",
    "category_prefix",
    "decide_trigger",
    "dispatch_events",
    "find_pr_id_by_external_comment_id",
    "finding_handle",
    "get_review",
    "handle_developer_reply",
    "humanize_skip",
    "is_off_topic_message",
    "is_yaaos_command",
    "list_findings_for_pr",
    "list_reviews_for_pr",
    "prefix_broken_creds_warning",
    "publish_findings",
    "refresh_ticket_findings_summary",
    "register_reviewer_terminal_hooks",
    "start_incremental_review",
    "start_pr_review",
]


class _TicketWorkflowContextProvider:
    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        return await _get_workspace_ticket_context(ticket_id)


async def cancel_workflows_for_ticket(ticket_id) -> int:  # type: ignore[no-untyped-def]
    """Cancel any non-terminal `workflow_executions` rows for this ticket."""
    from app.core.database import session as db_session  # noqa: PLC0415
    from app.core.workflow import list_active_execution_ids, request_cancel  # noqa: PLC0415

    cancelled = 0
    async with db_session() as s:
        active_ids = await list_active_execution_ids(ticket_id, session=s)
        for wfx_id in active_ids:
            if await request_cancel(str(wfx_id), session=s):
                cancelled += 1
        if cancelled:
            await s.commit()
    return cancelled


async def start_pr_review(
    ticket_id,  # type: ignore[no-untyped-def]
    *,
    org_id,
    trigger_reason: str = "pr_ready",
) -> object:
    """Start a `pr_review_v1` workflow for a ticket."""
    from uuid import UUID  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.core.workflow import get_engine  # noqa: PLC0415
    from app.core.workspace import get_workflow_context_provider  # noqa: PLC0415

    del trigger_reason
    provider = get_workflow_context_provider()
    ticket_uuid = ticket_id if isinstance(ticket_id, UUID) else UUID(str(ticket_id))
    ctx = await provider.get_workspace_ticket_context(ticket_uuid)
    if ctx is None:
        raise RuntimeError(f"ticket {ticket_id} not found")
    del org_id
    async with db_session() as s:
        wfx_id = await get_engine().start(
            workflow_name="pr_review_v1",
            ticket_id=str(ticket_uuid),
            ticket_payload=dict(ctx.payload),
            session=s,
        )
        await s.commit()
    return wfx_id


async def find_pr_id_by_external_comment_id(external_comment_id: str) -> object:
    """No-op stub. Thread-based comment routing is not wired."""
    del external_comment_id
    return None


async def handle_developer_reply(**kwargs: object) -> None:
    """No-op stub. Developer-reply routing is not wired."""
    del kwargs


async def start_incremental_review(
    pr_id: object,
    *,
    new_head_sha: str,
    prev_head_sha: str | None,
    org_id: object,
) -> None:
    """No-op stub. Incremental review is not wired."""
    del pr_id, new_head_sha, prev_head_sha, org_id


def _register_workflows() -> None:
    from app.core.workflow import WorkflowError, get_engine  # noqa: PLC0415
    from app.core.workspace import (  # noqa: PLC0415
        ALL_LIFECYCLE_COMMANDS,
        register_workflow_context_provider,
    )
    from app.domain.reviewer.commands import (  # noqa: PLC0415
        ALL_LOCAL_COMMANDS,
        ALL_WORKSPACE_COMMANDS,
    )
    from app.domain.reviewer.workflows import ALL_WORKFLOWS  # noqa: PLC0415

    engine = get_engine()
    for cmd in (*ALL_LIFECYCLE_COMMANDS, *ALL_WORKSPACE_COMMANDS, *ALL_LOCAL_COMMANDS):
        try:
            engine.register_command(cmd)
        except WorkflowError:
            pass
    for wf in ALL_WORKFLOWS:
        try:
            engine.register_workflow(wf)
        except WorkflowError:
            pass

    register_workflow_context_provider(_TicketWorkflowContextProvider())


_register_workflows()
