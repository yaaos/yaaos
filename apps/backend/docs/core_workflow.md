# core/workflow

> Workflow engine — typed workflows, three command categories, async event-driven execution.

## Scope

- **Owns:** `Workflow`, `Step`, `WorkflowCommand` types, engine state machine, three taskiq task bodies (`start_step`, `handle_agent_event`, `route_workflow`), `workflow_executions` + `pending_human_decisions` tables, recovery-policy registry (`app/core/workflow/recovery.py`).
- **Does not own:** business logic (callers register typed `Workflow` + `WorkflowCommand` impls); workspace provisioning ([`core/workspace`](core_workspace.md)); task durability ([`core/tasks`](core_tasks.md)).
- **Boundary:** callers enqueue work by calling `engine.start()`; the engine routes via taskiq; terminal AgentEvents arrive from [`core/agent_gateway`](core_agent_gateway.md) via `handle_agent_event`.

## Why / invariants

- **Three tasks, not two** — Workspace commands can issue long-running AgentCommands. `start_step` exits after dispatch; workers stay free during the wait; `handle_agent_event` fires when the terminal event arrives.
- **Recovery fires at most once per step instance** — repeated `auth_expired` after recovery has run falls through to Tier-2 retry then Tier-3 transitions. Prevents infinite auth-refresh loops.
- **Workspace commands always dispatch to `awaiting_agent`** — the engine parks the execution and assigns `pending_agent_command_id`; the terminal AgentEvent resumes routing. Provider resolution errors surface in the workspace module's dispatch, not the engine. There is no inline/in-memory dispatch path.
- **`$`-expression inputs** — `$<step_id>.<field>` reads a prior step's `outputs`; `$ticket.<field>` reads the payload stashed at `engine.start()` time. Absent fields return `None` rather than erroring.
- **Cross-module callers use read projections**, not raw SQLAlchemy rows. See `WorkflowExecutionSummary`, `HitlHistoryEntry`, and the `list_*` / `get_*` ops in `__init__.py`.

## Gotchas

- `register_workflow` allows forward references to unregistered commands; `start()` validates them and fails loud (no row written) when a step references an unregistered command.
- `TERMINAL_STATES = {done, failed, cancelled}` — check before enqueuing further work.
- `unregister_workflow` removes a workflow from the process-singleton engine.
- Test isolation uses `scoped_engine` / `scoped_workflow` from [`app/testing/workflow_harness`](../app/testing/workflow_harness.py); those names are no longer on `core/workflow`'s public surface. See [patterns.md § `scoped_*` context managers](patterns.md).

## Vocabulary

- **WorkflowCommand** — Protocol; one registered impl per `kind`. Carries `restart_safe`, `category` (`Workspace | Local | Hitl`).
- **CommandContext** — payload a command sees: execution id, ticket id, step id, attempt counter, optional traceparent.
- **Outcome** — tagged by `OutcomeKind` (`success | failure | hitl_pending`). Carries `outputs`, optional failure/hitl fields, and `append_steps` escape hatch.
- **Recovery-policy insertion (Tier-1)** — engine checks `core/workflow.get_recovery_policy(label)` (via `app/core/workflow/recovery.py`) before Tier-2 retry; appends a synthetic recovery step and resets the failed step's attempt counter. Producers (e.g. `core/workspace`) register their policies via an explicit startup call (`register_workspace_recovery_policies()`), not at import time — both `web.py` and `worker.py` call this after importing workspace.

## Data owned

- `workflow_executions` — indexes on `state`, `pending_agent_command_id`, `ticket_id`. Migration 015.
- `pending_human_decisions` — index on `(workflow_execution_id, resolved_at)`. Same migration.

## How it's tested

`test/test_types.py` — typed data validation (workflows, steps, retry policy, outcomes, terminal transitions).
`test/test_engine.py` — register validation (unknown entry step, dangling transitions, double-register), version-selection, `start()` writes `pending` row + enqueues `route_workflow`, `start()` fails loud on unregistered command.
`test/test_recovery_registry.py` — register/get/conflict/idempotent/sorted-labels; verifies workspace's recovery policy resolves after explicit `register_workspace_recovery_policies()` call.
