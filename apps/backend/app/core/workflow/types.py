"""Typed data structures for `core/workflow`.

This module defines the SHAPE — the engine + task bodies live in `service.py`.

Key types:
- `StepRef[I, O]` — one workflow step node; points at a command class and carries
  an optional lambda to evaluate typed inputs.
- `WorkflowInputRef[T]` — synthetic "step 0" giving the workflow's startup snapshot
  typed access via `_step_outputs_var`.
- `Workflow` — a typed workflow definition (steps + entry + transitions + optional finalizer).
- `Outcome` — the result of a `WorkflowCommand.execute()`.
- `WorkflowCommand` — base Protocol (kind + Inputs + Outputs ClassVars only).
- `AgentDispatchCommand` — ABC for commands that enqueue an AgentCommand and park in
  AWAITING_AGENT; engine uses isinstance to discriminate.
- `WorkspaceOpCommand` / `CodingAgentCommand` — concrete ABC sub-hierarchies (defined
  in `core/workspace/commands_base.py` and `core/coding_agent/commands_base.py`).
- `LocalCommand` — structural Protocol for the in-process command flavour (no isinstance in engine).
- `HITLCommand` — ABC for human-in-the-loop commands; engine uses isinstance to discriminate.
- `Empty` — zero-field frozen BaseModel used as the default `Inputs`/`Outputs`.
- `_NullDispatch` — private exception raised by `WorkspaceOpCommand.dispatch` when
  `build_command` returns None; engine catches it and short-circuits to success.
"""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


# ── Workflow callback type aliases ──────────────────────────────────────

WorkflowStartCallback = Callable[..., Awaitable[None]]
"""Async callable the engine invokes on workflow bootstrap.

Called inside the engine's bootstrap-commit transaction with keyword args:
  workflow_execution_id: UUID, workflow_name: str, ticket_id: UUID,
  org_id: UUID, session: AsyncSession.
Must NEVER commit (engine commits after callback returns). Raising rolls back
the entire bootstrap write.
"""

WorkflowTerminalCallback = Callable[..., Awaitable[None]]
"""Async callable the engine invokes on every terminal workflow transition.

Called inside the engine's terminal-commit transaction with keyword args:
  workflow_execution_id: UUID, workflow_name: str, ticket_id: UUID,
  org_id: UUID, terminal_state: WorkflowState, failure_reason: str | None,
  session: AsyncSession.
Same commit / raise rules as WorkflowStartCallback.
"""


# ── Zero-field sentinel ─────────────────────────────────────────────────


class Empty(BaseModel):
    """Zero-field frozen Pydantic model. Default `Inputs` and `Outputs` type
    for workflow commands that don't declare any."""

    model_config = ConfigDict(frozen=True)


class _DynModel(BaseModel):
    """Internal fallback for `Outcome.outputs` / `hitl_question` when a raw
    dict is passed for backward compatibility. Extra fields are stored and
    accessible as attributes."""

    model_config = ConfigDict(extra="allow")

    def __getitem__(self, key: str) -> Any:
        """Attribute-style access via dict syntax for backward-compat test assertions."""
        extras = self.model_extra or {}
        if key in extras:
            return extras[key]
        return getattr(self, key)

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, dict):
            return self.model_dump() == other
        return super().__eq__(other)


# ── ContextVar for step-output access in input lambdas ─────────────────

# Populated by `_enqueue_start_step` before evaluating a step's
# `inputs_factory` lambda.  Keyed by `step_id` (or `"__workflow_input__"` for
# the workflow-level snapshot).  Reset after the lambda call completes.
_step_outputs_var: ContextVar[dict[str, BaseModel] | None] = ContextVar("_step_outputs_var", default=None)


def get_step_output(step_id: str) -> BaseModel | None:
    """Return the typed `Outputs` for a completed step, or ``None`` if the step
    has not run yet.

    Valid only inside an ``inputs_factory`` lambda — i.e., while the engine has
    populated the ContextVar before evaluating the lambda. Callers that need a
    fallback when an upstream step may not have run (e.g. a finalizer whose
    predecessor failed before completing) should guard on ``None``.
    """
    return (_step_outputs_var.get() or {}).get(step_id)


# ── StepRef and WorkflowInputRef ────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class StepRef:
    """One node in a Workflow — binds a command class to its step id, an
    optional typed-inputs factory lambda, and a retry policy.

    `outputs` is a lazy property: it reads the current value from
    `_step_outputs_var` so it is only valid while `_enqueue_start_step` has
    populated the ContextVar (i.e., inside a step's `inputs_factory` call).
    """

    command_class: type
    step_id: str
    inputs_factory: Callable[[], BaseModel] | None = dataclasses.field(default=None, compare=True)
    retry_policy: RetryPolicy = dataclasses.field(default_factory=lambda: RetryPolicy())

    @property
    def outputs(self) -> BaseModel:
        """Return the typed Outputs for this step from the current ContextVar.

        Only valid inside a `inputs_factory` lambda call (i.e., after
        `_enqueue_start_step` populates the ContextVar). Raises `KeyError`
        when the step hasn't run yet — callers must order steps correctly.
        """
        return (_step_outputs_var.get() or {})[self.step_id]


@dataclasses.dataclass(frozen=True)
class WorkflowInputRef:
    """Synthetic "step 0" that gives lambda access to the typed workflow-input
    snapshot.  `step_id` is always ``"__workflow_input__"`` so it's
    unambiguous in the ContextVar map.
    """

    snapshot_type: type
    step_id: str = "__workflow_input__"

    @property
    def outputs(self) -> BaseModel:
        """Return the typed snapshot from the current ContextVar."""
        return (_step_outputs_var.get() or {})["__workflow_input__"]


# ── Workflow enums and value objects ────────────────────────────────────


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


class TerminalAction(StrEnum):
    """A `Workflow.transitions` value can be either a target `StepRef` or one
    of these terminal-action sentinels."""

    COMPLETE_WORKFLOW = "complete_workflow"
    FAIL_WORKFLOW = "fail_workflow"


class RetryPolicy(BaseModel):
    """Per-step retry budget. Tier-2 in the three-tier model."""

    model_config = ConfigDict(frozen=True)
    max_attempts: int = Field(default=1, ge=1)
    backoff_seconds: float = Field(default=0.0, ge=0.0)


# ── Workflow ─────────────────────────────────────────────────────────────


class Workflow(BaseModel):
    """A typed workflow definition. Registered once at startup against the
    engine; executed many times against `workflow_executions` rows.

    `steps` is the ordered tuple of all step nodes (controls run-view ordering).
    `entry` is the first step to execute.
    `transitions` maps each StepRef to its outcome-label→next routing table;
    omitted entries fall back to `success → next-in-list` / `failure → fail_workflow`.
    `finalizer` is a one-shot cleanup step that fires on terminal-fail before
    recording failure.
    `workflow_input` is the typed snapshot Reference for the engine.start() payload;
    when set, `engine.start` validates the supplied BaseModel's type.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    name: str
    version: int = Field(ge=1)
    steps: tuple[StepRef, ...]
    entry: StepRef
    # dict[StepRef, dict[str, StepRef | TerminalAction]]
    transitions: Any = Field(default_factory=dict)
    finalizer: StepRef | None = None
    workflow_input: WorkflowInputRef | None = None

    recovery_commands: tuple[type, ...] = ()
    """Command classes that handle failure-label recovery, not listed in `steps`.

    The engine reads each class's `recovers_failure_label: ClassVar[str]` at
    `register_workflow` time and builds the failure-label → command-class map.
    The engine also auto-registers each class as a command (no separate
    `register_command` call needed). May be any command kind (Local or
    AgentDispatch).
    """
    on_start: WorkflowStartCallback | None = None
    """Async callback fired inside the bootstrap-commit transaction when the
    workflow first transitions to RUNNING. Set to None for workflows that
    don't need start-time side effects."""
    on_terminal: WorkflowTerminalCallback | None = None
    """Async callback fired inside the terminal-commit transaction on every
    done / failed / cancelled transition. Set to None for workflows that
    don't need terminal side effects."""

    def step_by_step_id(self, step_id: str) -> StepRef | None:
        for s in self.steps:
            if s.step_id == step_id:
                return s
        return None


# ── Outcomes ────────────────────────────────────────────────────────────


class OutcomeKind(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    HITL_PENDING = "hitl_pending"


class Outcome(BaseModel):
    """The result of a WorkflowCommand.execute(). One of three shapes —
    discriminated by `kind`."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)
    kind: OutcomeKind
    label: str = "success"
    outputs: Any = Field(default_factory=Empty)
    failure_reason: str | None = None
    retryable: bool = True
    hitl_question: Any = None

    @field_validator("outputs", mode="before")
    @classmethod
    def _coerce_outputs(cls, v: Any) -> BaseModel:
        if isinstance(v, BaseModel):
            return v
        if isinstance(v, dict):
            return _DynModel(**v) if v else Empty()
        return Empty()

    @field_validator("hitl_question", mode="before")
    @classmethod
    def _coerce_hitl_question(cls, v: Any) -> BaseModel | None:
        if v is None or isinstance(v, BaseModel):
            return v
        if isinstance(v, dict):
            return _DynModel(**v)
        return None

    @classmethod
    def success(
        cls,
        *,
        label: str = "success",
        outputs: BaseModel | Mapping[str, Any] | None = None,
    ) -> Outcome:
        return cls(
            kind=OutcomeKind.SUCCESS,
            label=label,
            outputs=outputs if outputs is not None else Empty(),
        )

    @classmethod
    def failure(
        cls,
        *,
        reason: str,
        label: str = "failure",
        outputs: BaseModel | Mapping[str, Any] | None = None,
    ) -> Outcome:
        return cls(
            kind=OutcomeKind.FAILURE,
            label=label,
            outputs=outputs if outputs is not None else Empty(),
            failure_reason=reason,
        )

    @classmethod
    def hitl_pending(cls, *, question: BaseModel | Mapping[str, Any]) -> Outcome:
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
    """Base Protocol for all WorkflowCommands. Implementations register against
    `WorkflowEngine` by their `kind` string.

    `Inputs` and `Outputs` are ClassVar type references — the engine uses
    them to reconstruct typed models from the task queue's serialised dict
    and to populate the ContextVar for downstream lambdas respectively.

    The engine discriminates concrete impls via isinstance against
    `AgentDispatchCommand`, `HITLCommand`, and `LocalCommand` — the class
    hierarchy IS the category; no enum needed.
    """

    kind: ClassVar[str]
    Inputs: ClassVar[type[BaseModel]]
    Outputs: ClassVar[type[BaseModel]]

    async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome: ...


class AgentDispatchCommand(ABC):
    """Abstract base for commands that enqueue an AgentCommand and park the
    workflow execution in AWAITING_AGENT.

    The engine uses `isinstance(command, AgentDispatchCommand)` to discriminate
    this branch. Concrete dispatch paths:
      - `WorkspaceOpCommand` (in `core/workspace/commands_base.py`) — operations
        on an existing workspace via `dispatch_via_workspace`.
      - `CodingAgentCommand` (in `core/coding_agent/commands_base.py`) — full
        invocation via `dispatch_invocation`.
      - `ProvisionWorkspace` (in `core/workspace/commands/provision.py`) — uses
        Layer 1 directly because no workspace row exists yet.
    """

    kind: ClassVar[str]
    Inputs: ClassVar[type[BaseModel]]
    Outputs: ClassVar[type[BaseModel]] = Empty  # type: ignore[assignment]

    @abstractmethod
    async def dispatch(
        self,
        inputs: BaseModel,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> UUID: ...


class LocalCommand(Protocol):
    """Protocol for in-process commands that execute synchronously in the
    `start_step` task and advance the workflow immediately.

    `session` is passed by the engine as a keyword argument.
    Command bodies must NEVER call `session.commit()` — the engine commits.

    Not runtime-checkable (isinstance not used by engine). Provides static
    type annotation only — the engine's Local branch is the implicit fallback
    when a command is neither `AgentDispatchCommand` nor `HITLCommand`.
    """

    kind: ClassVar[str]
    Inputs: ClassVar[type[BaseModel]]
    Outputs: ClassVar[type[BaseModel]]

    async def execute(
        self,
        inputs: BaseModel,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> Outcome: ...


class HITLCommand(ABC):
    """Abstract base for Human-in-the-Loop commands. Must return
    `Outcome.hitl_pending(question=...)`. Engine writes the
    `pending_human_decisions` row and parks in AWAITING_HUMAN.

    ABC (not Protocol) so `isinstance(cmd, HITLCommand)` is reliable — the
    engine uses it to discriminate the HITL branch from the Local branch.
    """

    kind: ClassVar[str]
    Inputs: ClassVar[type[BaseModel]]
    Outputs: ClassVar[type[BaseModel]]

    @abstractmethod
    async def execute(self, inputs: BaseModel, ctx: CommandContext) -> Outcome: ...


# ── Internal dispatch signals ────────────────────────────────────────────


class _NullDispatch(Exception):
    """Raised by `WorkspaceOpCommand.dispatch` when `build_command` returns None.

    The engine catches this in the AgentDispatch branch and treats the step
    as `Outcome.success()` without parking in AWAITING_AGENT. Used by
    `CleanupWorkspace` when `workspace_id` is None (no workspace to clean up).
    """


# ── Error hierarchy ─────────────────────────────────────────────────────


class WorkflowError(Exception):
    """Base class for workflow-engine errors. Raised by the engine, not by
    commands (commands return `Outcome.failure()` instead)."""


class WorkflowNotFoundError(WorkflowError):
    """Engine couldn't resolve a workflow name + version."""


class CommandNotRegisteredError(WorkflowError):
    """A step references a command kind that no WorkflowCommand has registered."""


class WorkflowExecutionNotFoundError(WorkflowError):
    """The engine looked up a workflow execution by id and got nothing."""


class WorkflowValidationError(WorkflowError):
    """Raised at `register_workflow` time when a step's `inputs_factory` lambda
    references a field that doesn't exist on the upstream step's `Outputs` type.
    Catches typos like `provision.outputs.workspaze_id` before they surface at
    runtime."""
