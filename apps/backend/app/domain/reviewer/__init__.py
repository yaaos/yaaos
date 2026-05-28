"""domain/reviewer — workflow-engine reviews + durable findings.

Entry points:

- `start_pr_review(ticket_id, *, org_id, trigger_reason)` — starts a
  `pr_review_v1` workflow execution via `core/workflow` for the full-review
  path. Intake's pr-ready handler + `/yaaos full review` + the SPA `/rereview`
  endpoint all route through here.
- `start_incremental_review(pr_id, *, new_head_sha, prev_head_sha, org_id)` —
  runs the trigger policy for incremental review on push. On `Run`,
  dispatches an `incremental_review_v1` workflow_execution via
  `core/workflow.engine`; the `IncrementalReview` command body runs the
  full review end-to-end against the engine-provisioned workspace.
  `handle_push` is a backwards-compatible alias.
- `cancel_workflows_for_ticket(ticket_id)` — `workflow.request_cancel` on
  every non-terminal `workflow_executions` row for the ticket.

The `PRReviewAggregate` (`Finding`/`Review`/threads/acks with a state
machine) is the durable layer; helpers like `SqlAlchemyAggregateRepository`,
`acquire_pr_lock`, and the aggregate types are exported for extension by
other modules.
"""

from app.domain.reviewer import web  # noqa: F401
from app.domain.reviewer.aggregate import (
    AdmissionDrop,
    PRReviewAggregate,
    RawFinding,
)
from app.domain.reviewer.events import (
    AgentReplyPosted,
    CommentReplyReceived,
    DomainEvent,
    FindingAcknowledged,
    FindingAnchorUpdated,
    FindingRaised,
    FindingReObserved,
    FindingResolutionDetected,
    FindingStaleDetected,
    FindingStateChanged,
    ReviewCompleted,
    ReviewFailed,
    ReviewRequested,
    ReviewStarted,
    ReviewSuperseded,
)
from app.domain.reviewer.incremental_trigger import handle_push, start_incremental_review
from app.domain.reviewer.llm import (
    ClassifyReplyInput,
    ClassifyReplyOutput,
    classify_reply,
)
from app.domain.reviewer.lock import acquire_pr_lock
from app.domain.reviewer.mcp_wiring import prefix_broken_creds_warning
from app.domain.reviewer.models import ReviewRow
from app.domain.reviewer.replies import handle_developer_reply
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.repository_protocol import AggregateRepository
from app.domain.reviewer.review_job import (
    ReviewJob,
    ReviewJobInput,
)
from app.domain.reviewer.service import (
    VERIFY_ACT_THRESHOLD,
    VERIFY_OBSERVE_THRESHOLD,
    ConversationView,
    FindingView,
    ReplyAction,
    StaleCheckAction,
    ThreadMessageView,
    ThreadView,
    VerifyFixAction,
    aggregate_findings_by_prs,
    all_conversations_view,
    apply_classified_reply,
    apply_stale_check_result,
    apply_verify_fix_result,
    compute_acceptance_rate,
    compute_resolved_without_edit_rate,
    dispatch_events,
    find_pr_id_by_external_comment_id,
    get_org_id_for_review,
    get_review,
    get_thread,
    is_off_topic_message,
    is_yaaos_command,
    list_findings_for_pr,
    list_findings_view,
    list_reviews_for_pr,
    review_summary,
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
    AckKind,
    AcknowledgmentDecision,
    AuthorKind,
    CodeAnchor,
    CommentMessage,
    CommentThread,
    Finding,
    FindingFingerprint,
    FindingObservation,
    FindingState,
    ReplyIntent,
    Review,
    ReviewScope,
    ReviewScopeKind,
    ReviewTrigger,
    Severity,
)

__all__ = [
    "VERIFY_ACT_THRESHOLD",
    "VERIFY_OBSERVE_THRESHOLD",
    "AckKind",
    "AcknowledgmentDecision",
    "AdmissionDrop",
    "AgentReplyPosted",
    "AggregateRepository",
    "AuthorKind",
    "ClassifyReplyInput",
    "ClassifyReplyOutput",
    "CodeAnchor",
    "CommentMessage",
    "CommentReplyReceived",
    "CommentThread",
    "ConversationView",
    "Debounce",
    "DomainEvent",
    "Finding",
    "FindingAcknowledged",
    "FindingAnchorUpdated",
    "FindingFingerprint",
    "FindingObservation",
    "FindingRaised",
    "FindingReObserved",
    "FindingResolutionDetected",
    "FindingStaleDetected",
    "FindingState",
    "FindingStateChanged",
    "FindingView",
    "PRReviewAggregate",
    "RawFinding",
    "ReplyAction",
    "ReplyIntent",
    "Review",
    "ReviewCompleted",
    "ReviewFailed",
    "ReviewJob",
    "ReviewJobInput",
    "ReviewRequested",
    "ReviewRow",
    "ReviewScope",
    "ReviewScopeKind",
    "ReviewStarted",
    "ReviewSuperseded",
    "ReviewTrigger",
    "Run",
    "Severity",
    "Skip",
    "SkipReason",
    "SqlAlchemyAggregateRepository",
    "StaleCheckAction",
    "ThreadMessageView",
    "ThreadView",
    "TriggerDecision",
    "TriggerInputs",
    "VerifyFixAction",
    "acquire_pr_lock",
    "aggregate_findings_by_prs",
    "all_conversations_view",
    "apply_classified_reply",
    "apply_stale_check_result",
    "apply_verify_fix_result",
    "cancel_workflows_for_ticket",
    "classify_reply",
    "compute_acceptance_rate",
    "compute_resolved_without_edit_rate",
    "decide_trigger",
    "dispatch_events",
    "find_pr_id_by_external_comment_id",
    "get_org_id_for_review",
    "get_review",
    "get_thread",
    "handle_developer_reply",
    "handle_push",
    "humanize_skip",
    "is_off_topic_message",
    "is_yaaos_command",
    "list_findings_for_pr",
    "list_findings_view",
    "list_reviews_for_pr",
    "prefix_broken_creds_warning",
    "review_summary",
    "start_incremental_review",
    "start_pr_review",
]


class _TicketWorkflowContextProvider:
    """Bridges core/workspace WorkflowCommands to domain/tickets without
    crossing the core → domain layer boundary at import time. Registered
    by `_register_workflows()` at module import."""

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        from app.domain.tickets import get_workspace_ticket_context  # noqa: PLC0415

        return await get_workspace_ticket_context(ticket_id)


async def cancel_workflows_for_ticket(ticket_id) -> int:  # type: ignore[no-untyped-def]
    """Cancel any non-terminal `workflow_executions` rows for this ticket.

    The engine transitions each affected workflow to `cancelled` at its
    next step boundary.

    Returns the number of workflow rows that were transitioned to
    `cancelled` (or had `cancel_requested` set, depending on engine state).
    """
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
):
    """Start a `pr_review_v1` workflow for a ticket.

    Intake + the /rereview endpoint use this so production has a single
    path into the engine. Returns the workflow_execution_id.

    `trigger_reason` is recorded on the workflow's audit trail; the
    workflow doesn't gate behavior on it — kept for observability +
    audit-log compatibility.
    """
    from uuid import UUID  # noqa: PLC0415

    from app.core.database import session as db_session  # noqa: PLC0415
    from app.core.workflow import get_engine  # noqa: PLC0415
    from app.core.workspace import get_workflow_context_provider  # noqa: PLC0415

    del trigger_reason  # observed via workflow_executions.workflow_name today
    provider = get_workflow_context_provider()
    if provider is None:
        raise RuntimeError("workflow context provider not registered")
    ticket_uuid = ticket_id if isinstance(ticket_id, UUID) else UUID(str(ticket_id))
    ctx = await provider.get_workspace_ticket_context(ticket_uuid)
    if ctx is None:
        raise RuntimeError(f"ticket {ticket_id} not found")
    del org_id  # ticket-context already carries org_id; kept for caller-side clarity
    async with db_session() as s:
        wfx_id = await get_engine().start(
            workflow_name="pr_review_v1",
            ticket_id=str(ticket_uuid),
            workspace_provider="in_memory",
            ticket_payload=dict(ctx.payload),
            session=s,
        )
        await s.commit()
    return wfx_id


def _register_workflows() -> None:
    """Register the five reviewer workflows + their WorkflowCommands +
    the three workspace lifecycle commands against `core/workflow`. Also
    installs the workflow-context provider so `ProvisionWorkspace` can
    read ticket fields. Called at import time; idempotent on re-import
    (tests reset the engine)."""
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
            # Already registered (test reload, double-import). Leave it.
            pass
    for wf in ALL_WORKFLOWS:
        try:
            engine.register_workflow(wf)
        except WorkflowError:
            pass

    register_workflow_context_provider(_TicketWorkflowContextProvider())


_register_workflows()
