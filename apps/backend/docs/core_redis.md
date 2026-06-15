# core/redis

> The single seam in front of Redis — the client never leaves the module; every Redis operation is a named primitive.

## Scope

- Owns: loop-bound client cache, the JSON pub/sub bus (`publish`/`subscribe`/`subscriber_count`), named Redis primitives for HASH / SET / ZSET operations (`hash_ops.py`, `set_ops.py`, `zset_ops.py`), the `sliding_window_hit` rate-limit counter, `set_if_absent` idempotency primitive, `scan_keys` pattern sweep, health `ping`, shutdown registration.
- Does NOT own: channel naming or event shapes (consumers like [`core/sse`](core_sse.md) add those); the broker's connection (taskiq-redis builds its own pool from `settings.redis_url`).

## Why / invariants

**The client is private.** `_get_client()` never crosses the module boundary — no `get_client`, no `get_url`. Callers reach Redis only through the named primitives. This is what makes `core/redis` an actual encapsulation boundary rather than a client vendor.

**Per-loop client cache** — redis-py's async client binds its connection pool to the event loop where the first command ran. Reusing one client across loops fails with "Future attached to a different loop". `_clients: dict[int, Redis]` keyed by `id(asyncio.get_running_loop())` gives each loop its own client transparently.

**`decode_responses=False`** — the client speaks bytes; the JSON bus (`pubsub.py`) owns encode/decode so callers publish/subscribe `dict` events.

**ContextVar-bound bus** — the active `RedisPubsub` instance lives in `_pubsub_var: ContextVar`. The composition root (`app/web.py`, `app/worker.py`) calls `bind_pubsub(RedisPubsub())` at startup before any code can call `get_pubsub()`. `get_pubsub()` raises `RuntimeError` when unbound — deliberate fail-fast that surfaces forgotten startup binds immediately. The `pubsub_isolation` fixture in `app/testing/isolation` calls `bind_pubsub` per test so every test gets a fresh instance without any reset call.

**`bind_pubsub` is the production DI seam.** It appears in `__all__` because the composition root is the intended importer. `reset_pubsub` does not exist — tests use the fixture, not a reset helper.

**Self-registers `shutdown()`** with both web and worker shutdown registries at import time — it closes every cached client and clears the ContextVar binding. No explicit caller required. See [patterns.md § Two process lifecycles, two registries](patterns.md).

## Named primitives

- `sliding_window_hit(key, *, limit, window_seconds)` — rate-limit counter backed by a Redis ZSET. Returns `True` if the hit is within the limit, `False` if it would exceed it. Caller owns the policy (axis, error shape, HTTP status).
- `set_if_absent(key, ttl_seconds)` — cross-pod idempotency / replay protection. Wraps `SET key 1 NX EX ttl_seconds`; returns `True` on insert (this caller wins), `False` when the key already existed (replay / duplicate). Used by `core/agent_gateway/sts_verifier` to reject replayed signed STS envelopes across pods.
- `scan_keys(pattern)` — returns all keys matching `pattern` via `SCAN MATCH pattern COUNT 100` (iterated to completion). Used by the subscriber sweeper to find `workflow_subscribers:*` keys for GC.
- **HASH primitives** (`hash_ops.py`):
  - `hash_set(key, fields: Mapping[str, str])` — `HSET key field1 val1 ...` (multi-field).
  - `hash_get_all(key)` — `HGETALL key`; returns `dict[str, str]`, empty on missing key.
  - `hash_delete(key)` — `DEL key` (full key, not a specific field).
- **SET primitives** (`set_ops.py`):
  - `set_add(key, member)` — `SADD key member`.
  - `set_remove(key, member)` — `SREM key member`.
  - `set_members(key)` — `SMEMBERS key`; returns `set[str]`.
- **ZSET primitives** (`zset_ops.py`):
  - `zset_add_member(key, member, score)` — `ZADD key score member`.
  - `zset_remove_member(key, member)` — `ZREM key member`.
  - `zset_card(key)` — `ZCARD key`; returns `int`.
  - `zset_remove_by_score(key, min_score, max_score)` — `ZREMRANGEBYSCORE key min max`.

Each primitive is a single async function in its own file, mirroring `sliding_window.py`.

## Gotchas

- **`subscriber_count(channel)` is process-local** — not cluster-wide (`PUBSUB NUMSUB`); don't use it for load decisions.
- **`sliding_window_hit` owns the ZSET mechanics, not the policy** — it returns `True`/`False`; the caller (e.g. `core/agent_gateway/rate_limit.py`) decides the limit, the axis, and what to raise. Approximate at sub-second resolution.
- **`get_pubsub()` raises if unbound** — if a code path hits this at startup, `bind_pubsub` was not called before that code ran. Fix the startup order; don't restore lazy init.
