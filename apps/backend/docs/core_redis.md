# core/redis

> Single Redis access point — client construction, pub/sub primitives, health ping.

## Purpose

Centralizes everything any other module needs from Redis: a loop-bound client (per running event loop), a URL accessor that consumers like [`core/tasks/broker`](core_tasks.md) hand to taskiq, raw publish/subscribe primitives that [`core/sse_pubsub`](core_sse_pubsub.md) builds on, and the health-check ping for `/api/health`. No higher-level semantics; consumers add their own (channel naming, JSON encoding, etc.).

## Public interface

Exports `get_client`, `get_url`, `publish`, `subscribe`, `ping`, `aclose`, `shutdown`. See `apps/backend/app/core/redis/__init__.py`.

- `get_client()` — returns the Redis client bound to the current running event loop. Constructs on first call per loop. `decode_responses=False` (bytes).
- `get_url()` — returns `settings.redis_url`. Single accessor so other modules don't read config directly.
- `publish(channel, payload: bytes) -> int` — `PUBLISH` on `channel`; returns cluster-wide delivery count.
- `subscribe(channel) -> AsyncIterator[bytes]` — yields each subsequent message body; filters out subscribe/unsubscribe confirmations. Subscriber registers on first iteration, unregisters on iterator close.
- `ping() -> bool` — `PING` against Redis; swallows exceptions.
- `aclose()` — closes every cached client; idempotent.
- `shutdown()` — async alias for `aclose()`; self-registered with both web and worker shutdown registries at import time.

No HTTP routes.

## Module architecture

### Per-loop client cache

redis-py's async client binds its connection pool to the event loop where the first command ran. Reusing one client across loops (web request loop vs worker loop vs `TestClient` portal loop) fails with "Future attached to a different loop". `_clients: dict[int, Redis]` keyed by `id(asyncio.get_running_loop())` gives each loop its own client transparently. The cost is one extra Redis connection per loop — negligible.

### Bytes everywhere

`get_client()` returns a client with `decode_responses=False`. Consumers encode their own payloads (JSON, MsgPack, raw bytes). Keeps the module substrate-only.

### Lifecycle

`core/redis` self-registers `shutdown()` with both the web and worker shutdown registries at import time. In both processes it is called by the registry loop during teardown — no explicit caller required. See [patterns.md § Two process lifecycles, two registries](patterns.md).

## Data owned

None. Pure connection management.

## How it's tested

`test/test_service.py` covers loop-bound client identity (same loop = same client), `ping()` against real Redis, `aclose()` clears the cache, and cross-loop isolation (two `new_event_loop()`s get two different clients).

`test/test_pubsub.py` covers the publish/subscribe round-trip against real Redis with unique per-test channel names. Both files use the `redis_or_skip` fixture from the root conftest so local dev workflows without Redis aren't blocked.
