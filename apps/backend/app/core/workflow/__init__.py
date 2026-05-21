"""core/workflow — Workflow engine.

Phase 1 (foundations) ships:
- `Workflow`, `Step`, `RetryPolicy` typed data structures.
- `WorkflowCommand` Protocol with three categories (Workspace / Local / HITL).
- `Outcome` with success / failure / hitl_pending shapes + `append_steps`.
- `WorkflowState` enum (`pending|running|awaiting_agent|awaiting_human|done|failed|cancelled`).
- `WorkflowEngine` with workflow + command registries + `start(name, ticket_id, *, session)`.
- Three registered `core/tasks` task names (`workflow.start_step`,
  `workflow.handle_agent_event`, `workflow.route_workflow`) — bodies stubbed
  until the next Phase 1 iteration.

See `apps/backend/docs/core_workflow.md`.
"""

from app.core.workflow.models import PendingHumanDecisionRow, WorkflowExecutionRow
from app.core.workflow.service import (
    HANDLE_AGENT_EVENT,
    ROUTE_WORKFLOW,
    START_STEP,
    WorkflowEngine,
    _reset_for_tests,
    get_engine,
    handle_agent_event,
    request_cancel,
    resume_hitl,
    route_workflow,
    start_step,
)
from app.core.workflow.types import (
    TERMINAL_STATES,
    CommandCategory,
    CommandContext,
    CommandNotRegisteredError,
    Outcome,
    OutcomeKind,
    RetryPolicy,
    Step,
    TerminalAction,
    Workflow,
    WorkflowCommand,
    WorkflowError,
    WorkflowExecutionNotFoundError,
    WorkflowNotFoundError,
    WorkflowState,
)

__all__ = [
    "HANDLE_AGENT_EVENT",
    "ROUTE_WORKFLOW",
    "START_STEP",
    "TERMINAL_STATES",
    "CommandCategory",
    "CommandContext",
    "CommandNotRegisteredError",
    "Outcome",
    "OutcomeKind",
    "PendingHumanDecisionRow",
    "RetryPolicy",
    "Step",
    "TerminalAction",
    "Workflow",
    "WorkflowCommand",
    "WorkflowEngine",
    "WorkflowError",
    "WorkflowExecutionNotFoundError",
    "WorkflowExecutionRow",
    "WorkflowNotFoundError",
    "WorkflowState",
    "_reset_for_tests",
    "get_engine",
    "handle_agent_event",
    "request_cancel",
    "resume_hitl",
    "route_workflow",
    "start_step",
]
