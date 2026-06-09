"""Typed data structures for `core/workflow`.

Mirrors ` §
Workflow + WorkflowCommand model`. Workflows are stored as typed Pydantic
data; commands are objects implementing `WorkflowCommand`.

This module defines the SHAPE — the engine + task bodies live in `service.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class WorkflowState(StrEnum):
    """`workflow_executions.state`. See architecture.md state-machine table."""

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_AGENT = "awaiting_agent"
    AWAITING_HUMAN = "awaiting_human"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES: frozenset[WorkflowState] = frozenset(
    {WorkflowState.DONE, WorkflowState.FAILED, WorkflowState.CANCELLED}
)


class CommandCategory(StrEnum):
    """The three WorkflowCommand categories. Each gets a distinct branch in
    `start_step` (see architecture.md § Async event-driven model)."""

    WORKSPACE = "workspace"
    LOCAL = "local"
    HITL = "hitl"


class TerminalAction(StrEnum):
    """A `Step.transitions` value can be either a target step id (str) or one
    of these terminal-action sentinels."""

    COMPLETE_WORKFLOW = "complete_workflow"
    FAIL_WORKFLOW = "fail_workflow"


class RetryPolicy(BaseModel):
    """Per-step retry budget. Tier-2 in the three-tier model."""

    model_config = ConfigDict(frozen=True)
    max_attempts: int = Field(default=1, ge=1)
    backoff_seconds: float = Field(default=0.0, ge=0.0)


class Step(BaseModel):
    """One node in a Workflow. `transitions[label]` is either a step id or a
    `TerminalAction`. Default transitions (when not explicitly set) are
    `success → next listed step` and `failure → fail_workflow`."""

    model_config = ConfigDict(frozen=True)
    id: str
    command_kind: str
    inputs: Mapping[str, str] = Field(default_factory=dict)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    hitl: bool = False
    transitions: Mapping[str, str | TerminalAction] = Field(default_factory=dict)


class Workflow(BaseModel):
    """A typed workflow definition. Registered once at startup against the
    engine; executed many times against `workflow_executions` rows."""

    model_config = ConfigDict(frozen=True)
    name: str
    version: int = Field(ge=1)
    steps: tuple[Step, ...]
    entry_step_id: str
    # When set, the engine runs this step on any terminal-fail before recording
    # `failed`. One-shot: fires only on terminal-fail; on the success path the
    # step runs as the normal terminal step and the finalizer does not re-fire.
    finalizer_step_id: str | None = None

    def step_by_id(self, step_id: str) -> Step | None:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None


# ── Outcomes ────────────────────────────────────────────────────────────


class OutcomeKind(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    HITL_PENDING = "hitl_pending"


class Outcome(BaseModel):
    """The result of a WorkflowCommand.execute(). One of three shapes —
    discriminated by `kind`. `append_steps` is the escape hatch the engine
    inserts at the front of the remaining sequence before evaluating the
    transition."""

    model_config = ConfigDict(frozen=True)
    kind: OutcomeKind
    label: str = "success"
    outputs: Mapping[str, Any] = Field(default_factory=dict)
    failure_reason: str | None = None
    hitl_question: Mapping[str, Any] | None = None
    append_steps: tuple[Step, ...] = ()

    @classmethod
    def success(
        cls,
        *,
        label: str = "success",
        outputs: Mapping[str, Any] | None = None,
        append_steps: tuple[Step, ...] = (),
    ) -> Outcome:
        return cls(
            kind=OutcomeKind.SUCCESS,
            label=label,
            outputs=outputs or {},
            append_steps=append_steps,
        )

    @classmethod
    def failure(
        cls,
        *,
        reason: str,
        label: str = "failure",
        outputs: Mapping[str, Any] | None = None,
    ) -> Outcome:
        return cls(
            kind=OutcomeKind.FAILURE,
            label=label,
            outputs=outputs or {},
            failure_reason=reason,
        )

    @classmethod
    def hitl_pending(cls, *, question: Mapping[str, Any]) -> Outcome:
        return cls(kind=OutcomeKind.HITL_PENDING, label="hitl_pending", hitl_question=question)


# ── Command interface ───────────────────────────────────────────────────


class CommandContext(BaseModel):
    """Context passed to every WorkflowCommand.execute(). Carries the
    workflow execution id, the active OTel traceparent (for span linkage),
    attempt counter (Tier-2 retry), and ticket id for cross-domain lookups.

    Commands receive their inputs as a typed Pydantic model — that's their
    entire workflow-related payload. They never read `step_state`.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    workflow_execution_id: str
    ticket_id: str
    step_id: str
    attempt: int
    traceparent: str | None = None


@runtime_checkable
class WorkflowCommand(Protocol):
    """A WorkflowCommand. Implementations register against `WorkflowEngine`
    by their `kind` string. The category dictates how `start_step` runs them:

    - **Workspace** category — must additionally satisfy
      `WorkspaceWorkflowCommand` (`dispatch(inputs, ctx, *, session) -> UUID`).
      The engine calls `dispatch` inside `start_step`'s transaction to enqueue
      an AgentCommand row and parks on the returned `command_id`.
    - **Local + HITL** category — only `execute` is called; `dispatch` is never
      invoked and need not exist.
    """

    kind: str
    category: CommandCategory
    restart_safe: bool

    async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome: ...


@runtime_checkable
class WorkspaceWorkflowCommand(WorkflowCommand, Protocol):
    """Workspace-category seam: enqueue an `AgentCommand` durably inside the
    caller's transaction and return the new `command_id`. The engine sets
    `pending_agent_command_id` to the returned value and parks the workflow
    in `awaiting_agent`."""

    async def dispatch(
        self,
        inputs: BaseModel,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> UUID: ...


class WorkflowError(Exception):
    """Base class for workflow-engine errors. Raised by the engine, not by
    commands (commands return `Outcome.failure()` instead)."""


class WorkflowNotFoundError(WorkflowError):
    """Engine couldn't resolve a workflow name + version."""


class CommandNotRegisteredError(WorkflowError):
    """A Step references a `command_kind` that no WorkflowCommand has registered."""


class WorkflowExecutionNotFoundError(WorkflowError):
    """The engine looked up a workflow execution by id and got nothing."""
