"""`WorkflowEngine` — workflow + command registries, start().

Phase 1 (foundations) ships:
- `WorkflowEngine` with `register_workflow`, `register_command`, `start`.
- `start()` creates a `workflow_executions` row and enqueues an initial
  `route_workflow` task via `core/tasks`. The router task body, the
  `start_step` task body, and `handle_agent_event` land in later
  iterations of Phase 1 (the bulk of the engine state-machine logic).

The engine is a singleton — `get_engine()` returns the process-wide
instance. Domain modules register their workflows + commands once at
import time (Phase 4+).
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tasks import TaskContext, TaskRef, enqueue, task
from app.core.workflow.models import WorkflowExecutionRow
from app.core.workflow.types import (
    CommandNotRegisteredError,
    TerminalAction,
    Workflow,
    WorkflowCommand,
    WorkflowError,
    WorkflowNotFoundError,
    WorkflowState,
)

log = structlog.get_logger("core.workflow")


# Placeholder task bodies. The real implementations land in the next
# Phase 1 iteration (state machine + atomic transitions + outcome routing).
# Registered now so the @task names exist and `enqueue` has stable refs.


@task("workflow.start_step", queue="workflow", max_retries=1)
async def start_step(
    ctx: TaskContext,
    *,
    workflow_execution_id: str,
    step_id: str,
    attempt: int,
    inputs: dict,
    traceparent: str | None = None,
) -> None:
    """Phase 1 (foundations): placeholder. The real body lands in the next
    Phase 1 commit — looks up the command, branches on category, dispatches
    Workspace command + sets `awaiting_agent` / runs Local inline / writes
    pending HITL row."""
    del ctx, workflow_execution_id, step_id, attempt, inputs, traceparent
    raise NotImplementedError("workflow.start_step body lands in Phase 1 cont'd")


@task("workflow.handle_agent_event", queue="workflow", max_retries=1)
async def handle_agent_event(
    ctx: TaskContext,
    *,
    workflow_execution_id: str,
    agent_command_id: str,
    outcome_label: str,
    outputs: dict,
    traceparent: str | None = None,
) -> None:
    """Phase 1 (foundations): placeholder. Validates the event matches
    `pending_agent_command_id`, clears it, enqueues `route_workflow`."""
    del ctx, workflow_execution_id, agent_command_id, outcome_label, outputs, traceparent
    raise NotImplementedError("workflow.handle_agent_event body lands in Phase 1 cont'd")


@task("workflow.route_workflow", queue="workflow", max_retries=1)
async def route_workflow(
    ctx: TaskContext,
    *,
    workflow_execution_id: str,
    completed_step_id: str | None,
    outcome_label: str | None,
    outputs: dict,
    traceparent: str | None = None,
) -> None:
    """Phase 1 (foundations): placeholder. Persists outcome, evaluates the
    step's transitions, enqueues the next `start_step` or marks the
    workflow terminal."""
    del ctx, workflow_execution_id, completed_step_id, outcome_label, outputs, traceparent
    raise NotImplementedError("workflow.route_workflow body lands in Phase 1 cont'd")


# Export the task refs for the engine + future tests.
START_STEP: TaskRef = start_step
HANDLE_AGENT_EVENT: TaskRef = handle_agent_event
ROUTE_WORKFLOW: TaskRef = route_workflow


class WorkflowEngine:
    """Workflow + WorkflowCommand registry. Process-singleton via
    `get_engine()`. Domain modules call `register_workflow(...)` and
    `register_command(...)` once at import time.

    `start(workflow_name, ticket_id, *, session)` opens a workflow execution
    and enqueues the initial routing task. Required `session` — the caller
    commits and the outbox drain delivers the task post-commit.
    """

    def __init__(self) -> None:
        self._workflows: dict[tuple[str, int], Workflow] = {}
        self._commands: dict[str, WorkflowCommand] = {}

    def register_workflow(self, wf: Workflow) -> None:
        key = (wf.name, wf.version)
        if key in self._workflows:
            raise WorkflowError(f"workflow '{wf.name}' v{wf.version} already registered")
        if wf.step_by_id(wf.entry_step_id) is None:
            raise WorkflowError(f"workflow '{wf.name}' entry_step_id '{wf.entry_step_id}' not in steps")
        for step in wf.steps:
            # Forward reference: commands can register after workflows, so we
            # don't check `_commands` here. The engine validates command
            # registration at start() time.
            for label, target in step.transitions.items():
                # TerminalAction subclasses str (StrEnum) — exclude it from the
                # step-id resolution check.
                if isinstance(target, TerminalAction):
                    continue
                if isinstance(target, str) and wf.step_by_id(target) is None:
                    raise WorkflowError(
                        f"workflow '{wf.name}' step '{step.id}' transitions['{label}'] points to "
                        f"unknown step '{target}'"
                    )
        self._workflows[key] = wf

    def register_command(self, command: WorkflowCommand) -> None:
        if command.kind in self._commands:
            raise WorkflowError(f"WorkflowCommand kind '{command.kind}' already registered")
        self._commands[command.kind] = command

    def get_workflow(self, name: str, version: int | None = None) -> Workflow:
        if version is not None:
            wf = self._workflows.get((name, version))
            if wf is None:
                raise WorkflowNotFoundError(f"workflow '{name}' v{version} not registered")
            return wf
        # Latest version when unspecified.
        matches = [w for (n, _), w in self._workflows.items() if n == name]
        if not matches:
            raise WorkflowNotFoundError(f"workflow '{name}' not registered")
        return max(matches, key=lambda w: w.version)

    def get_command(self, kind: str) -> WorkflowCommand:
        cmd = self._commands.get(kind)
        if cmd is None:
            raise CommandNotRegisteredError(f"WorkflowCommand kind '{kind}' not registered")
        return cmd

    def registered_workflow_names(self) -> list[str]:
        return sorted({name for (name, _) in self._workflows})

    def registered_command_kinds(self) -> list[str]:
        return sorted(self._commands.keys())

    async def start(
        self,
        *,
        workflow_name: str,
        ticket_id: str,
        version: int | None = None,
        traceparent: str | None = None,
        session: AsyncSession,
    ) -> str:
        """Create a `workflow_executions` row in `pending` state, enqueue
        the initial `route_workflow` task (which decides the first step),
        and return the new execution id. Required `session` — the caller
        commits and the outbox drain delivers the task post-commit."""
        wf = self.get_workflow(workflow_name, version=version)
        # Validate every step's command_kind is registered before we even
        # write a row. Fail loud at start, not mid-execution.
        for step in wf.steps:
            self.get_command(step.command_kind)

        row = WorkflowExecutionRow(
            ticket_id=ticket_id,
            workflow_name=wf.name,
            workflow_version=wf.version,
            state=WorkflowState.PENDING.value,
            current_step_id=None,
            pending_agent_command_id=None,
            step_state={},
            cancel_requested=False,
            otel_trace_context=traceparent,
        )
        session.add(row)
        await session.flush()

        await enqueue(
            ROUTE_WORKFLOW,
            args={
                "workflow_execution_id": str(row.id),
                "completed_step_id": None,
                "outcome_label": None,
                "outputs": {},
                "traceparent": traceparent,
            },
            session=session,
        )
        log.info(
            "workflow.started",
            workflow_execution_id=str(row.id),
            workflow_name=wf.name,
            workflow_version=wf.version,
            ticket_id=ticket_id,
        )
        return str(row.id)


_engine: WorkflowEngine | None = None


def get_engine() -> WorkflowEngine:
    """Process-singleton engine. Tests call `_reset_for_tests()` between runs."""
    global _engine
    if _engine is None:
        _engine = WorkflowEngine()
    return _engine


def _reset_for_tests() -> None:
    """Drop the process-singleton; test fixtures rebuild it. Also clears the
    `core/tasks` registry since the three task bodies register at import
    time of this module."""
    global _engine
    _engine = None
