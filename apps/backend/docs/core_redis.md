# core/redis

> The single seam in front of Redis — the client never leaves the module; every Redis operation is a named primitive.

## Scope

- Owns: loop-bound client cache, the JSON pub/sub bus (`publish`/`subscribe`/`subscriber_count`), the `sliding_window_hit` rate-limit counter, health `ping`, shutdown registration.
- Does NOT own: channel naming or event shapes (consumers like [`core/sse`](core_sse.md) add those); the broker's connection (taskiq-redis builds its own pool from `settings.redis_url`).

## Why / invariants

**The client is private.** `_get_client()` never crosses the module boundary — no `get_client`, no `get_url`. Callers reach Redis only through the named primitives. This is what makes `core/redis` an actual encapsulation boundary rather than a client vendor.

**Per-loop client cache** — redis-py's async client binds its connection pool to the event loop where the first command ran. Reusing one client across loops fails with "Future attached to a different loop". `_clients: dict[int, Redis]` keyed by `id(asyncio.get_running_loop())` gives each loop its own client transparently.

**`decode_responses=False`** — the client speaks bytes; the JSON bus (`pubsub.py`) owns encode/decode so callers publish/subscribe `dict` events.

**Process-singleton bus** — `get_pubsub()` holds the local subscriber-count state; `reset_pubsub()` drops it synchronously (test isolation only).

**Self-registers `shutdown()`** with both web and worker shutdown registries at import time — it closes every cached client and drops the bus singleton. No explicit caller required. See [patterns.md § Two process lifecycles, two registries](patterns.md).

## Gotchas

- **`subscriber_count(channel)` is process-local** — not cluster-wide (`PUBSUB NUMSUB`); don't use it for load decisions.
- **`sliding_window_hit` owns the ZSET mechanics, not the policy** — it returns `True`/`False`; the caller (e.g. `core/agent_gateway/rate_limit.py`) decides the limit, the axis, and what to raise. Approximate at sub-second resolution.
