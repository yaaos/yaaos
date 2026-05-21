# M05 architecture

> Module layout, data model, lifecycles, protocols, internal contracts. Read [requirements.md](requirements.md) first.

## Backend module dependency graph

```
domain/intake ──→ core/workflow         (engine interface)
              ──→ domain/ticket
              ──→ core/workspace        (workspace-lifecycle WorkflowCommands)
              ──→ domain/reviewer       (CodeReview, PostFindings WorkflowCommands)

domain/reviewer ──→ core/workflow        (WorkflowCommand interface)
                ──→ domain/coding_agent  (invocation machinery)
                ──→ core/workspace       (workspace handle for invoke)

core/workspace  ──→ core/agent_gateway

core/workflow   ──→ core/tasks           (enqueue API; goes through outbox)
core/tasks      ──→ core/outbox          (DB-atomic enqueue mechanism)
              ──→ (taskiq + Redis broker, hidden behind the wrapper)
core/outbox     ──→ (Postgres + Redis)

core/agent_gateway ──→ (none — wire protocol only)
core/sse_pubsub ──→ (Redis pub/sub, for activity streaming to UI)
```

**Two new modules wrap the Redis-touching surface:**

- `core/tasks` is the only module that imports taskiq. Everything else (including `core/workflow`) uses `core/tasks`'s typed API.
- `core/outbox` is the only module that knows enqueueing happens via "write DB row + drain to Redis." Callers see a simple atomic-in-session API.
- `core/sse_pubsub` wraps Redis pub/sub for the SSE-streaming path.

If we swap the task queue or pub/sub backend, the blast radius is contained to these wrappers.

Adding a new ticket type later (e.g. investigation): new domain module owning the WorkflowCommands AND the workflow definition (e.g. `domain/investigator` with `domain/investigator/workflows/investigation_v1.py`). The new module registers its workflow with `core/workflow` at startup. `domain/intake` is extended only to recognize the new inbound signal type and route to the new ticket type — never touches workflow internals.

**Note: `domain/intake` and `domain/tickets` already exist in the codebase.** M05 extends them rather than creating them — see [the M05 plan revision](#m05-extension-of-existing-domainintake--domaintickets).

## Entities

| Term | Definition | Lifetime | Identity |
|---|---|---|---|
| **Intake** | An inbound signal that creates work. Sources: GitHub PR webhook (M05); future Slack, scheduled scans, user-initiated requests. | Synchronous handler. | Idempotency key derived per intake type. |
| **Ticket** | User-facing unit of work. Persistent. Has a type (`pr_review` for M05), state, payload. | Until terminal (`done`, `failed`, `cancelled`). | UUID + idempotency key. |
| **Workflow** | A typed data structure (definition) describing how a ticket type is processed. **Lives in the domain module that owns its WorkflowCommands** (e.g. `domain/reviewer/workflows/pr_review.py` for the `pr_review_v1` workflow). Versioned. Domain modules register their workflows with `core/workflow` at startup. | Definitions are immutable; workflow executions are bound to definition versions. | `<name>_<version>`. |
| **Workflow Execution** | A live instance of a Workflow being driven by the engine for one ticket. Has state, current step, attempt counters, OTel span context. | Created when workflow starts; terminal at `done` / `failed`. | UUID, references ticket + workflow definition. |
| **WorkflowCommand** | A unit of work invoked by the workflow engine as a step. Implementations live in domain modules. Three categories below. | One per step execution. | UUID per execution, attempt counter. |
| **AgentCommand** | Wire-protocol primitive from control plane to WorkspaceAgent. Single-flight per workspace. Five kinds: `CreateWorkspace`, `WriteFiles`, `RefreshWorkspaceAuth`, `InvokeClaudeCode`, `CleanupWorkspace`. | Seconds to minutes (or hours for future Implement-type modes). | UUID + attempt counter. |
| **Workspace** | Isolated sandbox: a dedicated OS process + directory + checked-out code + auth context. Hosts AgentCommands sequentially. | Up to 1h (TTL ceiling matches installation-token lifetime). | UUID. |
| **Agent** | Long-lived Go supervisor process on a customer ECS task. Spawns and routes commands to workspace processes. | As long as the ECS task lives. | Established once via sigv4 → bearer at startup. |

Notes:
- **Instance is not an entity.** The agent represents the host.
- **Workspace bound to its agent for life.** Agent dies → workspace dies. Replacement on a different agent. No migration.
- **Workspace bound to a single workflow execution for M05.** Schema (`current_holder_workflow_id` nullable column rather than hard FK) keeps the future relaxation add-only.
- **AgentCommand and WorkflowCommand are different layers, deliberately.** Some kinds share names across layers (`CreateWorkspace`, `CleanupWorkspace`) because they describe the same operation at different abstractions — disambiguated by layer noun.

## Workflow + WorkflowCommand model

### Workflow as typed data

A `Workflow` is a typed Pydantic data structure (not ad-hoc code).

| Field | Meaning |
|---|---|
| `name` | Identifier, e.g. `pr_review`. |
| `version` | Integer, incremented on breaking changes. In-flight executions keep using their definition version. |
| `steps` | Ordered list of `Step`. |
| `entry_step_id` | The first step (typically the first in the list). |

Each `Step`:

| Field | Meaning |
|---|---|
| `id` | Identifier unique within the workflow (e.g. `provision`). |
| `command_kind` | The WorkflowCommand kind, e.g. `CreateWorkspace`. |
| `inputs` | Mapping from input names to source expressions (`$ticket.repo`, `$provision.workspace_id`, `$mint_install_token`). |
| `retry_policy` | Bounded attempts + backoff. |
| `hitl` | If true, step pauses workflow until external resume signal. |
| `transitions` | Map from outcome label → next step ID, or terminal action (`fail_workflow`, `complete_workflow`). Default: `success → <next listed step>`, `failure → fail_workflow`. |

### The three WorkflowCommand categories

| Category | What it does | Examples (M05 + near-future) | Implementation home |
|---|---|---|---|
| **Workspace** | Operates on a workspace. Issues one or more AgentCommands under the hood. | `ProvisionWorkspace` (issues `CreateWorkspace` + `WriteFiles`), `CleanupWorkspace`, `RefreshWorkspaceAuth` (lifecycle); `CodeReview`, `IncrementalReview`, `VerifyFix`, `StaleCheck`, `AnswerQuestion` (work — **all five ship in M05**) | `core/workspace` owns lifecycle ones; `domain/reviewer` owns all five work commands. |
| **Local** | Runs in the backend process. No workspace. | `CheckShouldReview` (PR draft / skip-label / external-contributor / org-config gating, in `domain/reviewer`); `PostFindings`, `ResolveFinding`, `ArchiveStaleFindings`, `PostReply` (all in `domain/reviewer`, target-shape varies per task mode); future `NotifyUser`, `RecordAudit` | Whatever domain module owns the concern. |
| **HITL** | Suspends workflow until a human resolves it. | Future `RequestApproval`, `AwaitClarification` | `core/workflow` provides the primitive; domain modules instantiate. |

**One WorkflowCommand can issue multiple AgentCommands.** `ProvisionWorkspace` is the example: it issues `CreateWorkspace` and then `WriteFiles` (with the org's yaaos skills) as part of the same WorkflowCommand execution. Atomicity is at the WorkflowCommand level — if either AgentCommand fails, the WorkflowCommand fails.

### WorkflowCommand interface

A WorkflowCommand implementation declares:

- `kind` (string).
- `restart_safe` (boolean — see restart-safety section below).
- `inputs_schema` (Pydantic model — what it expects).
- `outputs_schema` (Pydantic model — what it returns; available as `$<step_id>.<field>` to later steps).
- `execute(inputs, context) -> Outcome` where `Outcome` is one of: success-with-outputs, failure-with-reason, hitl-pending-with-question-payload. The implementation may also return `append_steps=[...]` (see escape hatch).

### State machine

`WorkflowExecution` row tracks:

- `id`, `workflow_name`, `workflow_version`, `ticket_id`.
- `state`: one of `pending`, `running`, `awaiting_agent`, `awaiting_human`, `done`, `failed`, `cancelled`.
- `current_step_id`.
- `pending_agent_command_id` (nullable uuid): the AgentCommand we're waiting for (when `state = awaiting_agent`). Cleared when terminal event arrives.
- `step_state`: per-step attempt counters + last outcome + outputs (kept for input resolution in later steps).
- `cancel_requested boolean default false`.
- `otel_trace_context`: serialized W3C traceparent + tracestate.
- `created_at`, `updated_at`.

State transitions:

```
pending ──start──→ running ──Workspace step dispatched──→ awaiting_agent
                          ──Local step success/failure──→ running (route_workflow)
                          ──HITL step──→ awaiting_human
                          ──terminal action──→ done | failed | cancelled

awaiting_agent ──agent terminal event arrives──→ running (handle_agent_event then route_workflow)
              ──cancel + event arrival──→ cancelled (route applies cancel path)

awaiting_human ──resume signal──→ running
              ──cancel──→ cancelled
```

### HITL pattern — how it actually works

1. Workflow reaches a step with `hitl: true`. Step implementation writes a row to a `pending_human_decisions` table (`workflow_execution_id`, `question_payload`, `created_at`).
2. Engine marks workflow `awaiting_human`. **Does not enqueue the next step.** No `core/tasks` task pending.
3. Workflow is dormant. No resource burn beyond a row in two tables.
4. Human visits UI, sees the decision, submits a response.
5. UI handler writes resolution to the decision row, transitions workflow to `running`, looks up the step's `transitions` map keyed on the user's response, enqueues the next step via `core/tasks` with that response as input.
6. Workflow resumes.

No `core/tasks` feature needed beyond "enqueue this task." HITL is a workflow state, not a long-running task.

### Dynamic step insertion (append-steps escape hatch)

A WorkflowCommand implementation can return `append_steps=[Step, Step, ...]` along with its outcome. The engine inserts those steps at the front of the remaining sequence before evaluating the next transition.

Used for: cases where a static workflow definition is the prefix, and what comes next is determined by what's discovered. Example future use: an `Investigate` step's findings determine how many `Plan` and `Implement` sub-steps follow.

M05's `pr_review` workflow does not use this mechanism, but the engine supports it.

### Three-tier retry separation

| Tier | Where | Triggered by | Action |
|---|---|---|---|
| **1. AgentCommand recovery** | `core/workspace` | Workspace process / agent reports a failure event. | Apply recovery policy (e.g. `auth_expired` → issue `RefreshWorkspaceAuth` AgentCommand; retry original). If recovery fails, dispose workspace + provision new + re-dispatch original WorkflowCommand. Bounded. From the engine's view this is still one WorkflowCommand execution in flight. |
| **2. WorkflowCommand step retry** | `core/workflow` engine | WorkflowCommand returns failure (after AgentCommand-level recovery exhausted). | Per `step.retry_policy` (default: 1 attempt). On exhaustion, evaluate `step.transitions[failure]`. |
| **3. Workflow-level transition** | `core/workflow` engine | Step's failure transition. | Route to next step, skip, fail workflow, or terminal action. Workflow failure → ticket marked `failed`. |

Ticket-level retry (re-running the workflow itself) is post-M05.

## Workflow execution model

How workflows actually run. They're never executed inside an intake / webhook / API call loop.

### Async event-driven model — workers never block on AgentCommand completion

The critical design property: **WorkflowCommands that issue AgentCommands (potentially long-running — Implement-type tasks can run for hours) do not hold a taskiq worker for the duration.** The step dispatches the AgentCommand and exits. The terminal event from the WorkspaceAgent triggers the next phase of execution. Workers stay free.

**Three task kinds in `core/workflow`:**

| Task | What it does | Duration |
|---|---|---|
| **`start_step(exec_id, step_id, attempt, inputs, traceparent)`** | Looks up the step's Command kind and dispatches based on category: (a) **Workspace** Command → calls `core/workspace.dispatch(workspace_id, agent_command_payload)` which writes the AgentCommand to the dispatch path + sets `WorkflowExecution.state = awaiting_agent` + sets `WorkflowExecution.pending_agent_command_id`, then exits. (b) **Local** Command → runs the Command body inline, persists outcome, enqueues `route_workflow`, exits. (c) **HITL** Command → writes `pending_human_decisions` row, sets `state = awaiting_human`, exits. | Workspace: milliseconds. Local: as long as the local op takes (typically seconds). HITL: milliseconds. |
| **`handle_agent_event(exec_id, agent_command_id, outcome_label, outputs, traceparent)`** | Triggered when an AgentCommand's terminal event arrives at `core/agent_gateway`. Validates that the event matches `WorkflowExecution.pending_agent_command_id` (race guard). Clears `pending_agent_command_id`, transitions `state` from `awaiting_agent` back to `running`, enqueues `route_workflow` with the outcome. | Milliseconds. |
| **`route_workflow(exec_id, completed_step_id, outcome_label, outputs, traceparent)`** | Persists outcome to `step_state`. Applies retry-budget check. Evaluates the step's transition map. Enqueues the next `start_step`, or marks the workflow `awaiting_human` / `done` / `failed` / `cancelled`. | Milliseconds. |

In prose we refer to the `route_workflow` pattern as **the WorkflowRouter**. There is no class named `WorkflowRouter` — the pattern is implemented as the `route_workflow` task body in `core/workflow`.

### Why three tasks (not two)

The earlier two-task design had `execute_step` synchronously awaiting AgentCommand terminal events. That works for short commands (CodeReview at minutes) but breaks for long-running ones (Implement at hours). A worker held for an hour while an AgentCommand runs means:
- 1-2 worker instances can support only N concurrent workflows where N = `worker_processes × per_process_concurrency`.
- A burst of Implement-type workflows would exhaust the pool and block everything else.

**Splitting `start_step` (dispatch + exit) from `handle_agent_event` (process result when it arrives) means workers only hold slots for milliseconds, never minutes.** The same 1-2 worker instances now support tens of thousands of in-flight workflows, almost all waiting on agents with zero worker cost.

### Output passing — through context, not DB reads

Step outputs are written to `WorkflowExecution.step_state[step_id]` by `route_workflow` (persistence) and read by `route_workflow` again when resolving the next step's inputs. **Commands themselves never read `step_state`.** The router resolves the next step's `inputs` map (e.g. `{workspace_id: "$provision.workspace_id"}`) against `step_state` + ticket payload, packages the values, and passes them as task arguments to the next `start_step`.

This means:
- Commands receive a typed `inputs` Pydantic model. That's their entire workflow-related input.
- `step_state` schema can evolve without touching any Command.
- Commands can still read durable domain state (ticket, repos, lessons) through normal services / repos — exactly like the rest of the system.

### `start_step` task body

`start_step(exec_id, step_id, attempt, inputs, traceparent)`:

1. **Load WorkflowExecution.** Guard: if state isn't `running`, exit cleanly.
2. **Restore span context**; open child span `start:<kind>`.
3. **Validate inputs** against the Command's `inputs_schema`.
4. **Look up Command** from the engine's registry by `step.command_kind`.
5. **Branch on Command category:**
   - **Workspace Command:** open Postgres transaction. Call `core/workspace.dispatch(workspace_id, agent_command_payload)` — this writes the dispatch through `core/outbox` AND sets `workflow_executions.pending_agent_command_id = <new_command_id>` AND sets `state = 'awaiting_agent'`. Commit. The outbox drain pushes the AgentCommand to the WorkspaceAgent within ~100ms. Span closed. **Task exits.**
   - **Local Command:** call `command.execute(inputs, ctx)` inline. Persist outcome + outputs to `step_state[step_id]`. Enqueue `route_workflow(...)` via `core/outbox`, same transaction. Span closed. **Task exits.**
   - **HITL Command:** write `pending_human_decisions` row + set `state = 'awaiting_human'` in one transaction. Span closed. **Task exits.**

### `handle_agent_event` task body

Triggered by `core/agent_gateway` enqueueing it when an AgentCommand terminal event arrives via `/v1/commands/{id}/events`.

`handle_agent_event(exec_id, agent_command_id, outcome_label, outputs, traceparent)`:

1. **Load WorkflowExecution.** 
2. **Validate:** `workflow_execution.pending_agent_command_id == agent_command_id` AND `state == 'awaiting_agent'`. If either fails (race / cancelled / already handled): exit cleanly (idempotent).
3. **Open transaction.** Clear `pending_agent_command_id`. Transition `state = 'running'` (transient).
4. **Enqueue `route_workflow(exec_id, completed_step_id=current_step_id, outcome_label, outputs)`** via `core/outbox`.
5. **Commit.** Span closed. **Task exits.**

### `route_workflow` task body

`route_workflow(exec_id, completed_step_id, outcome_label, outputs, traceparent)`:

1. **Load WorkflowExecution.** Guard: if state is terminal or cancelled, exit cleanly.
2. **Restore span**; open child span `route:after-<step_id>`.
3. **If `completed_step_id` is null** (initial call from intake): decide first step is `entry_step_id`, enqueue `start_step`, exit.
4. **Persist outcome + outputs** to `step_state[completed_step_id]` in a transaction.
5. **Apply `append_steps`** from the outcome (if any).
6. **Check `cancel_requested` flag.** If true: skip remaining `runs_on_cancel=false` steps; enqueue next `runs_on_cancel=true` step (if any), else terminate with `cancelled`.
7. **If outcome is failure AND retry budget remains** for `(step_id, attempt)`: enqueue `start_step(exec_id, completed_step_id, attempt+1, inputs)`. Exit.
8. **Otherwise look up `step.transitions[outcome_label]`.** Three results:
   - **Next step.** Resolve next step's `inputs` map. Enqueue `start_step(exec_id, next_step_id, attempt=1, inputs)`.
   - **HITL pause** (next step has `hitl: true`). Resolve its inputs, write `pending_human_decisions` row, mark `state = awaiting_human`. Do not enqueue.
   - **Terminal action** (`complete_workflow` / `fail_workflow`). Mark execution terminal; mark ticket terminal.
9. **Commit** state change + enqueue in single transaction.
10. Span closed. **Task exits.**

### How events route to workflows

When the WorkspaceAgent sends `POST /v1/commands/{id}/events` with a terminal outcome:

1. `core/agent_gateway` receives.
2. Looks up `workspaces WHERE current_command_id = $id` → finds the workspace row.
3. Looks up `workspace.current_holder_workflow_id` → WorkflowExecution.
4. Validates: `workflow_execution.pending_agent_command_id == $id` (race-guard against stale events).
5. Enqueues `handle_agent_event(workflow_execution_id, command_id, outcome_label, outputs)` via `core/outbox`.
6. Clears `workspace.current_command_id` (workspace is no longer busy on a specific command).
7. Responds 200 to the WorkspaceAgent.

Lookup chain: `agent_command_id → workspaces → current_holder_workflow_id → WorkflowExecution`. All durable in Postgres. No in-memory state required.

### Failure isolation

| Failure mode | What happens |
|---|---|
| `start_step` crashes before persisting state | `core/tasks` retries the same task. Workflow state unchanged. Re-runs cleanly. |
| `start_step` crashes after persisting `pending_agent_command_id` + outbox-write but before transaction commit | Atomic — either all happen or none. Crash before commit = retry as if it never happened. |
| `start_step` crashes after commit | The outbox row is durable; drain picks it up; AgentCommand dispatched. Task crash is irrelevant. |
| AgentCommand terminal event arrives, but workflow has been cancelled | `handle_agent_event` validates state; if not `awaiting_agent`, exits cleanly. Event becomes a no-op. |
| Two events for the same command (retry on the agent side) | First triggers `handle_agent_event` → `pending_agent_command_id` cleared. Second arrives, validation fails, exit cleanly. Idempotent. |
| `handle_agent_event` crashes | `core/tasks` retries. Idempotent (validation handles "already processed"). |
| `route_workflow` crashes before deciding | `core/tasks` retries. Deterministic re-run from same state. |
| `route_workflow` crashes after enqueue + commit | Already done; retry is a no-op (idempotent). |
| Backend crashes while WorkflowExecution is in `awaiting_agent` | WorkflowExecution row durable. When the agent eventually sends the event, `core/agent_gateway` enqueues `handle_agent_event` and the workflow resumes. |

### Cancellation interaction (Floor 2)

Cancel during `awaiting_agent`:
- UI cancel sets `cancel_requested=true` on the WorkflowExecution row.
- The in-flight AgentCommand on the WorkspaceAgent continues running (Floor 2: no mid-AgentCommand kill in M05).
- When the AgentCommand's terminal event eventually arrives, `handle_agent_event` enqueues `route_workflow` as normal.
- `route_workflow` checks `cancel_requested`; since set, transitions to cleanup path (run `runs_on_cancel=true` steps) and reaches `cancelled`.

Cancellation latency = remaining AgentCommand duration. Acceptable per the Floor 2 lock.

### HITL resume

Human responds via UI → API handler enqueues `route_workflow(exec_id, completed_step_id=<hitl_step>, outcome_label=<user_response>, outputs=<response_data>)`. Router applies the HITL step's transition map. Same shape as agent events arriving — just from a different source.

### Pacing + concurrency

- **`core/tasks` workers** scale horizontally. Each task is short — milliseconds for `start_step` (workspace branch), `handle_agent_event`, and `route_workflow`. Only Local Command bodies hold a worker for non-trivial time (typically seconds).
- **Worker pool sizing:** 1-2 worker instances with `WORKER_CONCURRENCY=100` give tens of thousands of in-flight workflows of headroom because most are sitting in `awaiting_agent` with zero worker cost.
- **Per-workspace concurrency** enforced at a different layer (`core/workspace`'s single-flight on `current_command_id`).
- **Per-org fairness:** none in M05. FIFO. Hot customer can briefly saturate workflow router cycles but the system stays responsive because per-task time is milliseconds. If saturation becomes real, per-org caps drop in as an additive feature later — not premature for POC.

### What happens on backend restart

- FastAPI workers restart cleanly (stateless).
- `core/tasks` workers restart cleanly (Redis broker persists pending tasks; in-flight task gets retried on the new worker if mid-execution).
- WorkflowExecution rows are durable; `awaiting_agent` survives. When the agent sends its terminal event after restart, the lookup chain still works.
- **One scenario worth calling out:** if backend crashes between `core/agent_gateway` receiving an event and enqueueing `handle_agent_event`, the event is lost. Mitigation: event ingestion writes to the outbox in the same transaction that updates workspace state, so the enqueue is atomic with the workspace state change. Worst case the workspace state moves and the workflow stays `awaiting_agent` forever; a periodic sweeper (TBD post-M05) reconciles.

## Engine implementation — clean layering

Four layers, separated on purpose:

| Layer | Owns |
|---|---|
| `core/workflow` engine | Workflow state machine, transitions (static + dynamic), HITL gating, retry decisions, span propagation, ticket-state synchronization. Knows nothing about how tasks are scheduled — just calls `core/tasks.enqueue(...)`. |
| `core/tasks` | Thin wrapper around taskiq. Provides `@task` decorator, `enqueue(task, args, *, session)` API, `TaskContext` (session + traceparent + attempt) passed to task bodies. Worker entrypoint. Hides taskiq-specific imports from biz logic. The `enqueue` is atomic-in-session by virtue of routing through `core/outbox` — callers don't see Redis directly. |
| `core/outbox` | Atomic-in-session enqueue mechanism. Writes to `outbox_entries` table inside the caller's transaction. A background **outbox drain** process polls the table and pushes ready entries to Redis (taskiq broker). The drain is one of the worker process's responsibilities. |
| taskiq + Redis | Durable enqueue (Redis broker), bounded retries on hard crash, periodic tasks, worker concurrency. We don't lean on taskiq's higher-level orchestration. |

We use only the small slice of taskiq we need (`@task` + `enqueue` + retry-on-hard-crash). This means `core/workflow` and all WorkflowCommands are portable — if taskiq ever has to be replaced, the blast radius is `core/tasks` only.

## `core/tasks` API + outbox pattern

The required-session pattern still applies. `enqueue` requires a session — but instead of writing the task directly to Redis, it writes to the outbox in the same DB transaction. The drain process pushes outbox entries to Redis after commit.

Surface (illustrative):

```
@task("route_workflow", queue="workflow", max_retries=3)
async def route_workflow(ctx: TaskContext, exec_id: str, completed_step_id: str | None, outcome_label: str | None): ...

# Caller side — atomic-in-session enqueue:
async with db_session() as s:
    await service_a(s, ...)
    await tasks.enqueue(route_workflow, args={...}, session=s)   # writes to outbox in s
    await s.commit()
# Drain picks up the outbox entry within ~100ms and pushes to Redis.
```

**Two things are deliberately leaky** (inherent to the pattern):

- **`enqueue` requires `session: AsyncSession`.** The atomic guarantee is "if the session commits, the outbox row is durable; the drain will deliver." If the session rolls back, the outbox row never exists, and the task isn't enqueued. Same shape as direct atomic enqueue from the caller's POV.
- **Task arguments are JSON-serializable.** Tasks must take primitive types or Pydantic models.

The "atomic-in-session" mental model is preserved for callers. The outbox layer is invisible to them.

### `core/tasks` concrete responsibilities

- **App configuration:** taskiq `Broker` configured with the Redis URL from settings. Workers connect to Redis.
- **Task discovery + registration:** worker entrypoint imports a known module (e.g. `apps.backend.app.tasks_registry`) which side-imports every module containing `@task`-decorated functions. Tasks register at import time. CI test asserts worker startup discovers the expected task set.
- **TaskContext:** dataclass with `session: AsyncSession` (a fresh session opened by the task wrapper for the task body), `traceparent: str | None`, `attempt: int`, `job_id: str`.
- **Retry policy:** declared on `@task(max_retries=, backoff=)`. Defaults to `max_retries=1` (taskiq retries the same task once on hard crash). Workflow-level retry is tier 2/3 in `core/workflow`, distinct.
- **Periodic tasks:** taskiq's scheduler exposed via `@task(periodic="*/1 * * * *")`. Used for workspace TTL sweep, outbox-drain heartbeat, future cleanup sweeps.
- **Worker concurrency:** configured via env (`WORKER_CONCURRENCY`, default 8). Separate Postgres connection pool per worker process.

### `core/outbox` concrete responsibilities

- **`outbox_entries` table:** `id uuid pk`, `kind text not null` (`taskiq_enqueue` initially; future kinds: `pubsub_publish`, etc.), `payload jsonb not null` (task name + args + queue), `created_at timestamptz`, `dispatched_at timestamptz` nullable, `attempt int default 0`. Index on `(dispatched_at, created_at)` for drain query.
- **`outbox.write(session, kind, payload)`:** the atomic primitive. Inserts an outbox row in the caller's session. Caller controls commit.
- **Outbox drain process:** runs as part of `apps/backend/bin/worker`. Polls `outbox_entries WHERE dispatched_at IS NULL ORDER BY created_at LIMIT N` (~100ms cadence). For each: dispatch to its target (Redis for `taskiq_enqueue`), then `UPDATE ... SET dispatched_at = now()`. On dispatch failure: leave row (next poll picks it up); after N failed attempts, mark dead and alert.
- **Idempotency:** drain sets `dispatched_at` *after* successful dispatch. If the drain crashes between dispatch and update, the next drain redispatches — taskiq sees a duplicate task. Task bodies are idempotent (they look up state from DB) so duplicate dispatch is safe.
- **Retention:** drained rows pruned by a periodic task (e.g., delete `dispatched_at < now() - 24h`).

### Relationship to existing `spawn()` pattern

`core/observability/spawn()` (fire-and-forget background work tracked via DB status fields) stays in the codebase for cases where:
- Work is request-scoped (immediate-ish completion without strong durability needs).
- No retry policy needed.
- The "long-running work is first-class domain state" pattern is already in use.

`core/tasks` (via outbox) is used for cases where:
- Work must survive backend restarts (durable via outbox + Redis).
- Work participates in a workflow (the workflow engine drives it).
- Work needs bounded retries with policy.

Don't conflate them: a future periodic GitHub-token health-check is a `core/tasks` periodic task; a fire-and-forget audit write after a domain action is still `spawn()`.

## Session management + atomicity pattern

Project-wide rule, adopted as part of M05. Documented in `apps/backend/docs/patterns.md`.

**Transactional service functions take a required `session: AsyncSession` and never commit.** The orchestrating layer (FastAPI endpoint handler, `core/tasks` task body) owns the transaction:

```
async def some_service_op(session: AsyncSession, ...) -> Result:
    await _do_work(session, ...)
    # never commits — caller owns transaction boundary
    return ...

# Orchestrator:
async with db_session() as s:
    await service_a(s, ...)
    await service_b(s, ...)
    await tasks.enqueue(some_task, args, session=s)   # atomic with the writes above
    await s.commit()
```

Why required (not optional):
- The type system catches "forgot to pass session" at the call site. With optional-session, callers could accidentally execute a service outside the orchestrator's transaction, causing premature commits and inconsistent state.
- Self-documenting: grep for `session: AsyncSession` to find all transactional functions.
- Consistent with the `core/tasks.enqueue(session=...)` contract.

**Exceptions** (functions that own their own session): fire-and-forget background work, periodic-task bodies that need a fresh session, request-independent maintenance tasks. These are named with `_owns_session` suffix or live in clearly-marked entrypoints. They are the minority.

**Refactor scope in M05 Phase 0:**
- `core/audit_log.audit()` — drop the optional-session branch, require session.
- All callers of `audit()` — already pass session in most cases; the few that don't get updated.
- Other services that currently open their own session (workspace lifecycle, reviewer queue, etc.) — refactor to required-session with the orchestrating layer owning transactions.
- `apps/backend/docs/patterns.md` — new section codifying the rule with one short example.

## Workspace provider contract

The `WorkspaceProvider` interface in `core/workspace` is the boundary. Two implementations:

| Provider | What it does | When used |
|---|---|---|
| `InMemoryWorkspaceProvider` (existing, evolves) | Spawns workspaces as OS subprocesses inside the backend container (or wherever the FastAPI/worker process runs). Same OS-process-per-workspace model as the remote agent's workspace processes — just spawned by the backend process instead of by a remote supervisor. Implements the **exact same protocol** — same AgentCommands, same lifecycle, same single-flight, same recovery policy, same failure-report-precedes-disposal invariant. | Dev, E2E tests, single-tenant self-hosted (future). |
| `RemoteAgentWorkspaceProvider` (new in M05) | Dispatches via `core/agent_gateway` to a customer-deployed agent. Agent's supervisor process spawns the workspace OS process. | Production, multi-tenant. |

Per-org config in org settings: `workspace_provider: in_memory | remote_agent`.

**Both providers use OS-process workspaces.** The difference is who spawns them and where IPC goes:
- `InMemoryWorkspaceProvider`: backend process spawns a workspace OS process directly via `os.exec`. IPC is pipes between backend and workspace process (same shape as supervisor↔workspace in the remote model).
- `RemoteAgentWorkspaceProvider`: backend dispatches an AgentCommand over HTTP to the remote agent's supervisor, which spawns the workspace process.

Keeping the spawn model identical across providers is what makes the contract uniform. The in-memory provider doesn't get a free pass on invariants because it uses the same primitive (OS process + pipe IPC + same workspace-process subcommand of the same agent binary, possibly via `apps/agent/` Go binary even in the in-memory case — open TBD whether we reuse the Go binary or implement an equivalent in Python).

Implication: E2E tests can use `in_memory` and validate every rule, because the rules live above the provider. Eventually in prod the `in_memory` option gets disabled at the org-settings allowlist level. We don't delete the implementation — it's too useful for E2E.

## WorkspaceAgent process model (the remote agent only)

OS-process isolation, not goroutines.

- **Supervisor process** (`agent supervisor` subcommand): one per ECS task. Holds the only network connection to yaaos control plane. Runs long-poll workers, spawns/kills workspace processes, routes AgentCommands, forwards events, heartbeats, runs disk janitor + reconciliation.
- **Workspace process** (`agent workspace --id <uuid>` subcommand of the same binary): one per active workspace. Spawned by the supervisor via `os/exec` on `AgentCommand: CreateWorkspace`; killed on `CleanupWorkspace`. Owns its workspace directory. Spawns the CodingAgent (e.g. Claude Code), git, tests as its children — when the workspace process dies, all its children die.

**IPC:** pipes between supervisor and workspace process. AgentCommands written as JSON-newline to the workspace's stdin; events read as JSON-newline from stdout. stderr captured for supervisor-local logs.

**Why OS processes:**
- Real memory + CPU isolation; one workspace's subprocess can't reach into another's heap.
- Crash isolation; a segfault in a workspace doesn't kill the supervisor.
- Foundation for future per-workspace sandboxing without rearchitecting.
- The supervisor stays small and stable; volatile code paths live in disposable processes.

**Provisioning cost:** spawning a workspace process adds ~1s vs. a goroutine. Acceptable; workspaces live for minutes.

## CodingAgent isolation (what M05 ships)

Within a single WorkspaceAgent instance (single customer, single VPC), multiple workspace processes run concurrently. They share a Linux UID. M05's isolation is intentionally lightweight + distro-agnostic + low-maintenance — three mechanisms only:

| Mechanism | What it does | Distro-agnostic? |
|---|---|---|
| **Path validation in Go** | Supervisor validates every `WriteFiles` path stays under `<workspace_dir>/`. Subprocess `cwd` is always set to the workspace dir. Periodic symlink-out scan in the disk janitor. | ✅ Pure Go, stdlib. |
| **Container filesystem read-only except `/var/agent/workspaces/`** | Set in the published Dockerfile + ECS task definition. CodingAgent subprocess cannot mutate anything outside the workspace tree. | ✅ Mount config. |
| **`os.RLimit` per workspace process** | Memory, CPU time, open files, max file size caps applied at workspace-process spawn. Limits "screws up peers via resource exhaustion." | ✅ Pure Go, stdlib. |

**What this catches:** accidents (buggy CodingAgent writes to wrong path), resource hogging, container-wide damage.

**What this does not prevent:** a determined malicious subprocess inside a workspace process can still read/write peer workspace dirs (same UID). This is bounded to a single customer (WorkspaceAgent instances are single-tenant by deployment), but is an acknowledged limitation. Per-workspace UID, landlock (Linux 5.13+), Linux namespaces, and seccomp are tracked as **near-term post-M05 hardening items** — none ship in M05 because they add real ops complexity for marginal gain at the POC stage.

The `docs/system-security.md` doc (written in Phase 10) states this honestly: "WorkspaceAgent isolates workspaces at the OS-process level, not the UID level. A compromised CodingAgent subprocess in one workspace could in principle interfere with peer workspaces on the same WorkspaceAgent instance. This is bounded to a single customer."

## Three liveness signals

Distinct on purpose. Never conflated.

| Signal | What it asserts | Source | Cadence | Failure action |
|---|---|---|---|---|
| **Agent liveness** | Supervisor up, network reachable, can accept commands. | Supervisor → `POST /v1/agents/{id}/heartbeat`. Includes inventory of workspaces it currently owns (reconciliation channel). | 30s | Silent > 90s → all its workspaces marked `agent_unreachable`; in-flight AgentCommands fail with `agent_lost`. |
| **Workspace health** | Workspace process alive, in known state, ready for AgentCommands. | Per-workspace status field carried inside the agent heartbeat inventory. | Implicit. | Failure → cleanup + re-provision (per disposable-workspaces rule). |
| **AgentCommand progress** | This AgentCommand hasn't hung. | Status events from the workspace process on state changes; wall-clock timeout in AgentCommand payload. | No per-command heartbeat in POC. | Wall-clock exceeded → supervisor kills workspace process; emits `command_timeout`. |

## Workspace lifecycle

```
requested → provisioning → ready ⇄ busy → terminating → gone
                ↓             ↓        ↓
              (failed) ──→ cleanup (best-effort) ──→ gone
```

- `gone` is the only terminal state.
- No `degraded` state. Failure goes straight through cleanup to `gone`.
- An AgentCommand transitions a workspace `ready → busy → ready`. Single-flight; many AgentCommands per workspace sequentially.
- Any non-terminal state can carry an `agent_unreachable` flag (transient). Past a threshold → `orphaned` (terminal, equivalent to `gone` for accounting).

## Single-flight per workspace

At most one AgentCommand in flight per workspace at any moment. Enforced in two places.

**Control plane (primary).** Workspace row has a nullable `current_command_id` column. Dispatch to workspace `W` is an atomic claim:

> `UPDATE workspaces SET current_command_id = $cmd WHERE id = $W AND current_command_id IS NULL` — zero rows updated means another dispatcher won; don't send.

Terminal event from the agent clears the field. **No separate commands table** in M05 — AgentCommands are ephemeral, live in `core/agent_gateway`'s in-memory per-agent queue. Backend restart drops in-flight commands; reconciliation via the next heartbeat rebuilds state.

**Agent (defense in depth).** The supervisor maps `workspace_id → workspace_process`. Each workspace process has one command pipe and processes one command at a time by construction. If a second command somehow arrives for an already-busy workspace, the supervisor emits a `workspace_busy` event and drops the command. Control plane reconciles.

## Workspace-to-workflow binding (M05)

A workspace is bound to exactly one workflow execution for its lifetime. Never shared across tickets, never shared across workflows.

Schema choice that keeps future relaxation add-only: workspaces table has a nullable `current_holder_workflow_id` column (not a hard FK). M05 invariant: this is set at workspace creation and cleared on cleanup, equal to the creating workflow execution's lifetime. Future relaxation (workspace reuse across workflows on the same PR): nullable when a workflow releases-but-doesn't-cleanup; provisioning policy can claim an unheld workspace by setting the field. Schema doesn't change; only the population pattern does.

## Disposable workspaces + recovery

Workspaces are disposable. Any unexpected failure → cleanup → new workspace.

**But the control plane tries to save first.** Each failure event carries a `reason` enum. `core/workspace` has a policy mapping reason → recovery AgentCommand, applied before dispose-and-replace.

Initial policy (will grow as real failure modes appear):

| Reason | Recovery |
|---|---|
| `auth_expired` | Issue `RefreshWorkspaceAuth` to the workspace; retry original AgentCommand. |
| everything else | Dispose + provision new workspace + re-dispatch. |

Recovery attempts and outcomes are audit-logged. From the workflow engine's perspective, recovery is internal to one WorkflowCommand execution.

## Cleanup failsafes (belt-and-suspenders)

1. **Idempotent cleanup commands.** Re-delivery is a no-op once workspace is `gone`.
2. **TTL.** Every workspace carries `expires_at ≤ created_at + 1h`. Agent unconditionally cleans up past expiry.
3. **Idle timeout.** Workspace carries `max_idle_seconds`. Agent cleans up after that much idle.
4. **Startup reconciliation.** Supervisor boots → inventories `/var/agent/workspaces/`, reports in first heartbeat. Control plane returns "delete these" for any it doesn't recognize.
5. **Disk sweep.** Slow background pass in the supervisor: any directory whose UUID isn't in the in-memory workspace table → force delete.
6. **Agent-loss recovery.** Control plane marks unreachable agents' workspaces `orphaned` after threshold. If the agent comes back it gets cleanup commands. If not, Fargate eventually reclaims storage.
7. **Audit trail.** Every workspace state transition writes an audit row.

## Invariant: failure report precedes disposal

Before any workspace disposal — `CleanupWorkspace`, TTL/idle, crash-handler, recovery-driven — a terminal event must be emitted capturing failure reason, subprocess exit codes, last error message, and tail of the workspace process's internal log. Best-effort: if the control plane is unreachable, the event is queued for retry and disposal proceeds anyway (debuggability falls back to supervisor-local logs).

## Protocol shape (AgentCommands)

Five HTTP endpoints + one WebSocket. Outbound TLS connections from the WorkspaceAgent's supervisor process. All paths under `/v1/`.

| Endpoint | Purpose | Transport |
|---|---|---|
| `POST /v1/identity/exchange` | sigv4-signed STS request → short-lived bearer (Vault AWS auth pattern). | HTTPS |
| `POST /v1/agents/{id}/heartbeat` | Supervisor liveness + workspace inventory (reconciliation). | HTTPS |
| `POST /v1/agents/{id}/commands/claim` | Long-poll (~30s, max 55s) → returns one AgentCommand at a time. | HTTPS long-poll |
| `POST /v1/workspaces/{id}/events` | Workspace state transitions; scoped to a `command_id` for ack. | HTTPS |
| `POST /v1/commands/{id}/events` | AgentCommand progress + terminal result. | HTTPS |
| `WSS /v1/agents/{id}/activity` | High-frequency CodingAgent ActivityEvent streaming back to control plane. **Separate from the command-and-control endpoints to keep them independent.** | WebSocket |

**Channel separation rationale:** command-and-control (claim/heartbeat/events) is request-response shaped, low frequency, naturally HTTPS. Activity events are push-shaped, high frequency, naturally streaming. Mixing them on one channel forces awkward tradeoffs; separating them gives each pattern the transport it wants.

**AgentCommand kinds (M05 + slot for future per-CodingAgent invokers):**

- `CreateWorkspace(workspace_id, repo, history, auth, ttl, max_idle, traceparent)` — supervisor spawns a new workspace OS process and clones the repo.
- `WriteFiles(workspace_id, files: [{path, content, mode}], traceparent)` — supervisor materializes content (e.g. yaaos skills) into the workspace dir before invocation. **`path` is workspace-relative** (e.g. `"skills/architecture.md"`). Path validation (workspace-relative-only; no `..` escape; no absolute paths) is enforced by the Go supervisor before writing. The Workspace Protocol's `write_text(path, content)` accepts relative paths and resolves them against the workspace root — callers never see the absolute filesystem path.
- `RefreshWorkspaceAuth(workspace_id, new_token, traceparent)` — rotates the installation token in an existing workspace process.
- `InvokeClaudeCode(workspace_id, command_id, invocation, mcp_servers, limits, result_spec, traceparent)` — runs Claude Code in the workspace. Carries Claude-Code-specific payload shape (CLI flags, Anthropic auth, MCP configs).
- *(Future: `InvokeCodex`, `InvokeAider`, etc. — each typed for its CodingAgent's auth/params. Slow-changing surface; adding one is a customer-visible WorkspaceAgent rollout.)*
- `CleanupWorkspace(workspace_id, traceparent)` — supervisor terminates the workspace process and removes its directory.

**Wire schema stability is tied to the set of CodingAgents we support, not to backend workflow logic.** WorkflowCommands change freely without affecting deployed WorkspaceAgents; AgentCommand changes force customer rollouts and are made deliberately.

Every AgentCommand and AgentEvent carries `traceparent` (W3C trace context) so spans nest correctly across the wire.

**Concurrency:** default 4 workspaces per agent. Configurable up to ~10–20 on larger ECS task sizes. Each free slot in the supervisor issues its own long-poll. ECS service auto-scales tasks above sustained load.

**Stale-claim guard:** event endpoints return `410 Gone` when the agent's `command_id`/`attempt` doesn't match current control-plane state. Agent abandons silently.

## Per-AgentCommand restart safety

| AgentCommand | Restart-safe? | Notes |
|---|---|---|
| `CreateWorkspace` | yes | New workspace ID per retry; no observable leak on retry of a failed create. |
| `WriteFiles` | yes | Overwrites by path; idempotent. |
| `RefreshWorkspaceAuth` | yes | Rotates the in-memory token in the workspace process; idempotent. |
| `InvokeClaudeCode` | yes (at user-visible level) | Re-running a review regenerates findings; the backend's `PostFindings` step dedups via `(finding_id, external_thread_id)` before posting to GitHub, so duplicates don't surface in VCS. |
| `CleanupWorkspace` | yes | Re-delivery is a no-op once `gone`. |

Future AgentCommands (e.g. anything with VCS-write side effects) must declare this property when added.

## Secrets handling

Different processes hold different secrets. The supervisor is the trusted shell; the workspace process is the disposable executor.

| Credential | Supervisor has? | Workspace process has? | Source | Scope |
|---|---|---|---|---|
| Yaaof control plane bearer | yes (in memory, refreshed proactively) | **no** | Identity exchange | All `/v1/...` endpoints |
| GitHub installation token | yes (forwards to workspace) | yes (env at spawn) | Minted by control plane per workspace | Single repo, ~1h |
| Anthropic API key (BYOK) | yes (inherits from ECS env) | yes (forwarded to Claude Code via env) | Customer Secrets Manager → ECS env | LLM API only |
| MCP proxy credentials (M04) | yes (in AgentCommand payload) | yes (in Claude Code MCP config) | Control plane in `InvokeClaudeCode` payload | Tool access via M04 proxy |

**Critical property:** the workspace process has no credentials for the yaaos control plane API. Findings cross the trust boundary only through the supervisor, which is the audited piece.

**Logging discipline:** secrets wrapped in a redacting type from day one; subprocess command lines and env vars never logged verbatim.

## Trust boundary

- **Crosses into yaaos:** findings, structured supervisor telemetry, workspace + AgentCommand state events, OTel spans.
- **Stays in customer VPC:** all source code, all diffs, all subprocess stdout/stderr (Claude Code, git, tests). Subprocess output may eventually ship to the customer's own observability stack (configured separately) but never to yaaos.
- **Crosses to the M04 MCP proxy (not yaaos):** tool-call traffic from Claude Code in workspace processes.

## Tracing (OpenTelemetry)

End-to-end distributed tracing from webhook arrival to GitHub comment posted. One trace ID covers the entire journey.

### Span hierarchy

- **Workflow execution span** — created when `core/workflow.start()` is called. Attributes: `workflow.name`, `workflow.version`, `ticket.id`, `ticket.type`. Spans the entire workflow including HITL waits.
- **WorkflowCommand step span** — child of the workflow span. One per step execution (including retries). Attributes: `step.id`, `step.kind`, `step.attempt`, `step.outcome`.
- **AgentCommand span** — child of the step span (when the WorkflowCommand issues one). Attributes: `agent_command.kind`, `agent_command.id`, `workspace.id`, `agent.id`.
- **Wire spans** — propagated via `traceparent` header (and `traceparent` field in payloads). Agent supervisor creates spans for claim, dispatch, event-forward. Workspace process creates spans for clone, invocation, subprocess.
- **Subprocess spans** — workspace process exports `TRACEPARENT` / `TRACESTATE` env to Claude Code; if subprocess emits OTel it nests correctly. Otherwise the workspace-process span covers it.

### Cross-process propagation

- Backend → agent: `traceparent` in AgentCommand payload.
- Supervisor → workspace process: `TRACEPARENT` / `TRACESTATE` in env at spawn.
- Workspace process → Claude Code subprocess: same env vars.

### Persistence of trace context

- `WorkflowExecution.otel_trace_context` field stores serialized W3C trace context. Survives backend restarts. Restored when a `core/tasks` task picks up the next step.

### SDK wired without exporter (M05 default)

OTel is NOT currently wired in the codebase (audit confirmed: `core/observability` exports only `spawn`, `active_task_count`, `configure`, `get_logger`). M05 wires it as **Phase 0c** with this profile:

- **TracerProvider** configured in `core/observability.configure()`.
- **W3C TraceContext propagator** registered globally.
- **FastAPI + asyncpg auto-instrumentation** for ASGI request spans + DB spans.
- **structlog processor** that pulls active span context onto every log record (trace_id, span_id as standard fields). Logs carry trace context even without span export.
- **No `SpanExporter` configured in production.** Spans are created and discarded at end-of-span. The TracerProvider has no exporter chain attached.
- **Tests use `InMemorySpanExporter`** for assertions about span shape.

Rationale: customer (yaaos team) will hook up Datadog (or similar) later. Until then, no observability backend is configured. The SDK wiring is what enables traceparent propagation and trace_id correlation in logs *now*; adding an exporter later is a one-line config change.

### Go WorkspaceAgent OTel

Same approach on the agent side:

- `go.opentelemetry.io/otel` SDK installed.
- `TracerProvider` with no exporter.
- `propagation.TraceContext` set as global propagator.
- Supervisor extracts `traceparent` from inbound AgentCommand payloads + WebSocket activity messages, creates child spans under that parent.
- Workspace process inherits via `TRACEPARENT` / `TRACESTATE` env vars at spawn.
- Same "wire SDK, defer exporter" pattern. When backend hooks up Datadog, agent's exporter config is updated symmetrically (customer SREs configure on their side — agent SDK reads `OTEL_EXPORTER_OTLP_ENDPOINT` env var).

### Span structure end-to-end (so cross-process traces nest correctly)

- **Workflow span** — created at `core/workflow.start()`. Lifetime spans the workflow (including HITL waits + awaiting_agent pauses). Persisted via `otel_trace_context`.
- **Step span** — child of workflow. Created in `start_step` per execution.
- **AgentCommand span** — child of step span (when the step issues an AgentCommand). traceparent extracted from this span, threaded into AgentCommand payload + WebSocket messages.
- **Wire spans (agent side)** — Go agent creates child spans under the extracted parent. Supervisor span (per AgentCommand handled) + workspace-process span (clone, invoke, cleanup).
- **Subprocess spans** — workspace process exports env vars; if Claude Code emits OTel, it nests automatically. Otherwise the workspace-process span covers it.

Even with no exporter configured, the trace IDs are consistent across services. Once Datadog (or whatever) is hooked up, historical traces don't exist but forward-going ones show full cross-service depth.

## Heartbeat / reclaim defaults (POC)

- Supervisor heartbeat: 30s cadence, 90s reclaim threshold (3 misses).
- All values payload/config controlled — these are defaults, not constants.

## Activity streaming (CodingAgent → UI)

CodingAgents (Claude Code etc.) emit `ActivityEvent`s describing what they're doing (e.g. "reading file X", "considering tests"). The existing UX streams these to the UI live via SSE. M05 preserves this behavior across the new process boundaries.

### Trust boundary invariant

`ActivityEvent`s are **metadata only — never source content.** Action names, file paths, line numbers, status changes are allowed. File body snippets are forbidden. The CodingAgent's pre-rendering layer (in `domain/coding_agent` and equivalent in the workspace process) enforces this before any event leaves the workspace.

### Path

```
CodingAgent stdout → workspace process pre-renderer → ActivityEvent (typed)
   ↓
   (in-memory pipe) → WorkspaceAgent supervisor
   ↓
   (WebSocket /v1/agents/{id}/activity, batched ~250ms) → backend
   ↓
   core/agent_gateway receives → core/sse_pubsub.publish(workflow_id, event) → Redis pub/sub channel
   ↓
   Any backend instance with an SSE subscriber for that workflow_id receives via Redis → forwards to the UI's SSE connection
```

### Batching

Activity events are high-frequency (1–10/sec per active CodingAgent). The supervisor batches them client-side at ~250ms intervals, sending an `ActivityBatch` over the WebSocket per tick. Reduces wire chatter; UI latency stays well under human-perceptible threshold.

### `core/sse_pubsub` responsibilities

- Thin wrapper around the Redis client's pub/sub API.
- `publish(channel: str, event: dict)` — called by `core/agent_gateway` when activity events arrive.
- `subscribe(channel: str) -> AsyncIterator[dict]` — used by SSE handlers in `web.py` to feed a client connection.
- Channel naming: `activity:{workflow_execution_id}`. SSE subscribers know which workflow they're watching; backend instances scope their subscriptions accordingly.

### In-memory provider path

When `WorkspaceProvider = in_memory`, the workspace process is a child of the taskiq worker, not on a customer machine. Events still go through the same pipe → backend boundary, but the "backend" in this case is the taskiq worker process. The worker publishes directly to Redis (same `core/sse_pubsub.publish`). The web instance's SSE handler subscribes to the same channel via Redis. Identical UI experience either way.

### Persistence invariant

**Activity events are never persisted to any database, anywhere in the system.** They exist only in flight: workspace process → wire (WebSocket) → backend `core/sse_pubsub.publish` → Redis pub/sub → SSE → UI. After delivery they're gone. No `activity_log` column on any table. No `workflow_activity_events` table.

Volume rationale: ~5 events/sec/CodingAgent × N concurrent reviews = real network volume but trivial for Redis pub/sub. Persisting them would mean ~1.5–2 GB/day per active customer at modest scale. Cost without value: nobody scrolls through historical agent activity, and debugging needs are covered by OTel spans + customer-side workspace process logs + structured error events on workflow audit.

### What's deliberately not in M05

- No replay of historical activity (UI shows live activity only; reload = empty until next event).
- No "snapshot at SSE-reconnect time" of recent activity (best-effort: missed events stay missed). State of record stays in DB.
- No per-event ack/retry. Activity is best-effort fire-and-forget. State-of-record (findings, workflow state) is in DB and is not best-effort.
- **No activity-event persistence in any form.** If a future use case forces this (analytics dashboard, ML training), it's a separate explicit decision — not a sneak-in via "we have a log."

### Demand-pull streaming (only ship events when someone's watching)

Activity isn't always-on. The WorkspaceAgent only forwards events for workspaces with at least one UI subscriber. Mechanism:

- The activity WebSocket is **bidirectional**:
  - **Backend → WorkspaceAgent:** `{type: "subscribe", workspace_id: "..."}` / `{type: "unsubscribe", workspace_id: "..."}` control messages.
  - **WorkspaceAgent → backend:** `{type: "activity_batch", workspace_id: "...", events: [...]}` payload.
- The WorkspaceAgent maintains an in-memory `subscribed_workspaces: Set[workspace_id]`. Events from any workspace process are dropped unless its workspace_id is in the set.
- The backend's `core/agent_gateway` tracks `subscriber_counts: Map[workflow_execution_id, int]`. SSE handlers increment on connect, decrement on disconnect. Transition `0 → 1` sends `subscribe` to the relevant WorkspaceAgent; transition `1 → 0` sends `unsubscribe`.
- **No activity events flow** until a user opens the UI. Webhook-triggered reviews that nobody watches generate zero wire traffic for activity (saves customer VPC egress + yaaos ingress + Redis pub/sub volume).
- **Latency to start streaming after UI opens:** sub-second (WebSocket roundtrip + ~250ms batch interval).

### WebSocket connection lifecycle (outbound-only direction preserved)

- **WorkspaceAgent opens the WebSocket** after `identity_exchange` at startup. Bearer token presented in `Authorization` header on the upgrade request.
- **TCP direction is outbound-only.** Subscribe/unsubscribe messages from the backend ride down the WorkspaceAgent's existing outbound connection; no inbound TCP from yaaos.
- **Stays open for supervisor lifetime.** On disconnect, agent reconnects with exponential backoff (capped at single-digit seconds).
- **Reconnect handling backend-side:** `core/agent_gateway` looks up "workflows with live SSE subscribers whose workspaces this agent currently owns" and re-sends subscribe messages on the new connection. WorkspaceAgent rebuilds its `subscribed_workspaces` set.
- **Drop detection backend-side:** Starlette raises `WebSocketDisconnect`; handler clears per-connection state, marks agent unreachable per existing heartbeat-loss path.

### Library choices

- **Backend (Python):** FastAPI / Starlette native WebSocket support. No external library. Authentication on upgrade reads bearer from `websocket.headers["authorization"]`; rejects with `await websocket.close(code=4401)` if invalid.
- **WorkspaceAgent (Go):** [`github.com/coder/websocket`](https://github.com/coder/websocket) (formerly `nhooyr.io/websocket`). Modern context-aware API, well-maintained by Coder Inc. Simpler than `gorilla/websocket` for the use case.

### uvicorn / proxy keepalive

WebSocket idle connections through intermediate proxies (notably AWS ALB, default 60s idle timeout) are silently killed without ping/pong frames. Configure uvicorn with `--ws-ping-interval=30 --ws-ping-timeout=10`. Captured in `docs/setup.md`.

## End-to-end PR review flow (M05 reference)

Used to validate every layer of the design.

1. **Webhook arrives** at `domain/intake`'s existing handlers. Signature verified, payload validated, dedup + filters applied (existing logic), idempotency key derived.
2. **Ticket created.** `domain/tickets.create(type=pr_review, payload={repo, pr_number, base_ref, head_ref, installation_id, trigger_reason}, idempotency_key=...)`. Returns `ticket_id`.
3. **Workflow started.** `core/workflow.start(workflow_name="pr_review_v1", ticket_id=...)`. Engine looks up the workflow definition (registered by `domain/reviewer` at startup), creates `WorkflowExecution`, opens a new OTel span, enqueues the initial `route_workflow` task.
4. **Webhook returns 200** with `{ticket_id}`. Synchronous.
5. **Step 1: `CheckShouldReview`** (Local WorkflowCommand in `domain/reviewer`). Worker checks PR draft status, skip labels, external-contributor approval, org config. If skip → workflow transitions to `complete_workflow` (no workspace provisioned, no token spent). Records `Review` row with `state=skipped`. If proceed → next step.
6. **Step 2: `ProvisionWorkspace`** (Workspace WorkflowCommand in `core/workspace`). Calls configured `WorkspaceProvider`. Issues `AgentCommand: CreateWorkspace` → WorkspaceAgent (or in-memory equivalent) spawns workspace process + clones repo. Then issues `AgentCommand: WriteFiles` → workspace receives yaaos skills package. Both must succeed. Records `Review` row with `state=running`. Step outputs `workspace_id`.
7. **Step 3: `CodeReview`** (Workspace WorkflowCommand in `domain/reviewer`). Imports invocation machinery from `domain/coding_agent`. Assembles `AgentCommand: InvokeClaudeCode` payload (directive, MCP servers, limits, result spec, traceparent). Calls `core/workspace.invoke(workspace_id, payload)`. WorkspaceAgent's workspace process runs Claude Code → findings (FindingDrafts) returned via terminal event. Runs admission pipeline. For each surviving draft: `INSERT INTO findings ... ON CONFLICT (pr_id, fingerprint) DO NOTHING RETURNING id`. The returned subset = newly-raised findings (passed as step output to PostFindings).
8. **Step 4: `PostFindings`** (Local WorkflowCommand in `domain/reviewer`). For each newly-raised finding: call `vcs.post_review()` → receive GitHub thread id → `UPDATE findings SET external_thread_id = ?`. Idempotent on retry: skip findings whose `external_thread_id` is already set.
9. **Step 5: `CleanupWorkspace`** (Workspace WorkflowCommand in `core/workspace`). Issues `AgentCommand: CleanupWorkspace` → workspace process terminated, directory removed.
10. **Workflow done.** Engine marks execution `done`, marks `Review` row `state=completed`, marks ticket `done`, closes workflow span.

The `pr_review_v1` workflow definition lives at `domain/reviewer/workflows/pr_review.py`. ~25 lines of declarative data plus the five WorkflowCommand implementations co-located in `domain/reviewer/commands.py`.

## M05 extension of existing `domain/intake` + `domain/tickets`

Both modules **already exist** in the codebase and are fully realized — they're not new in M05.

**`domain/intake` current state (preserved by M05):**
- Routes VCS webhook events (PR opened, sync, comment, etc.).
- Applies filters: fork, bot author, trivial diff, size thresholds.
- Parses `@yaaos rereview` comments.
- Syncs PR metadata bidirectionally with `tickets` and `pull_requests`.

**`domain/intake` M05 additions:**
- After existing routing decides "this signal produces a ticket," call `domain/tickets.create(...)` (new signature; existing call may differ) and `core/workflow.start("pr_review_v1", ticket_id)`.
- That's it. No workflow definitions, no engine logic, no workspace knowledge.

**`domain/tickets` current state:**
- Ticket aggregate with state machine `in_review → complete | abandoned`.
- HTTP routes `GET /api/tickets`, `GET /api/tickets/{id}`, `GET /api/tickets/{id}/audit`.
- `TicketStatusChanged` events.

**`domain/tickets` M05 additions:**
- Add `type text not null` column to `tickets` table (`pr_review` is the only value in M05).
- Reconcile state machine: existing `in_review|complete|abandoned` + new requirements (`pending|running|done|failed|cancelled`). Likely outcome: existing `in_review` → new `running`; `complete` → `done`; `abandoned` → `cancelled`; new state `failed` added; new state `pending` added (between create and workflow-start). Concrete migration TBD when implementing.
- Add `idempotency_key text unique` column.
- Add `current_workflow_execution_id uuid` nullable column (FK to `workflow_executions`) for the canonical workflow execution.
- `domain/tickets.create(type, payload, idempotency_key) -> ticket_id` new public API method.

**State machine reconciliation is the largest piece** of this extension. Sketch (refine when implementing):

```
[new]    pending      → workflow not yet started (transient — should immediately transition)
[merged] running      ⇄ awaiting_human    (engine active; in_review was the Gen 1 name)
[merged] done         ← terminal success  (was: complete)
[new]    failed       ← terminal failure  (no Gen 1 equivalent — used to leave row stuck)
[merged] cancelled    ← terminal cancel   (was: abandoned)
```

Migration: existing rows mapped via the above renames. New `pending` and `failed` states are net-new — no historical rows to migrate into them.

## Data model

New tables (consolidated into a single named migration `014_create_all_m05` following project convention; `_MIGRATIONS` tuple in `core/database/service.py`).

### Workflow / orchestration tables (new in M05)

- **`tickets`** — `id uuid pk`, `org_id uuid not null`, `type text not null` (e.g. `pr_review`), `state text not null` (`pending|running|done|failed|cancelled`), `payload jsonb not null`, `idempotency_key text unique`, `created_at`, `updated_at`. Index on `(org_id, state, created_at)`.
- **`workflow_executions`** — `id uuid pk`, `ticket_id uuid not null references tickets(id)`, `workflow_name text not null`, `workflow_version int not null`, `state text not null` (`pending|running|awaiting_agent|awaiting_human|done|failed|cancelled`), `current_step_id text`, `pending_agent_command_id uuid` (nullable; set when `state=awaiting_agent`, cleared when terminal event handled), `step_state jsonb not null default '{}'`, `cancel_requested boolean not null default false`, `otel_trace_context text`, `created_at`, `updated_at`. Index on `(state)` for `core/tasks` pickup; index on `(pending_agent_command_id)` for event-arrival lookups.
- **`pending_human_decisions`** — `id uuid pk`, `workflow_execution_id uuid not null references workflow_executions(id)`, `question_payload jsonb not null`, `resolution_payload jsonb`, `resolved_at timestamptz`, `created_at`. Index on `(workflow_execution_id, resolved_at)`.
- **`workspaces`** — `id uuid pk`, `org_id uuid not null`, `provider text not null` (`in_memory|remote_agent`), `workspace_agent_id uuid` (nullable; null when in_memory), `repo text`, `state text not null`, `current_command_id uuid`, `current_holder_workflow_id uuid`, `expires_at timestamptz not null`, `max_idle_seconds int not null`, `created_at`, `updated_at`. Indexes on `(workspace_agent_id, state)`, `(current_holder_workflow_id)`, `(expires_at)`.
- **`workspace_agents`** — **per-pod instance row** (not per-customer-registration). `id uuid pk`, `org_id uuid not null`, `agent_pod_id uuid not null` (generated by each pod at startup, persisted locally), `iam_arn text not null` (the org's registered ARN; same value for all pods sharing it), `version text`, `last_heartbeat_at timestamptz`, `state text not null` (`reachable|unreachable`), `created_at`. UNIQUE `(org_id, agent_pod_id)`. Index on `(org_id, last_heartbeat_at)` for connection-status aggregation.
- **`org_settings`** (existing) gains `workspace_provider text` (`in_memory|remote_agent`) + `registered_iam_arn text` (nullable; set when provider is `remote_agent`). Single ARN per org — multi-pod scaling happens at the ECS service level (same role, multiple tasks).
- **`outbox_entries`** — `id uuid pk`, `kind text not null` (initially `taskiq_enqueue`), `payload jsonb not null`, `created_at timestamptz`, `dispatched_at timestamptz` nullable, `attempt int default 0`, `last_error text` nullable. Index on `(dispatched_at, created_at)` for drain query, `(dispatched_at)` for retention sweep.

### Reviewer tables (Gen 1 dropped; Gen 2 simplified to the minimum needed for cross-review dedup)

- **`reviews`** (new, replaces dropped `review_jobs`) — `id uuid pk` (same id as the WorkflowExecution row that drives it, FK), `pr_id uuid not null`, `org_id uuid not null`, `trigger_reason text not null` (`pr_ready|pr_synchronized|rereview_command|ui_button`), `state text not null` (`running|completed|failed|skipped|cancelled`), `started_at`, `finished_at`, `findings_count int default 0`. Index on `(pr_id, started_at desc)`.
- **`findings`** (simplified — no FindingState enum) — `id uuid pk`, `pr_id uuid not null`, `fingerprint text not null`, `first_review_id uuid not null references reviews(id)`, `external_thread_id text` (nullable; GitHub thread id once posted), `severity text not null`, `body text not null`, `code_anchor jsonb not null` (path + line range), `created_at`. **UNIQUE (pr_id, fingerprint)** for cross-review dedup.

### Dropped (M05 cutover)

- `review_jobs` — Gen 1 table; new `reviews` replaces it. Historical data discarded; no backfill, no compat layer.
- `finding_observations`, `comment_threads`, `comment_messages`, `acknowledgment_decisions` — Gen 2 had these tables planned; **M05 doesn't build them.** Cross-review dedup is achieved via UNIQUE `(pr_id, fingerprint)` on `findings`. Verify-fix, stale-check, reply-classification are explicitly future work, not M05.

Audit (`audit_entries`, owned by `core/audit_log`) — new `kind` values: `workspace.created`, `workspace.cleanup`, `workspace.failed`, `workflow.started`, `workflow.step.<id>`, `workflow.done`, `workflow.failed`, `workspace_agent.connected`, `workspace_agent.lost`, `review.started`, `review.completed`, `review.failed`, `review.skipped`. No schema change.

## Risks

- **`InMemoryWorkspaceProvider` contract drift.** The risk of two providers behind one interface is that the in-memory one cuts corners on invariants ("it's just a dev tool"). Mitigated by running the same E2E suite against both providers — if the contract drifts, tests break.
- **`core/tasks` broker outage.** All workflows pause; reviews queue up. Backend stays responsive (FastAPI doesn't depend on broker for webhook ack). Recovery: broker back → workers resume → pending tasks drain. Acceptable degradation.
- **Workflow long-tail.** A workflow stuck in `awaiting_human` indefinitely consumes a row but no resources. Need future "stale HITL" cleanup policy. Not M05.
- **First-version OpenAPI schema lock-in.** Once customers run agents with v1 of the protocol, schema changes cost. `agent_version` field in identity exchange is the future hook; compatibility policy is a TBD strategic gap.
- **AgentCommand bursts to a busy workspace.** Single-flight + per-workspace serial pipe means burst → queue grows. We don't currently bound the queue per workspace. Operationally fine for M05 (workspaces only see commands from their bound workflow). Future "multi-holder" relaxation will surface this.
- **Recovery policy churn.** Initial policy table is tiny (`auth_expired` only). Real failure modes will surface in prod. The policy itself is data — growth is additive, not architectural.
- **OS-process IPC framing edge cases.** Pipe + JSON-newline has known sharp edges (partial reads, embedded newlines in error messages). Land a small framing library in `apps/agent/internal/ipc/` with thorough tests.
- **Redis as new infra.** M05 adds Redis as a hard dependency (task broker + SSE pub/sub). Customers running M05's backend run a Redis instance (ElastiCache or self-managed). Mitigation: standard infra; well-understood operationally. Docker-compose includes a real Redis container in local dev and CI — no mocking.
- **Outbox drain liveness.** If the outbox drain stops, enqueues silently pile up in `outbox_entries`. Workflows pause. Mitigation: drain emits liveness metric; alerting on "undrained outbox > N entries"; drain runs in the same worker process so worker liveness covers it.
- **Outbox drain duplicate dispatch.** If the drain dispatches to Redis then crashes before marking `dispatched_at`, the next drain re-dispatches the same task. Mitigation: task bodies are idempotent (look up state from DB); duplicate dispatch is safe.

## Open questions — strategic gaps (deferred design rounds)

Locked-aside topics that need their own design pass before M05 ships. Each is large enough to deserve a focused conversation.

### Image + protocol versioning (locked)

**Image registry:** AWS ECR Public Gallery. Free, no rate limits, native for customer ECS pulls.

**Tagging scheme:** `latest`, `vX`, `vX.Y`, `vX.Y.Z`. `vX.Y.Z` is immutable; others float. Customers free to pin any level (`vX.Y` recommended; `latest` permitted, documented as "trust us with auto-updates").

**Protocol-version structure:** `/v1/` URL prefix. **Agent major version locked to API version:** `1.x` ↔ `/v1`, future `2.x` ↔ `/v2`. Bidirectional contract: agent 1.x always supports all of `/v1`; backend on `/v1` always supports all 1.x agents.

**Within-major evolution rules (`/v1`-stable surface):**

- ✅ Adding optional fields to existing schemas (old agents ignore gracefully).
- ❌ Adding new AgentCommand kinds (would break old agents that don't recognize them) → requires major bump.
- ❌ Adding required fields → requires major bump.
- ❌ Removing fields or changing semantics → requires major bump.

`/v1`'s AgentCommand set is **frozen** at: `CreateWorkspace`, `WriteFiles`, `RefreshWorkspaceAuth`, `InvokeClaudeCode`, `CleanupWorkspace`. Any new kind = `/v2` + agent `2.x`.

**No capabilities array.** Agent connecting on `/v1` is trusted to support all of `/v1`'s AgentCommands. The `agent_version` field in identity exchange is informational only (UI, logging) — not a dispatch gate.

**No minimum-version floor; no force-upgrades.** Customers can run `1.x` agents indefinitely. When `/v2` ships, both protocols are maintained in parallel forever (or until empirically zero customers are on the old version). Maintenance cost is accepted as the price of the no-force-upgrade promise.

**Customer-visible state:** UI lists each registered WorkspaceAgent with its `agent_version` + a non-blocking "update available" indicator when a newer release exists. No mandatory upgrade prompts; no email about minimums.

**Discipline:** every new field added to a `/v1` schema must be `Optional[...]` in Pydantic + backend must handle the missing case explicitly. If we're tempted to require it, that's a `/v2` signal.

### Multi-tenancy + fairness

Control plane dispatches across N customers. One customer drops 50 PRs at once — does that block another customer's single PR? Need:

- Per-org concurrency caps on workflow executions (`core/tasks`-level or engine-level?).
- Fair scheduling across orgs (round-robin? weighted by tier?).
- Per-org workspace count caps (already implicit via per-agent capacity but not enforced cross-customer at the control plane).
- SLA story — what does "review will complete within X minutes" cost us?

### Customer-side observability + audit

What does the customer see about their own agent's activity? Their security/compliance teams will ask.

- Customer-visible audit log of their own agent's activity (UI surface).
- Per-customer metrics export (Prometheus / OTel-collector relay).
- Customer-readable structured logs (already emitted by supervisor; where do they land — customer's CloudWatch / S3 / their observability stack?).
- Admin actions like "view live agents," "view recent workspaces," "force-cleanup a workspace."

### MCP proxy interaction details (locked)

**Deployment:** yaaos-hosted (same as M04 — no customer-deployed MCP component). Customer's workspace process makes outbound HTTPS to yaaos's MCP proxy URL. Customer-hosted variant is a future option; not M05 default.

**Auth from workspace process:** per-`workflow_execution_id` short-lived bearer minted by yaaos. Inherits M04's `mcp_review_token` discipline — token bound to one workflow, hashed in DB, URL-path must match.

**Token flow:**
1. When the first `InvokeClaudeCode` step is about to dispatch in a workflow, `core/workspace` calls `domain/mcp_proxy.mint_token(workflow_execution_id)` → bearer.
2. Bearer + proxy URL + server name go into the `mcp_servers` field of the `InvokeClaudeCode` AgentCommand payload.
3. Workspace process writes `.mcp.json` (in workspace dir) with these configs.
4. Claude Code reads `.mcp.json`, makes JSON-RPC calls to `https://yaaos.example/api/mcp/{workflow_execution_id}/{server}` with the bearer.
5. MCP proxy validates bearer → workflow_execution_id; verifies URL-path match; looks up `mcp_credentials` for org; forwards to upstream Linear/Notion using org's service-account OAuth.
6. Audit row per JSON-RPC method (M04 behavior, unchanged).
7. On workflow terminal (`done`/`failed`/`cancelled`): `domain/mcp_proxy.revoke_token(workflow_execution_id)`.

**Token TTL:** `workflow_max_wall_seconds + 1h buffer`. No mid-workflow refresh — workflow's own wall-clock timeout fires before token expiry. Simpler than refresh, no new AgentCommand kind required.

**Data flow + trust boundary:**

MCP traffic flows through yaaos (request from workspace process → yaaos proxy → upstream service → yaaos proxy → workspace process). Contents are external-service data (Linear tickets, Notion pages, etc.) — NOT source code. Source code never enters the MCP path.

**Critical security property: MCP request/response data is never persisted by yaaos.** The proxy is purely in-memory forward — yaaos reads the request body, looks up org credentials, makes the upstream call, streams the response back to the caller. Only metadata (tool name, args_hash, result_summary) is written to `audit_entries`. Raw arguments and response bodies are not stored anywhere.

This property has to be load-bearing in `docs/system-security.md`:
- yaaos sees the traffic (in memory).
- yaaos does not retain the traffic (no logs, no DB rows, no caches).
- yaaos retains only structured audit metadata.

If a customer's security team needs zero-touch from yaaos for MCP traffic: that's the customer-hosted MCP proxy option (future).

**Latency:** customer VPC → yaaos's MCP proxy (HTTPS RTT) → Linear/Notion (HTTPS RTT) → back. Typically 200-500ms per tool call. A review makes a handful of calls; total latency contribution is sub-second to the review duration.

**Credential surface added:**
- Per-`workflow_execution_id` bearer (workspace process has it via `.mcp.json`; supervisor has it via the AgentCommand payload that arrived for that workspace).
- Org-level OAuth tokens for Linear/Notion: stored encrypted in yaaos (M04 `mcp_credentials` table); never leave yaaos.

## Open questions — implementation TBDs

Smaller items, resolve during implementation rather than design.

### Protocol details

- **TBD: full OpenAPI schemas.** Concrete request/response shapes for all five endpoints, AgentCommand discriminated union, AgentEvent schemas, traceparent fields, error envelopes.
- **TBD: AgentCommand acknowledgement model.** Does the agent ack synchronously and then send completion events, or does each command have one terminal event that doubles as ack?
- **TBD: idempotency keys.** Sketched as `(command_id, attempt)`. Confirm against reclaim semantics.
- **TBD: findings schema.** Deferred to implementation alongside `domain/reviewer`.

### Agent internals

- **TBD: Claude Code invocation details.** Headless mode flags, how the directive is passed (stdin, `--print`, prompt file), where lessons live on disk, MCP server configuration mechanism, structured output channel.
- **TBD: workspace filesystem layout.** Path conventions, per-workspace UID strategy, where Claude Code config lives within the workspace.
- **TBD: workspace process IPC framing.** Pipe + JSON-newline is the plan; message envelope, error framing, backpressure TBD.
- **TBD: workspace orphan handling.** Should the workspace process self-shutdown if the supervisor pipe closes? (Probably yes — pipe-close as a death signal.)

### Control plane changes

- **TBD: existing `core_workspace` extension specifics.** Decide when reading existing code.
- **TBD: existing `domain/reviewer` reshape.** What stays, what becomes WorkflowCommands, what's deleted.
- **TBD: existing `review_job` migration mechanics.** Conversion path from existing rows.
- **TBD: provisioning policy.** "Which agent gets the next workspace?" Initial: least-loaded among reachable agents.
- **TBD: reconciliation algorithm details.** Concrete logic for comparing expected vs. reported workspace inventory.
- **TBD: HITL UI surface.** Where users see pending decisions. Not exercised in M05 but the data model lands.

### Identity + secrets

- **TBD: AWS sigv4 verification flow on yaaos's side.** Library choice, signature replay semantics, ARN registration UI.
- **TBD: installation token rotation cadence.** Triggered on dispatch, or on a schedule?
- **TBD: secrets redaction implementation.** Concrete `secret.String` type for Go; logging hook that scrubs known-secret field names.

### Operations

- **TBD: agent image release process.** Public ECR vs. Docker Hub, tagging strategy.
- **TBD: local dev story specifics.** docker-compose stack composition (Postgres + backend FastAPI + backend worker + optional Go agent + fake-STS). STS-bypass dev-mode identity exchange details. Which provider is the local-dev default (`in_memory` favored for fast iteration).
- **TBD: e2e test story.** How `apps/e2e/` exercises full flow against both providers; parameterization mechanism.
- **TBD: observability beyond tracing.** Metric set, structured log field set, OTel exporter config.
- **TBD: taskiq + Redis schema setup.** taskiq stores task state in Redis (no Postgres tables for the queue itself). Schema concern is for `core/outbox` (`outbox_entries` table) which is in our own migration. Confirm no taskiq side requires DB setup beyond what's covered.
- **TBD: Postgres connection pool sizing under web + worker model.** Both processes share the database; pool sizing per process.
- **TBD: test infrastructure for taskiq tasks.** taskiq's `InMemoryBroker` for sync execution vs. spinning a real taskiq worker against test Redis. Pick once and document. Test Redis runs as a docker-compose service per the locked decision.
- **TBD: task discovery mechanism.** Side-import at worker boot via a known module (`apps.backend.app.tasks_registry`) vs. explicit registration. Pick the simpler one and write the assertion test.
- **TBD: HITL UI surface.** Data model lands in M05, no UI. Decide which milestone owns the UI. Until then, HITL workflows can be exercised in integration tests via the resume API endpoint, but no user-facing UI ships in M05.

### Failure-mode coverage

- **TBD: supervisor crash mid-cleanup walkthrough.** Failsafes cover it; nail the sequence.
- **TBD: network partition behavior rules.** Give-up thresholds.
- **TBD: disk-full / OOM behavior.** Reporting + recovery.

## Optimizations (deferred until measured)

- **Git worktree cache.** Single bare clone per `(customer, repo)` with per-workspace worktrees. Right primitive identified; adds complexity (object DB lock contention, submodules, LFS). Add when first customer has a large monorepo.
- **Workspace reuse across workflow executions on the same PR.** Schema (`current_holder_workflow_id` nullable) makes the future relaxation add-only. Add when measurement shows clone time is the bottleneck or customer experience demands ~instant follow-up reviews.
- **Per-workspace network egress restrictions.** Restrict workspace process egress to allowed hosts. Customer-side concern.

## Cross-references

- `apps/backend/docs/core_workspace.md` — current workspace module shape (extension target).
- `apps/backend/docs/core_audit_log.md` — `kind` value conventions (extended here).
- `apps/backend/docs/patterns.md` — bearer-token discipline (reused by the agent's per-AgentCommand attempt id) + advisory-lock pattern (reused by recovery refresh).
- `plan/notes/security-posture.md` — predecessor strategy doc; reconciled with this milestone.
- `plan/milestones/M04-mcp/` — MCP proxy that `InvokeClaudeCode` payloads reference.
