# core/tasks

> Durable task scheduler over taskiq + Redis with atomic-in-session enqueue.

## Purpose

Owns the `@task` decorator, the `TaskContext` shape every task body receives, the `enqueue(task, args, *, session)` API, and the worker process that runs tasks. Atomic-in-session: `enqueue` writes a row to the private `outbox_entries` table inside the caller's transaction, so the task is durable iff the commit lands. Hides taskiq broker details from domain code — swapping the scheduler is contained here.

## Public interface

Exports `task`, `enqueue`, `TaskContext`, `TaskRef`, plus `OutboxEntryRow`, `drain_once`, `write` for the worker entrypoint and tests. See `apps/backend/app/core/tasks/__init__.py`.

- `@task(name, *, queue="default", max_retries=1)` — registers a task body; returns a `TaskRef` callers `enqueue` against.
- `enqueue(task_ref, args, *, session)` — writes a `taskiq_enqueue` outbox row in the caller's session. Returns the row id. Required `session` — there is no fire-and-forget path.
- `TaskContext` — first positional arg of every task body. Carries `session`, `traceparent`, `attempt`, `job_id`. The worker wrapper opens the session per-task and commits on success.
- `TaskRef` — frozen handle to a registered task name + queue + retry policy.

## Module architecture

### Core flow

1. Task author writes `@task("route_workflow") async def route_workflow(ctx, ...)`. The decorator registers the body in an in-process registry.
2. Domain caller inside a transaction: `await enqueue(route_workflow, args={...}, session=s); await s.commit()`. The `outbox_entries` row commits atomically with the rest of the caller's writes.
3. The worker's drain loop polls `outbox_entries WHERE dispatched_at IS NULL` with `FOR UPDATE SKIP LOCKED`, dispatches `kind='taskiq_enqueue'` rows to the taskiq broker (LPUSH to a Redis list), then stamps `dispatched_at`.
4. The same worker process runs `broker.listen()` — it BRPOPs tasks from Redis and invokes the registered body with a fresh `TaskContext`. On successful return the worker commits the task's session and ACKs; on raise it retries per `max_retries`.

### Worker process

`apps/backend/bin/worker` boots one event loop with two coroutines via `asyncio.gather`:

- `drain_loop(broker)` — Postgres → Redis pump (lives in `drain.py`). Sleeps ~100ms between empty polls; immediately re-polls when a batch had work. Per-batch transaction so a crash mid-batch redispatches at-most a batch's worth of rows on restart.
- `broker.listen()` — taskiq's consumer loop. Pops tasks from Redis, invokes the registered body.

Single-process POC. If the workload demands it, the two coroutines split into separate compose services (same image, different `CMD` args) so taskiq concurrency and drain throughput scale independently.

### Outbox table (private)

`outbox_entries` is the private substrate: `id`, `kind`, `payload` (jsonb), `created_at`, `dispatched_at`, `attempt`, `last_error`. Domain modules never import the model — they go through `enqueue()`. Migration `014_create_outbox_entries`.

`kind` is opaque so future consumers (e.g. cross-process webhook emission) plug in via their own dispatcher without schema changes. Today the only kind is `taskiq_enqueue` with payload `{task_name, queue, args}`.

### Idempotency

Task bodies MUST tolerate duplicate delivery. The drain stamps `dispatched_at` only after a successful broker push — a crash between push and stamp redispatches on the next poll. Body authors look up state from the DB rather than trusting the args alone.

### Relationship to `spawn()`

[`core/observability.spawn()`](core_observability.md) stays for fire-and-forget request-scoped background work without durability needs. `core/tasks` is for work that must survive restarts, has retry policy, or participates in a workflow. Don't conflate — the architecture doc has the matrix.

## Data owned

- `outbox_entries` — `(id uuid pk, kind text, payload jsonb, created_at, dispatched_at nullable, attempt int default 0, last_error text nullable)`. Created by migration 014.

## How it's tested

`test/test_service.py` covers registry registration, double-register rejection, and `enqueue()` writing the expected outbox payload. `test/test_drain.py` covers `write()` + `drain_once()` with a stub dispatcher: insert, drain, stamp; failure leaves the row pending with `attempt` and `last_error` updated. The broker-wired dispatcher is exercised end-to-end against the dev Redis stack — see `apps/e2e/` for the full enqueue → drain → execute path.
