# core/tasks

> Durable task scheduler over taskiq + Redis with atomic-in-session enqueue.

## Scope

- **Owns:** `@task` decorator, `enqueue` API, outbox table (`outbox_entries`), drain loop, worker process scaffolding, `OrgContextMiddleware`.
- **Does not own:** task bodies (owned by callers); broker topology (Redis detail hidden here).
- **Boundary:** `enqueue` writes a row in the caller's transaction; the drain pump pushes to Redis; taskiq pops and executes. The entire durable path is self-contained.

## Why / invariants

- **`enqueue` requires `session`** — there is no fire-and-forget path. Durability is opt-in by committing the session.
- **`metadata` auto-fill from `org_id` contextvar** — when enqueued inside an `org_context()` block and `metadata` is omitted, `enqueue` fills `TaskMetadata(org_id=...)` automatically. HTTP request handlers don't need to pass it explicitly.
- **`TaskMetadata` is JSON-dumped on the wire** — avoids the prior `str(dict)` / `ast.literal_eval` round-trip. Consumer parses with `model_validate_json`.
- **`OrgContextMiddleware`** enters `org_context(metadata.org_id, ActorKind.SYSTEM)` before every task body — `current_org_id()` is reliably available inside any task.
- **Task bodies must tolerate duplicate delivery.** The drain stamps `dispatched_at` only after a successful Redis push — a crash between push and stamp redispatches. Bodies look up state from DB rather than trusting args alone.
- **Worker races three tasks via `asyncio.wait(FIRST_COMPLETED):** drain loop, taskiq receiver, stop signal. Stop signal wins on SIGTERM; the others tear down gracefully.
- **`drain_loop` and the taskiq receiver catch their own errors** — a defect logs `tasks.worker.child_crashed` and exits cleanly rather than silently discarding the exception.

## Gotchas

- **`spawn()` vs `enqueue`** — use [`core/observability.spawn()`](core_observability.md) for fire-and-forget request-scoped background work. Use `enqueue` for work that must survive restarts, has retry policy, or participates in a workflow.
- **`scoped_task_registration`** — required for test isolation when registering tasks dynamically; see [patterns.md § `scoped_*` context managers](patterns.md).
- **`shutdown()` does not drop the broker singleton** — task registrations set at import time remain intact.

## Data owned

- `outbox_entries` — `(id uuid pk, kind text, payload jsonb, created_at, dispatched_at nullable, attempt int, last_error text nullable)`. Only kind today: `taskiq_enqueue`. Migration 014.

## How it's tested

`test/test_service.py` — registry registration, double-register rejection, `enqueue` outbox payload.
`test/test_enqueue_metadata_service.py` — metadata auto-fill from contextvar, explicit override, no-context path.
`test/test_drain.py` — `write` + `drain_once` with stub dispatcher; failure leaves row pending with updated `attempt` + `last_error`.
`test/test_middleware_service.py` — `current_org_id()` visible inside task body via `InMemoryBroker` + real outbox drain.
`test/test_scoped_task_registration.py` — task visible inside scope, gone outside, cleans up on exception.
