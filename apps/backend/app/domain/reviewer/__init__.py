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
    aggregate_findings_by_prs,
    dispatch_events,
    get_review,
    is_off_topic_message,
    is_yaaos_command,
    list_findings_for_pr,
    list_reviews_for_pr,
    refresh_ticket_findings_summary,
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
from app.domain.tickets import get_workspace_ticket_context as _get_workspace_ticket_context

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
    """Start a `pr_review_v1` workflow for a ticket.

    Builds a `TicketSnapshot` from the ticket + PR rows and passes it as
    `workflow_input` to the engine — commands receive their data from typed
    inputs populated by the workflow's `inputs_factory` lambdas, not from
    any context-provider lookup.
    """
    from uuid import UUID  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.core.workflow import get_engine  # noqa: PLC0415

    del trigger_reason
    ticket_uuid = ticket_id if isinstance(ticket_id, UUID) else UUID(str(ticket_id))
    ctx = await _get_workspace_ticket_context(ticket_uuid)
    if ctx is None:
        raise RuntimeError(f"ticket {ticket_id} not found")

    # Extract PR fields from the ticket context payload.
    payload = ctx.payload or {}
    snapshot = TicketSnapshot(
        ticket_id=ticket_uuid,
        org_id=ctx.org_id,
        plugin_id=ctx.plugin_id,
        repo_external_id=ctx.repo_external_id,
        pr_id=ctx.pr_id,
        pr_external_id=str(payload.get("pr_external_id") or "") or None,
        head_sha=str(payload.get("head_sha") or "HEAD"),
        base_sha=str(payload.get("base_sha") or "") or None,
        is_draft=bool(payload.get("is_draft", False)),
        is_fork=bool(payload.get("is_fork", False)),
        labels=tuple(str(l) for l in (payload.get("labels") or [])),
        author_login=str(payload.get("author_login") or "") or None,
    )

    async with db_session() as s:
        wfx_id = await get_engine().start(
            workflow_name="pr_review_v1",
            ticket_id=str(ticket_uuid),
            workflow_input=snapshot,
            session=s,
        )
        await s.commit()
    return wfx_id


def _register_workflows() -> None:
    from app.core.workflow import WorkflowError, get_engine  # noqa: PLC0415
    from app.domain.reviewer.workflows import ALL_WORKFLOWS  # noqa: PLC0415

    engine = get_engine()
    # Auto-discovery via register_workflow populates all regular commands from
    # the workflow's steps tuple, and all recovery commands from
    # wf.recovery_commands (including RefreshWorkspaceAuth for pr_review_v1).
    for wf in ALL_WORKFLOWS:
        try:
            engine.register_workflow(wf)
        except WorkflowError:
            pass


_register_workflows()
