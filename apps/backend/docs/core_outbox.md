# core/outbox

> DB-atomic outbound message queue. Atomic-in-session enqueue + post-commit dispatch.

## Purpose

Owns `outbox_entries` and the `write(session, kind, payload)` primitive. Callers write outbox rows inside their transaction; the drain delivers after commit. The atomic guarantee: if the session commits, the row is durable and will be dispatched; if it rolls back, the row never existed. This is the substrate [`core/tasks`](core_tasks.md) uses for atomic-in-session task enqueue.

## Public interface

Exports `write`, `drain_once`, `OutboxEntryRow`. See `apps/backend/app/core/outbox/__init__.py`.

- `await write(session, *, kind, payload)` — insert an undispatched row; required `session`; never commits. Returns the new row id.
- `await drain_once(session, *, dispatcher, batch_size=100)` — pull up to `batch_size` undispatched rows, hand each to `dispatcher(kind, payload)`, stamp `dispatched_at` on success. Failures bump `attempt` and leave the row. Returns the count successfully dispatched.

## Module architecture

### Drain semantics

- Polls `outbox_entries WHERE dispatched_at IS NULL ORDER BY created_at LIMIT N` (~100ms cadence in the worker).
- Dispatcher is caller-supplied — Phase 1's worker routes `kind="taskiq_enqueue"` to the taskiq broker. Future kinds (`pubsub_publish`, etc.) add their own dispatchers.
- Mark-dispatched happens *after* successful dispatch. If the drain crashes between dispatch and update, the next drain redispatches — task bodies must tolerate duplicates (they look up state from DB).
- On dispatch failure, the row stays undispatched with `attempt += 1` and `last_error` set. The next poll retries until success or some external policy marks it dead.

### Retention

Drained rows are pruned by a periodic task (Phase 1 wires it). Default retention: delete `dispatched_at < now() - 24h`.

## Data owned

- `outbox_entries` — `id uuid pk`, `kind text`, `payload jsonb`, `created_at`, `dispatched_at` (nullable), `attempt int default 0`, `last_error text` nullable. Created by migration `014_create_outbox_entries`.

## How it's tested

`test/test_service.py` covers: `write` inserts an undispatched row in the caller's session; `drain_once` delivers two rows and stamps both dispatched; dispatcher exception leaves the row undispatched with `attempt` and `last_error` updated.
