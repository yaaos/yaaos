# core/workflow

> Workflow engine — typed workflows, three command categories, async event-driven execution.

## Purpose

Owns `Workflow`, `Step`, `WorkflowCommand`, `Outcome`, and (Phase 1 cont'd) the three [`core/tasks`](core_tasks.md) task bodies that drive the engine (`start_step`, `handle_agent_event`, `route_workflow`). Workflows are typed data, registered at startup; the engine is mechanism, not policy.

## Public interface

Exports `Workflow`, `Step`, `RetryPolicy`, `WorkflowCommand`, `Outcome`, `OutcomeKind`, `CommandCategory`, `CommandContext`, `TerminalAction`, `WorkflowState`, `TERMINAL_STATES`, `WorkflowEngine`, `get_engine`, the SQLAlchemy rows `WorkflowExecutionRow` + `PendingHumanDecisionRow`, the task refs `START_STEP` / `HANDLE_AGENT_EVENT` / `ROUTE_WORKFLOW`, and the exception hierarchy (`WorkflowError`, `WorkflowNotFoundError`, `CommandNotRegisteredError`, `WorkflowExecutionNotFoundError`). See `app/core/workflow/__init__.py`.

- `WorkflowEngine.register_workflow(wf)` — register a typed `Workflow`. Validates the entry-step id and inter-step transition targets. Forward references to unregistered commands are allowed here; `start()` validates them.
- `WorkflowEngine.register_command(cmd)` — register a `WorkflowCommand` (Protocol) by its `kind`.
- `WorkflowEngine.start(*, workflow_name, ticket_id, version=None, traceparent=None, session)` — create a `workflow_executions` row in `pending` state, enqueue an initial `route_workflow` task via the outbox, return the new execution id. Required `session`; the caller commits.
- `get_engine()` — process-wide singleton.

## Module architecture

### Entities

- **WorkflowExecution** — one row in `workflow_executions` per in-flight workflow run. Identity: `id` (uuid). Carries the state-machine cursor, `step_state` for input resolution, `pending_agent_command_id` for the await-agent gate, `cancel_requested` flag, and `otel_trace_context`.
- **PendingHumanDecision** — one row per HITL pause. Identity: `id` (uuid). Closed by writing `resolution_payload` + `resolved_at` in the same transaction that re-enqueues the next step.

### Key value objects

- **Workflow** — frozen Pydantic. `name`, `version`, `steps`, `entry_step_id`.
- **Step** — frozen Pydantic. `id`, `command_kind`, `inputs` map (source expressions like `$provision.workspace_id`), `retry_policy`, `hitl`, `transitions` map (label → step id | TerminalAction).
- **Outcome** — frozen Pydantic. Tagged by `OutcomeKind` (`success | failure | hitl_pending`). Carries `outputs`, optional `failure_reason` or `hitl_question`, and the `append_steps` escape hatch.
- **CommandContext** — frozen Pydantic. Workflow execution id, ticket id, step id, attempt counter, optional traceparent. The entire workflow-related payload a command sees alongside its typed `inputs` model.

### Core user flows

1. **Register at startup.** Domain modules import `get_engine()` and call `register_command(...)` for each WorkflowCommand impl + `register_workflow(...)` for each typed `Workflow`.
2. **Start.** `domain/intake` (or any orchestrator) calls `engine.start(workflow_name=..., ticket_id=..., session=s)`. The engine validates every step's `command_kind` is registered, writes a `pending` `workflow_executions` row, enqueues an initial `route_workflow(workflow_execution_id, completed_step_id=None, ...)` via `core/outbox` in the same session.
3. **Route → start_step → handle_agent_event → route_workflow** — Phase 1 cont'd lands the state-machine bodies. See [architecture.md § Workflow execution model](../../../plan/milestones/M05-workspace-agent/architecture.md#workflow-execution-model).

### State machines

`workflow_executions.state` (`WorkflowState`):

| From | Event | To |
|---|---|---|
| `pending` | engine starts | `running` |
| `running` | Workspace step dispatched | `awaiting_agent` |
| `running` | HITL step pauses | `awaiting_human` |
| `running` | terminal action (`complete_workflow` / `fail_workflow`) | `done` / `failed` |
| `awaiting_agent` | terminal AgentEvent arrives | `running` |
| `awaiting_agent` | cancel + event arrival | `cancelled` |
| `awaiting_human` | resume signal | `running` |
| `awaiting_human` | cancel | `cancelled` |
| any | step failure + retry exhausted | `failed` |

`TERMINAL_STATES = {done, failed, cancelled}`.

### Phase boundaries

- **Phase 1 foundations (current commit)** — types, models, migration, `WorkflowEngine` register + start; three taskiq task names registered with placeholder bodies that raise `NotImplementedError`.
- **Phase 1 cont'd** — task bodies: `start_step` branches on command category; `handle_agent_event` validates + clears `pending_agent_command_id`; `route_workflow` persists outcome, applies retry budget, evaluates transitions. Cancellation. HITL resume API. OTel span propagation.

### Why three tasks (not two)

Workspace commands can issue long-running AgentCommands. `start_step` exits after dispatch; `handle_agent_event` is enqueued when the terminal event arrives at `core/agent_gateway`; `route_workflow` does the routing. Workers stay free during the wait. See [architecture.md § Why three tasks (not two)](../../../plan/milestones/M05-workspace-agent/architecture.md#why-three-tasks-not-two).

## Data owned

- `workflow_executions` — see [models.py](../app/core/workflow/models.py). Indexes on `state`, `pending_agent_command_id`, `ticket_id`. Created by migration `015_create_workflow_tables`.
- `pending_human_decisions` — see [models.py](../app/core/workflow/models.py). Index on `(workflow_execution_id, resolved_at)`. Same migration.

## How it's tested

- `test/test_types.py` — typed data validation (workflows, steps, retry policy, outcomes, terminal-action transitions).
- `test/test_engine.py` — register validation (unknown entry step, dangling transitions, double-register), version-selection (`get_workflow` picks latest), `start()` writes a `pending` row + enqueues `workflow.route_workflow` via the outbox, `start()` fails loud (no row written) when a step references an unregistered command.

Phase 1 cont'd adds: Local-only workflow runs to completion; Workspace step async cycle; failure + retry; HITL pause + resume; `append_steps` insertion; backend-restart resume; cancellation during `awaiting_agent`; idempotent duplicate-event handling; async-model load test (100 simultaneous workflows dispatch within < 1s wall time).
