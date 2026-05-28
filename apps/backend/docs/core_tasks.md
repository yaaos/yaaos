# core/tasks

> Durable task scheduler over taskiq + Redis with atomic-in-session enqueue.

## Purpose

Owns the `@task` decorator, the `enqueue(task, args, *, session)` API, and the worker process that runs tasks. Atomic-in-session: `enqueue` writes a row to the private `outbox_entries` table inside the caller's transaction, so the task is durable iff the commit lands. Hides taskiq broker details from domain code — swapping the scheduler is contained here.

The taskiq broker is the single task registry; `@task` registers directly with `broker.task(...)`. `broker.find_task(name)` is the lookup (used by the drain dispatcher and by in-process test drains).

## Public interface

Exports `task`, `enqueue`, `TaskRef`, `drain_once`, `scoped_task_registration`, `ShutdownHook`, `shutdown`, `register_worker_shutdown_hook`, `iter_worker_shutdown_hooks`. See `apps/backend/app/core/tasks/__init__.py`.

- `@task(name, *, queue="default", max_retries=1)` — registers a task body with the broker; returns a `TaskRef` callers `enqueue` against. `queue` / `max_retries` ride as taskiq labels (no consumer today; future brokers / middleware pick them up without API churn).
- `enqueue(task_ref, args, *, session)` — writes a `taskiq_enqueue` outbox row in the caller's session. Returns the row id. Required `session` — there is no fire-and-forget path.
- `TaskRef` — frozen handle to a registered task name + queue + retry policy.
- `drain_once(db_session, *, dispatcher, batch_size=100)` — pulls undispatched outbox rows and hands each to `dispatcher`. Service tests import from the package top-level (`from app.core.tasks import drain_once`).
- `scoped_task_registration(task_ref)` — context manager for test isolation. Wrap the test body after calling `@task(name)(fn)` to get a `TaskRef`; on exit, the name is popped from the broker registry. See [patterns.md § `scoped_*` context managers](patterns.md).
- `shutdown()` — calls the broker object's own async `shutdown()` (closes connections). Does NOT drop the broker singleton — task registrations set at import time remain intact. Re-exported from `core/shutdown_registry` registration side-effect.
- `ShutdownHook`, `register_worker_shutdown_hook`, `iter_worker_shutdown_hooks` — re-exported from `core/shutdown_registry`.

The outbox model (`OutboxEntryRow`) and the internal drain primitive (`write`) are private substrate in `app.core.tasks.models` and `app.core.tasks.drain`. The worker entrypoint imports `drain_loop` directly from `app.core.tasks.drain`. `drain_once` is public and exported from the package top-level.

## Module architecture

### Core flow

1. Task author writes `@task("route_workflow") async def route_workflow(*, exec_id, ...)`. The decorator registers the body with the taskiq broker.
2. Domain caller inside a transaction: `await enqueue(route_workflow, args={...}, session=s); await s.commit()`. The `outbox_entries` row commits atomically with the rest of the caller's writes.
3. The worker's drain loop polls `outbox_entries WHERE dispatched_at IS NULL` with `FOR UPDATE SKIP LOCKED`, dispatches `kind='taskiq_enqueue'` rows to the taskiq broker (LPUSH to a Redis list), then stamps `dispatched_at`.
4. The same worker process runs `broker.listen()` — it BRPOPs tasks from Redis and invokes the registered body with the kwargs the caller passed to `enqueue`. Bodies own their own session (they open one via `core/database.session()`). Retry middleware to honor `max_retries` isn't wired yet; for now the drain re-loops on failure.

### Worker process

`apps/backend/app/core/tasks/runtime.py` (entry point via `apps/backend/app/worker.py`) boots one event loop racing three tasks via `asyncio.wait(..., FIRST_COMPLETED)`:

- `drain_loop(broker)` — Postgres → Redis pump (lives in `drain.py`). Sleeps ~100ms between empty polls; immediately re-polls when a batch had work. Per-batch transaction so a crash mid-batch redispatches at-most a batch's worth of rows on restart.
- `Receiver.listen(stop)` — taskiq's consumer loop. Pops tasks from Redis, invokes the registered body. Exits when the `stop` event is set.
- `stop.wait()` — fires on SIGTERM/SIGINT and normally wins the race, triggering graceful shutdown of the other two.

The broker URL comes from [`core/redis.get_url()`](core_redis.md) — taskiq-redis takes a URL (not a client) so this is a thin accessor on top of `settings.redis_url`. After the stop signal wins, the runtime iterates `iter_worker_shutdown_hooks()` in reverse registration order to teardown all registered resources. See [patterns.md § Two process lifecycles, two registries](patterns.md).

`drain_loop` and the taskiq receiver each catch their own errors, so the worker scaffolding normally sees only the stop-signal task finish first. If a defect lets one escape, the worker logs `tasks.worker.child_crashed` with the traceback before tearing down — the process still exits and the supervisor restarts it. Without this the exception would be silently discarded when the `Task` is garbage-collected.

Single-process today. If the workload demands it, the drain and consume tasks split into separate compose services (same image, different `CMD` args) so taskiq concurrency and drain throughput scale independently.

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

`test/test_service.py` covers registry registration (via `scoped_task_registration`), double-register rejection, and `enqueue()` writing the expected outbox payload. `test/test_drain.py` covers `write()` + `drain_once()` with a stub dispatcher: insert, drain, stamp; failure leaves the row pending with `attempt` and `last_error` updated. `test/test_scoped_task_registration.py` covers the scoped context manager: task visible inside, gone outside, cleanup on exception. `test/test_drain_once_public.py` smoke-tests the public package import path. The broker-wired dispatcher is exercised end-to-end against the dev Redis stack — see `apps/e2e/` for the full enqueue → drain → execute path.

Cross-module service tests that exercise the outbox pump in-process import `drain_once` from the package top-level (`from app.core.tasks import drain_once`) and `OutboxEntryRow` from `app.core.tasks.models`.
