# core/tasks

> Durable task scheduler over taskiq + Redis with atomic-in-session enqueue.

## Purpose

Owns the `@task` decorator, the `TaskContext` shape every task body receives, and the `enqueue(task, args, *, session)` API. Atomic-in-session: enqueue writes a row to `outbox_entries` via [`core/outbox`](core_outbox.md), so the task is durable iff the caller's transaction commits. Hides taskiq imports from biz logic — swapping the scheduler is contained to this module.

## Public interface

Exports `task`, `enqueue`, `TaskContext`, `TaskRef`. See `apps/backend/app/core/tasks/__init__.py`.

- `@task(name, *, queue="default", max_retries=1)` — registers a task body; returns a `TaskRef` callers `enqueue` against.
- `enqueue(task_ref, args, *, session)` — writes a `taskiq_enqueue` outbox row in the caller's session. Returns the outbox row id. Required `session` — there is no fire-and-forget path.
- `TaskContext` — first positional arg of every task body. Carries `session`, `traceparent`, `attempt`, `job_id`. The wrapper (Phase 1) opens the session per-task and commits on success.
- `TaskRef` — frozen handle to a registered task name + queue + retry policy.

## Module architecture

### Core flow (Phase 0b scaffold)

1. Task author writes `@task("route_workflow") async def route_workflow(ctx, ...)`. The decorator registers the body in an in-process registry.
2. Caller inside a transaction: `await enqueue(route_workflow, args={...}, session=s); await s.commit()`. The outbox row is durable when the commit lands.
3. The drain (Phase 1's `apps/backend/bin/worker`) reads undispatched rows, dispatches `taskiq_enqueue` payloads to the broker, stamps `dispatched_at`.
4. A taskiq worker pops the task, opens a fresh DB session in the `TaskContext`, calls the registered body, commits on success or retries on raise per `max_retries`.

### Phase boundaries

- **Phase 0b (this commit)** — registry + `enqueue` writing outbox rows; no broker, no worker yet. Tests verify the in-memory registry + outbox row shape.
- **Phase 1** — taskiq broker configured against Redis; worker entrypoint at `apps/backend/bin/worker`; outbox drain wired to broker.

### Relationship to `spawn()`

[`core/observability.spawn()`](core_observability.md) stays for fire-and-forget request-scoped background work without durability needs. `core/tasks` is for work that must survive restarts, has retry policy, or participates in a workflow. Don't conflate — the architecture doc has the matrix.

## Data owned

None directly; outbox rows live in `outbox_entries` ([`core/outbox`](core_outbox.md)).

## How it's tested

`test/test_service.py` covers registry registration, double-register rejection, and `enqueue()` writing the expected outbox payload (`task_name` + `queue` + `args`). Phase 1 adds end-to-end coverage through the broker.
