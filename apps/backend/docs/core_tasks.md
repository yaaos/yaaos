# core/tasks

> Durable task scheduler over taskiq + Redis with atomic-in-session enqueue, plus a cluster-safe recurring-task scheduler.

## Scope

- **Owns:** `@task` decorator, `enqueue` API, outbox table (`outbox_entries`), drain loop, worker process scaffolding, `OrgContextMiddleware`, `TaskMetricsMiddleware`, `TaskSpanMiddleware`, recurring-task scheduler (`@scheduled` / `schedule_task` / `scheduler_loop`), `scheduled_runs` dedup ledger.
- **Does not own:** task bodies (owned by callers); broker topology (Redis detail hidden here).
- **Boundary:** `enqueue` writes a row in the caller's transaction; the drain pump pushes to Redis; taskiq pops and executes. The entire durable path is self-contained. The scheduler tick loop runs alongside the drain in every worker and gates per-slot enqueue via an atomic `INSERT … ON CONFLICT DO NOTHING` on `scheduled_runs`.

## Why / invariants

- **`enqueue` requires `session`** — there is no fire-and-forget path. Durability is opt-in by committing the session.
- **`metadata` auto-fill from `org_id` contextvar** — when enqueued inside an `org_context()` block and `metadata` is omitted, `enqueue` fills `TaskMetadata(org_id=...)` automatically. HTTP request handlers don't need to pass it explicitly.
- **`TaskMetadata` is JSON-dumped on the wire** — avoids the prior `str(dict)` / `ast.literal_eval` round-trip. Consumer parses with `model_validate_json`.
- **`OrgContextMiddleware`** enters `org_context(metadata.org_id, ActorKind.SYSTEM)` before every task body — `current_org_id()` is reliably available inside any task.
- **Task bodies must tolerate duplicate delivery.** The drain stamps `dispatched_at` only after a successful Redis push — a crash between push and stamp redispatches. Bodies look up state from DB rather than trusting args alone.
- **`@task` registration happens at composition root, not inside `runtime`.** `app/worker.py` imports all task-defining modules before calling `runtime.run()`, so `@task` decorators are registered with the broker before the worker loop starts. `runtime.run()` itself does not import task-defining modules.
- **Worker races five tasks via `asyncio.wait(FIRST_COMPLETED)`:** drain loop, taskiq receiver, scheduler tick loop, liveness ticker, stop signal. Stop signal wins on SIGTERM; shutdown then proceeds in ordered steps — see § Worker graceful drain below.
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

## Worker metrics

`core/tasks/metrics.py` declares four OTel instruments that the worker emits per task execution,
dimensioned by **task name only** (not `org_id` — per-org cardinality would explode the metric stream):

| Instrument | Kind | Unit | Meaning |
|---|---|---|---|
| `task.started` | Counter | `1` | Incremented once when a task body begins. |
| `task.succeeded` | Counter | `1` | Incremented when a task body returns without error. |
| `task.failed` | Counter | `1` | Incremented when a task body raises an exception. |
| `task.duration` | Histogram | `s` | Wall-clock execution time from `pre_execute` to `post_execute` or `on_error`. |

`TaskMetricsMiddleware` is the carrier — wired into the broker in `runtime.run()` alongside
`OrgContextMiddleware` and `TaskSpanMiddleware`. The module-level instruments are obtained via `metrics.get_meter(__name__)`
at import time; in the worker process they delegate to the real `MeterProvider` set by
`observability.configure(role="worker")`. Tests inject fresh instruments via constructor
parameters to avoid touching global OTel state.

## Worker task spans

`core/tasks/spans.py` declares `TaskSpanMiddleware`, wired into the broker in `runtime.run()` alongside `OrgContextMiddleware` and `TaskMetricsMiddleware`. For each task execution:

- Opens a span named `task:<task_name>` via `pre_execute` and `context.attach`es it as the current context (keyed with the span by `task_id`), so spans created inside the body — SQLAlchemy auto-instrumentation, manual spans — nest under the task span instead of the dequeue-time context.
- On error (`on_error` / `post_execute` with `is_err`): calls `span.record_exception(exc)` + `span.set_status(ERROR)` so the failure is visible in traces, not just logs.
- `context.detach`es the stored token and ends the span in `post_execute` / `on_error` — even if exception recording itself fails.

Tests inject a tracer from a local `TracerProvider` + `InMemorySpanExporter` via the `tracer=` constructor parameter, matching the `TaskMetricsMiddleware` instrument-injection pattern.

## Worker graceful drain

On SIGTERM `runtime.run()` executes an ordered shutdown sequence so in-flight task bodies are not hard-cancelled:

1. **Stop the drain loop** — `drain_stop` event is set; `drain_loop` exits cleanly between batches (not via `CancelledError`). No new tasks are pushed to the broker from this point.
2. **Stop the scheduler loop** — cancelled (idempotent; an interrupted tick re-runs on the next worker startup).
3. **Stop the liveness ticker** — cancelled; health check no longer needed during drain.
4. **Set `consume_stop`** — the Receiver's prefetcher stops accepting new messages from the broker.
5. **AWAIT the consume task (not cancel)** — `Receiver.listen` calls `asyncio.wait(in_flight_tasks, timeout=_WORKER_DRAIN_GRACE_SECONDS)`. Bodies that finish within the grace window complete normally; bodies that exceed it are **abandoned, not cancelled** — the worker exits regardless.
6. **Run reverse-order shutdown hooks** — broker, Redis, DB.

`_WORKER_DRAIN_GRACE_SECONDS = 60` is the constant in `runtime.py`. `fly.production.toml kill_timeout` must exceed this value plus the OTel flush budget or Fly will hard-kill mid-drain.

`drain_loop` accepts a `stop: asyncio.Event | None` parameter. When set, the loop exits after the current batch rather than continuing to poll. The runtime passes a dedicated `drain_stop` event (not the process-level `stop`) so the drain can be halted before the Receiver's finish event without racing the SIGTERM handler.

Shutdown hooks must tolerate in-flight work — `_WORKER_DRAIN_GRACE_SECONDS` bounds the wait, but abandoned bodies may still be running when hooks execute.

## Worker liveness heartbeat and health server

`runtime.run()` starts two additional tasks alongside drain / consume / scheduler:

- **Liveness ticker** — `_liveness_ticker(heartbeat, stop)` advances `WorkerHeartbeat.tick()` every `TICKER_INTERVAL_SECONDS` (5 s). Because it runs as a peer asyncio task in the same event loop as the consume loop, a wedged loop stops the ticker too, making the health check return 503 within two missed ticks (~10–60 s depending on the stale threshold).
- **Worker health server** — a minimal single-route Starlette app run via a background `uvicorn.Server` task on `0.0.0.0:<yaaos_worker_health_port>` (default `8081`). The handler (`worker_health.py`) runs `database.ping()` + `redis.ping()` and checks `WorkerHeartbeat.is_fresh()`. Returns 200 `{status:"ok",…}` when all pass; 503 `{status:"degraded",…}` when any fail. Response body always includes `db_ok`, `redis_ok`, `heartbeat_ok`, `status`.
- The health server is NOT the main FastAPI app — no auth middleware, no Cloudflare ingress gate — so Fly's machine checker reaches it directly.
- `yaaos_worker_health_port: int = 8081` in `core/config` controls the bind port.

## How it's tested

`test/test_service.py` — registry registration, double-register rejection, `enqueue` outbox payload.
`test/test_enqueue_metadata_service.py` — metadata auto-fill from contextvar, explicit override, no-context path.
`test/test_drain.py` — `write` + `drain_once` with stub dispatcher; failure leaves row pending with updated `attempt` + `last_error`.
`test/test_middleware_service.py` — `current_org_id()` visible inside task body via `InMemoryBroker` + real outbox drain.
`test/test_scoped_task_registration.py` — task visible inside scope, gone outside, cleans up on exception.
`test/test_scheduler_exactly_once_service.py` — N concurrent `tick_once` calls on independent sessions for one fire slot → exactly one `scheduled_runs` insert wins → exactly one outbox enqueue. The named guard for the per-tick claim invariant.
`test/test_scheduled_runs_prune_service.py` — broker registration of the prune task body; body deletes >7-day rows and leaves fresher rows alone.
`test/test_scheduler_backoff.py` — unit-tests the pure `_backoff_sleep` helper: exponential growth, 120 s cap, normal-cadence restore after a reset.
`test/test_task_metrics_service.py` — `TaskMetricsMiddleware` with injected `InMemoryMetricReader` instruments: successful body increments `task.started` + `task.succeeded` + records `task.duration`; failing body increments `task.failed` instead.
`test/test_task_span_service.py` — `TaskSpanMiddleware` with injected `InMemorySpanExporter` tracer: failing body produces a span with ERROR status + exception event; successful body produces a span with no exception events; a span opened inside the body nests under the task span (shared trace + parent = task span).
`test/test_worker_health_service.py` — `build_worker_health_app` with stub ping callables: 200 when all pass; 503 on DB failure; 503 on Redis failure; 503 when heartbeat is stale. Also unit-tests `WorkerHeartbeat.is_fresh()` transitions.
`test/test_graceful_drain_service.py` — drain loop exits cleanly on stop signal; in-flight body completes before worker exits (await-not-cancel); over-grace body is abandoned, not cancelled, and the worker still exits.
