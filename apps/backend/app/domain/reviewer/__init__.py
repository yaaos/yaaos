"""domain/reviewer — review workflow + per-PR queue + durable findings.

Two generations are live in this module:

- Generation 1: `ReviewJob` row + JSONB findings + `schedule_review` → vcs.post_review.
  Exported as-is so today's intake/UI keep working.
- Generation 2: `PRReviewAggregate` + first-class `Finding`/`Review`/threads/acks
  with a state machine. Not yet reachable from the public schedule_review flow
  — wires in §13 step 7.

External callers depend on the generation-1 surface for now. Generation-2
helpers (`SqlAlchemyAggregateRepository`, `acquire_pr_lock`, aggregate types)
are exported so other modules can extend the durable-findings flow once the
cut-over lands.
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
from app.domain.reviewer.incremental import handle_push
from app.domain.reviewer.llm import (
    ClassifyReplyInput,
    ClassifyReplyOutput,
    classify_reply,
)
from app.domain.reviewer.lock import acquire_pr_lock
from app.domain.reviewer.models import (
    AcknowledgmentDecisionRow,
    CommentMessageRow,
    CommentThreadRow,
    FindingObservationRow,
    FindingRow,
    ReviewRow,
)
from app.domain.reviewer.queue import (
    cancel_pending,
    schedule_review,
    startup_recovery,
)
from app.domain.reviewer.queue_events import (
    ReviewJobStatusChanged,
)
from app.domain.reviewer.replies import handle_developer_reply
from app.domain.reviewer.repository import SqlAlchemyAggregateRepository
from app.domain.reviewer.repository_protocol import AggregateRepository
from app.domain.reviewer.review_job import (
    ReviewJob,
    ReviewJobInput,
)
from app.domain.reviewer.review_job_queries import (
    get_review_job,
    list_in_flight,
    list_review_jobs_for_pr,
    metrics_summary,
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
    all_conversations_view,
    apply_classified_reply,
    apply_stale_check_result,
    apply_verify_fix_result,
    compute_acceptance_rate,
    compute_resolved_without_edit_rate,
    dispatch_events,
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
    "AcknowledgmentDecisionRow",
    "AdmissionDrop",
    "AgentReplyPosted",
    "AggregateRepository",
    "AuthorKind",
    "ClassifyReplyInput",
    "ClassifyReplyOutput",
    "CodeAnchor",
    "CommentMessage",
    "CommentMessageRow",
    "CommentReplyReceived",
    "CommentThread",
    "CommentThreadRow",
    "ConversationView",
    "Debounce",
    "DomainEvent",
    "Finding",
    "FindingAcknowledged",
    "FindingAnchorUpdated",
    "FindingFingerprint",
    "FindingObservation",
    "FindingObservationRow",
    "FindingRaised",
    "FindingReObserved",
    "FindingResolutionDetected",
    "FindingRow",
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
    "ReviewJobStatusChanged",
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
    "all_conversations_view",
    "apply_classified_reply",
    "apply_stale_check_result",
    "apply_verify_fix_result",
    "cancel_pending",
    "classify_reply",
    "compute_acceptance_rate",
    "compute_resolved_without_edit_rate",
    "decide_trigger",
    "dispatch_events",
    "get_review",
    "get_review_job",
    "get_thread",
    "handle_developer_reply",
    "handle_push",
    "humanize_skip",
    "is_off_topic_message",
    "is_yaaos_command",
    "list_findings_for_pr",
    "list_findings_view",
    "list_in_flight",
    "list_review_jobs_for_pr",
    "list_reviews_for_pr",
    "metrics_summary",
    "review_summary",
    "schedule_review",
    "start_pr_review",
    "startup_recovery",
]


class _TicketWorkflowContextProvider:
    """Bridges core/workspace WorkflowCommands to domain/tickets without
    crossing the core → domain layer boundary at import time. Registered
    by `_register_m05_workflows()` at module import."""

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        from app.domain.tickets.service import get_workspace_ticket_context  # noqa: PLC0415

        return await get_workspace_ticket_context(ticket_id)


async def start_pr_review(
    ticket_id,  # type: ignore[no-untyped-def]
    *,
    org_id,
    trigger_reason: str = "pr_ready",
):
    """Start an M05 `pr_review_v1` workflow for a ticket.

    Replaces the legacy `schedule_review` call for the full-review path.
    Intake + the /rereview endpoint use this so production has a single
    path into the M05 engine. Returns the workflow_execution_id.

    `trigger_reason` is recorded on the workflow's audit trail; the M05
    workflow doesn't gate behavior on it (legacy queue.py did) — kept
    for observability + audit-log compatibility.
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


def _register_m05_workflows() -> None:
    """Register the five M05 reviewer workflows + their WorkflowCommands +
    the three workspace lifecycle commands against `core/workflow`. Also
    installs the workflow-context provider so `ProvisionWorkspace` can
    read ticket fields. Called at import time; idempotent on re-import
    (tests reset the engine)."""
    from app.core.workflow import WorkflowError, get_engine  # noqa: PLC0415
    from app.core.workspace import register_workflow_context_provider  # noqa: PLC0415
    from app.core.workspace.commands import ALL_LIFECYCLE_COMMANDS  # noqa: PLC0415
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


_register_m05_workflows()
