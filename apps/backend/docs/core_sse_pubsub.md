# core/sse_pubsub

> Redis-backed pub/sub for ActivityEvent fanout from `core/agent_gateway` (and the reviewer's direct activity publisher) to SSE handlers.

## Purpose

Bridges the activity-event producers (the WebSocket ingress in `core/agent_gateway` and the in-memory reviewer's direct publisher) and the per-workflow SSE handler. Publishers call `publish(channel, event)` with `channel = activity:{workflow_execution_id}`; subscribers iterate `async for event in subscribe(channel)`. Backed by Redis `PUBLISH`/`SUBSCRIBE` so a publish from the worker process reaches an SSE subscriber attached to a different web process. Fire-and-forget per Redis semantics — slow consumers do not backpressure publishers, and no event persistence.

## Public interface

Exported from `app/core/sse_pubsub/__init__.py`:

- `publish(channel, event)` — fan out to every subscriber on `channel`; returns the Redis-reported delivery count (number of subscribers across the cluster).
- `subscribe(channel)` — async iterator that yields each subsequent event published on `channel`. Subscriber registers a Redis subscription on first iteration and unregisters when the iterator exits.
- `channel_for(workflow_execution_id)` — centralized name shape (`activity:{id}`) so publishers + subscribers agree.
- `subscriber_count(channel)` — diagnostic; **local-process** subscriber count (Redis's `PUBSUB NUMSUB` is cluster-wide and not what callers want).
- `RedisPubsub` — class form for callers that want to construct their own bus (mostly tests).
- `get_pubsub()` — process-singleton accessor.
- `_reset_for_tests()` — drop the singleton + close the held Redis client.

## Module architecture

### Backend

Layered on [`core/redis`](core_redis.md). This module owns the channel naming convention (`activity:{workflow_execution_id}`) + JSON encode/decode of the event dict; `core/redis` owns connection management and the per-loop client cache. Client construction is lazy — importing the module or grabbing the singleton doesn't touch Redis, so tests that don't publish/subscribe don't need Redis to be reachable.

### Channel naming

`activity:{workflow_execution_id}`. The publisher (`core/agent_gateway` WebSocket handler, or the reviewer's `_activity_publisher_for`) constructs this from the workflow execution id. The SSE handler in `web.py` constructs it from the route path. `channel_for()` is the single source of truth — neither side hard-codes the prefix.

### Persistence invariant

**Activity events are never persisted.** They exist only between publish and the subscriber's consumer loop. Reload-the-UI = empty until the next event. Rationale: volume + nobody-scrolls-history.

## Data owned

None. The module is transport — Redis is the substrate.

## How it's tested

`test/test_service.py` covers: publish with no subscribers returns 0; fan-out delivers to every subscriber; subscriber bookkeeping balances on iterator exit; singleton identity. Tests use the `redis_or_skip` fixture from the root conftest so local dev workflows without Redis aren't blocked — when Redis is reachable, they hit real Redis with per-test unique channel names.
