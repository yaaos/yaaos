# core/tasks

> Durable task scheduler over taskiq + Redis with atomic-in-session enqueue, plus a cluster-safe recurring-task scheduler.

## Scope

- **Owns:** `@task` decorator, `enqueue` API, outbox table (`outbox_entries`), drain loop, worker process scaffolding, `OrgContextMiddleware`, recurring-task scheduler (`@scheduled` / `schedule_task` / `scheduler_loop`), `scheduled_runs` dedup ledger.
- **Does not own:** task bodies (owned by callers); broker topology (Redis detail hidden here).
- **Boundary:** `enqueue` writes a row in the caller's transaction; the drain pump pushes to Redis; taskiq pops and executes. The entire durable path is self-contained. The scheduler tick loop runs alongside the drain in every worker and gates per-slot enqueue via an atomic `INSERT … ON CONFLICT DO NOTHING` on `scheduled_runs`.

## Why / invariants

- **`enqueue` requires `session`** — there is no fire-and-forget path. Durability is opt-in by committing the session.
- **`metadata` auto-fill from `org_id` contextvar** — when enqueued inside an `org_context()` block and `metadata` is omitted, `enqueue` fills `TaskMetadata(org_id=...)` automatically. HTTP request handlers don't need to pass it explicitly.
- **`TaskMetadata` is JSON-dumped on the wire** — avoids the prior `str(dict)` / `ast.literal_eval` round-trip. Consumer parses with `model_validate_json`.
- **`OrgContextMiddleware`** enters `org_context(metadata.org_id, ActorKind.SYSTEM)` before every task body — `current_org_id()` is reliably available inside any task.
- **Task bodies must tolerate duplicate delivery.** The drain stamps `dispatched_at` only after a successful Redis push — a crash between push and stamp redispatches. Bodies look up state from DB rather than trusting args alone.
- **`@task` registration happens at composition root, not inside `runtime`.** `app/worker.py` imports all task-defining modules before calling `runtime.run()`, so `@task` decorators are registered with the broker before the worker loop starts. `runtime.run()` itself does not import task-defining modules.
- **Worker races four tasks via `asyncio.wait(FIRST_COMPLETED):** drain loop, taskiq receiver, scheduler tick loop, stop signal. Stop signal wins on SIGTERM; the others tear down gracefully.
- **`drain_loop` and the taskiq receiver catch their own errors** — a defect logs `tasks.worker.child_crashed` and exits cleanly rather than silently discarding the exception.
- **Recurring schedules are static + declarative** — `@scheduled(name, cron)` and `schedule_task(name, cron, task_ref=...)` register at import time into a process-local registry. No runtime mutation, no leader election. Every worker runs `scheduler_loop`; cluster safety lives in the per-tick claim, not in elected ownership.
- **Per-tick atomic claim is the sole gate** — for each registered schedule whose cron matches the current floored-minute slot, the tick attempts `INSERT INTO scheduled_runs (schedule_id, fire_time) VALUES (...) ON CONFLICT DO NOTHING`. Only the worker whose insert wins (`rowcount == 1`) calls `enqueue(...)`. Losers see `rowcount == 0` and skip. Mirrors the `github_webhook_events` `ON CONFLICT` dedup precedent.
- **`fire_time` is floored to the minute (UTC)** — every worker computing within the same minute races the same composite-PK row regardless of within-minute drift; no double-enqueues from multiple sub-minute passes.
- **`scheduler_loop` failures back off exponentially** — a caught `tick_once` error is logged + swallowed (the loop never exits on a transient hiccup), but the post-error sleep grows as `tick_interval_seconds * 2**consecutive_failures`, capped at 120 s. A successful tick resets the counter and restores the normal cadence. This bounds the error-log rate during a persistent outage (DB unreachable, broker error) to O(log(duration)) instead of a fixed cadence.
- **Scheduled bodies must remain idempotent** — same rule as every `core/tasks` body. The claim is the strong guarantee that exactly one *enqueue* happens per slot; the body itself can still re-run on dispatch retry per the existing outbox-drain semantics.

## Gotchas

- **`spawn()` vs `enqueue`** — use [`core/observability.spawn()`](core_observability.md) for fire-and-forget request-scoped background work. Use `enqueue` for work that must survive restarts, has retry policy, or participates in a workflow.
- **`scoped_task_registration`** (in `app.core.tasks.service`, not re-exported from the package) — required for test isolation when registering tasks dynamically; tests reach it via direct submodule import. See [patterns.md § `scoped_*` context managers](patterns.md).
- **`shutdown()` does not drop the broker singleton** — task registrations set at import time remain intact.

## Data owned

- `outbox_entries` — `(id uuid, created_at timestamptz NOT NULL, kind text, payload jsonb, dispatched_at nullable, attempt int, last_error text nullable)`. Composite PK `(id, created_at)` — partition-ready hedge (migration 042). `created_at` has a server default; the NOT NULL + composite PK are idempotent DDL applied on top of migration 014. Only kind today: `taskiq_enqueue`.
- `scheduled_runs` — `(schedule_id text, fire_time timestamptz, created_at timestamptz)`. Composite PK `(schedule_id, fire_time)`. One row per fired slot; the insert IS the cluster-safe enqueue gate. Pruned daily by the `scheduled_runs_prune` `@scheduled` task (deletes rows >7 days old) — the first `@scheduled` consumer, self-exercising the scheduler.

## How it's tested

`test/test_service.py` — registry registration, double-register rejection, `enqueue` outbox payload.
`test/test_enqueue_metadata_service.py` — metadata auto-fill from contextvar, explicit override, no-context path.
`test/test_drain.py` — `write` + `drain_once` with stub dispatcher; failure leaves row pending with updated `attempt` + `last_error`.
`test/test_middleware_service.py` — `current_org_id()` visible inside task body via `InMemoryBroker` + real outbox drain.
`test/test_scoped_task_registration.py` — task visible inside scope, gone outside, cleans up on exception.
`test/test_scheduler_exactly_once_service.py` — N concurrent `tick_once` calls on independent sessions for one fire slot → exactly one `scheduled_runs` insert wins → exactly one outbox enqueue. The named guard for the per-tick claim invariant.
`test/test_scheduled_runs_prune_service.py` — broker registration of the prune task body; body deletes >7-day rows and leaves fresher rows alone.
`test/test_scheduler_backoff.py` — unit-tests the pure `_backoff_sleep` helper: exponential growth, 120 s cap, normal-cadence restore after a reset.
