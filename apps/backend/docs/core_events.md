# core/events

> In-process pub/sub for SSE broadcasting to UI clients.

## Purpose

Thin in-process transport. Domain modules `publish()` typed events; UI clients subscribe via `GET /api/events`. Doesn't own event types or know what events mean — domain modules subclass `Event` with their own `kind` literal. Not durable: events lost on process restart or subscriber disconnect. UI re-fetches on (re)connect. `core/audit_log` is the durable record; this is the live wire.

## Public interface

Exports `Event`, `EventFilter`, `publish`, `publish_after_commit`, `subscribe`, `serialize_for_sse`, `stream_events_for_filter`, `subscriber_count`. See `apps/backend/app/core/events/__init__.py`.

- `Event` — base Pydantic class; domain modules subclass.
- `EventFilter` — subscriber filter (`ticket_id`, `kinds`).
- `publish(event)` — fire-and-forget dispatch to matching subscribers.
- `publish_after_commit(session, event)` — canonical helper for write-path code: stash on `session.info`, flush via a SQLAlchemy `after_commit` listener. Commit publishes; rollback discards. Use this whenever the event is tied to a transaction the caller owns.
- `subscribe(filter)` — async iterator over matching events; auto-unregisters on consumer exit.

HTTP route registered by the module:
- `GET /api/events` — SSE stream. Query params: `ticket_id` (UUID), `kinds` (repeatable). Registered from `core/events/web.py`.

## Module architecture

### `Event` envelope

Base carries `kind`, `source_module`, `ts` (default `now()`), and optional `ticket_id` (the standard scoping key the UI filters by). Domain modules subclass with `kind: Literal["..."]` and `source_module: Literal["..."]` plus their scoping fields. Subscribers dispatch by `isinstance` or `kind` literal.

### `EventFilter`

Carries optional `ticket_id` and optional `kinds: list[str]` with a `matches(event)` method. Both `None` → matches every event (broadcast). `ticket_id` set → only events with that id. `kinds` set → only events with `kind` in the list.

### Publish / subscribe internals

Module-level `_subscribers` dict keyed by UUID, holding `(EventFilter, asyncio.Queue)` per subscriber. `publish(event)` iterates and `put_nowait`s onto each matching queue. Slow subscribers don't block fast ones — each has its own bounded queue. On `QueueFull` the event drops for that subscriber and an `event.dropped` warning logs; the UI recovers via re-fetch.

`subscribe(filter)` is an async generator: registers a UUID-keyed queue (`maxsize=100`), yields `queue.get()` in a loop, unregisters in `finally` when the consumer exits or is cancelled.

The 100-event backpressure is generous for few-events-per-PR-per-minute traffic.

### `publish_after_commit`

Write paths need to publish events tied to a transaction outcome: fire on commit, discard on rollback. The helper stashes events under a sentinel key on the caller's `session.info`; a single module-level `Session.after_commit` listener pops them and schedules `publish()` onto the running loop (the listener is sync; `publish` is async). The hook fires under the SAVEPOINT-based test rollback fixture as well, so service tests see the same publish path production does.

### The SSE endpoint

`core/events/web.py` mounts `GET /api/events`. The handler builds an `EventFilter` from query params and returns a `StreamingResponse(stream_events_for_filter(filter), media_type="text/event-stream")`. The stream helper wraps `subscribe()` and yields `serialize_for_sse(event)` (the standard SSE `data:` framing of `model_dump_json()`).

No `Last-Event-ID` replay support. Events missed while disconnected are lost.

### Subscriber lifecycle

1. SPA opens `EventSource('/api/events?ticket_id=...')`.
2. FastAPI invokes handler; `subscribe()` registers a queue.
3. Events flow as domain modules `publish()`.
4. Client disconnects → generator `finally` → subscriber unregistered.
5. Browser `EventSource` auto-reconnects; a fresh subscription is created. Gap events are not replayed.

### Domain-event envelope adapter

Domain modules whose events are plain `@dataclass`es (not Pydantic) wrap them in a lightweight `Event` subclass to dispatch through this bus — e.g. `domain/reviewer.service._DomainEventEnvelope` sets `kind` from the dataclass class name and carries the payload as a dict. The bus stays Pydantic-only; the adapter lives in the domain module that owns the events.

### What it does not do

- Does not persist events (`core/audit_log` is the durable counterpart).
- Does not replay on reconnect.
- Does not cross processes — single-process; the in-memory dict is the entire transport.
- Does not define event types — domain modules subclass `Event`.

## Data owned

None. Subscriber registry in-memory; reset on process restart.

## How it's tested

`app/core/events/test/test_pubsub.py` covers the publish/subscribe contract — filter matching, queue overflow drop, unregistration on consumer exit. The SSE endpoint is exercised end-to-end against `TestClient`. Tests import `_reset_for_tests` directly from `app.core.events.service` to clear subscribers between cases.
