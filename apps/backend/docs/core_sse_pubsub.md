# core/sse_pubsub

> Pub/sub for ActivityEvent fanout from `core/agent_gateway` to SSE handlers.

## Purpose

Owns the in-process fanout primitive that bridges the activity-stream WebSocket (ingress at `core/agent_gateway`) and the per-workflow SSE handler in `core/webserver`. Publishers call `publish(channel, event)` with `channel = activity:{workflow_execution_id}`; subscribers iterate `async for event in subscribe(channel)`. Best-effort delivery: a slow subscriber drops its oldest queued event rather than backpressuring the publisher. No event persistence — activity is in flight only.

## Public interface

Exported from `app/core/sse_pubsub/__init__.py`:

- `publish(channel, event)` — fan out to every subscriber on `channel`; returns the number reached.
- `subscribe(channel)` — async iterator that yields each subsequent event published on `channel`. Subscriber registers on first iteration and unregisters when the iterator exits.
- `channel_for(workflow_execution_id)` — centralized name shape (`activity:{id}`) so publishers + subscribers agree.
- `subscriber_count(channel)` — diagnostic; tests use it to assert demand-pull semantics.
- `InMemoryPubsub` — class form for callers that want to construct their own bus (mostly tests).
- `get_pubsub()` — process-singleton accessor.
- `_reset_for_tests()` — drop the singleton.

## Module architecture

### Backends

- **`InMemoryPubsub`** ships in Phase 8b foundations and is the only backend today. One `asyncio.Queue` per (channel, subscriber). Bounded buffer (default 256) per subscriber — when full, the head event is dropped before the new event is queued, so slow consumers can't induce unbounded memory growth.
- **Redis-backed** — slots in behind the same module surface in the Phase 8b follow-on alongside the worker process. `settings.redis_url` flips the implementation.

### Channel naming

`activity:{workflow_execution_id}`. The publisher (`core/agent_gateway` WebSocket handler) constructs this from the inbound `activity_batch` message. The SSE handler in `web.py` (Phase 8b follow-on) constructs it from the route path. `channel_for()` is the single source of truth — neither side hard-codes the prefix.

### Persistence invariant

**Activity events are never persisted.** They exist only between publish and the subscriber's consumer loop. Reload-the-UI = empty until the next event. The architecture's [§ Persistence invariant](../../../plan/milestones/M05-workspace-agent/architecture.md#persistence-invariant) explains the rationale (volume + nobody-scrolls-history).

## Data owned

None. The module is transport.

## How it's tested

`test/test_service.py` covers: publish with no subscribers returns 0; fan-out delivers to every subscriber; subscriber removal after iterator exit; slow-consumer drops the oldest event (the bounded-buffer behavior); singleton identity.
