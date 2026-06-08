# core/workflow

> Workflow engine — typed workflows, three command categories, async event-driven execution.

## Scope

- **Owns:** `Workflow`, `Step`, `WorkflowCommand` types, engine state machine, three taskiq task bodies (`start_step`, `handle_agent_event`, `route_workflow`), `workflow_executions` + `pending_human_decisions` tables, recovery-policy registry (`app/core/workflow/recovery.py`).
- **Does not own:** business logic (callers register typed `Workflow` + `WorkflowCommand` impls); workspace provisioning ([`core/workspace`](core_workspace.md)); task durability ([`core/tasks`](core_tasks.md)).
- **Boundary:** callers enqueue work by calling `engine.start()`; the engine routes via taskiq; terminal AgentEvents arrive from [`core/agent_gateway`](core_agent_gateway.md) via `handle_agent_event`.

## Why / invariants

- **Three tasks, not two** — Workspace commands can issue long-running AgentCommands. `start_step` exits after dispatch; workers stay free during the wait; `handle_agent_event` fires when the terminal event arrives.
- **Recovery fires at most once per step instance** — repeated `auth_expired` after recovery has run falls through to Tier-2 retry then Tier-3 transitions. Prevents infinite auth-refresh loops.
- **Finalizer step fires exactly once on terminal-fail, then execution records `failed`.** When `Workflow.finalizer_step_id` is set, the engine dispatches that step before recording `failed`. When the finalizer step completes (even with `transitions={"success": COMPLETE_WORKFLOW}`), the engine checks `_has_finalizer_fired` and redirects to the pending failure — never to `DONE`. The original failing step id and failure reason are stashed in `step_state` so they survive the round-trip through the finalizer. On the success path the finalizer step runs as the normal terminal step and transitions normally.
- **`workflow_executions.failure_reason`** — nullable short label written on terminal-fail (e.g. `provision_failed`, `agent_failure`). Populated from the `__failure_reason__` output key first, then the `outcome_label`. Exposed in `WorkflowExecutionSummary.failure_reason`.
- **`workflow.failed` audit row** — written to `core/audit_log` (via `audit(…, org_id=…, session=s)`) on every terminal-fail with payload `{workflow_execution_id, ticket_id, failed_step_id, failure_reason}`. The `org_id` comes from the `org_id` contextvar (set by `OrgContextMiddleware` in task bodies); falls back to `ticket_payload.org_id` when no contextvar is set (e.g. test bodies that bypass middleware), last resort `UUID(int=0)` with a warning log.
- **Workspace commands always dispatch to `awaiting_agent`** — the engine parks the execution and assigns `pending_agent_command_id`; the terminal AgentEvent resumes routing. Provider resolution errors surface in the workspace module's dispatch, not the engine. There is no inline/in-memory dispatch path.
- **`WorkflowCommand.dispatch` is the Workspace seam** — Workspace-category commands satisfy `WorkspaceWorkflowCommand` (a sub-Protocol of `WorkflowCommand`) by implementing `async dispatch(inputs, ctx, *, session) -> UUID`. `start_step` calls it inside the same transaction it parks the execution in, then sets `pending_agent_command_id` to the returned `agent_commands.id`. Local + HITL commands never have `dispatch` called and need not implement it. Command-to-workflow correlation is stamped via `agent_commands.workflow_execution_id` at enqueue time — `record_agent_event` resolves the workflow from that column with no workspace-row dependency.
- **`$`-expression inputs** — `$<step_id>.<field>` reads a prior step's `outputs`; `$ticket.<field>` reads the payload stashed at `engine.start()` time. Absent fields return `None` rather than erroring.
- **Cross-module callers use read projections**, not raw SQLAlchemy rows. See `WorkflowExecutionSummary`, `HitlHistoryEntry`, and the `list_*` / `get_*` ops in `__init__.py`.

## Gotchas

- `register_workflow` allows forward references to unregistered commands; `start()` validates them and fails loud (no row written) when a step references an unregistered command.
- `TERMINAL_STATES = {done, failed, cancelled}` — check before enqueuing further work.
- `unregister_workflow` removes a workflow from the process-singleton engine.
- Test isolation uses `scoped_engine` / `scoped_workflow` from [`app/testing/workflow_harness`](../app/testing/workflow_harness.py); those names are no longer on `core/workflow`'s public surface. See [patterns.md § `scoped_*` context managers](patterns.md).

## Vocabulary

- **WorkflowCommand** — base Protocol; one registered impl per `kind`. Carries `restart_safe`, `category` (`Workspace | Local | Hitl`), `execute`.
- **WorkspaceWorkflowCommand** — sub-Protocol adding `dispatch(inputs, ctx, *, session) -> UUID` for Workspace-category commands. Structurally satisfied by any class with the right shape; never subclassed explicitly in production.
- **CommandContext** — payload a command sees: execution id, ticket id, step id, attempt counter, optional traceparent.
- **Outcome** — tagged by `OutcomeKind` (`success | failure | hitl_pending`). Carries `outputs`, optional failure/hitl fields, and `append_steps` escape hatch. `Outcome.failure(reason=…)` sets `__failure_reason__` in outputs, which `route_workflow` reads for `failure_reason`.
- **`Workflow.finalizer_step_id`** — optional step id; when set the engine routes to that step on terminal-fail (before recording `failed`). One-shot per execution. Absent / already-fired flag lives in `step_state[__finalizer_fired__]`.
- **Recovery-policy insertion (Tier-1)** — engine checks `core/workflow.get_recovery_policy(label)` (via `app/core/workflow/recovery.py`) before Tier-2 retry; appends a synthetic recovery step and resets the failed step's attempt counter. Producers (e.g. `core/workspace`) register their policies via an explicit startup call (`register_workspace_recovery_policies()`), not at import time — both `web.py` and `worker.py` call this after importing workspace.

## Data owned

- `workflow_executions` — indexes on `state`, `pending_agent_command_id`, `ticket_id`. `failure_reason TEXT` column (nullable; added migration 041). Migration 015 (table).
- `pending_human_decisions` — index on `(workflow_execution_id, resolved_at)`. Migration 015.

## How it's tested

`test/test_types.py` — typed data validation (workflows, steps, retry policy, outcomes, terminal transitions).
`test/test_engine.py` — register validation (unknown entry step, dangling transitions, double-register), version-selection, `start()` writes `pending` row + enqueues `route_workflow`, `start()` fails loud on unregistered command.
`test/test_recovery_registry.py` — register/get/conflict/idempotent/sorted-labels; verifies workspace's recovery policy resolves after explicit `register_workspace_recovery_policies()` call.
`test/test_workspace_dispatch_service.py` — Workspace-branch parks on the dispatched `command_id`, terminal event resumes via `agent_commands.workflow_execution_id`.
`app/core/workspace/test/test_lean_lifecycle_service.py` — finalizer fires exactly once on terminal-fail; success path no refire; `failure_reason` + `workflow.failed` audit row written; original failure context survives the finalizer round-trip.
`test/test_state_machine.py::test_finalizer_runs_then_workflow_records_failed` — finalizer with `"success": COMPLETE_WORKFLOW` in its own transitions still ends in `FAILED`, not `DONE` (production-shape coverage).
