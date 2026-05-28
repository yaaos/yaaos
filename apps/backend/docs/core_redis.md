# core/redis

> Single Redis access point — client construction, pub/sub primitives, health ping.

## Scope

- Owns: loop-bound client cache, `publish`/`subscribe`, `ping`, shutdown registration.
- Does NOT own: channel naming, JSON encoding, or higher-level semantics — consumers add those.

## Why / invariants

**Per-loop client cache** — redis-py's async client binds its connection pool to the event loop where the first command ran. Reusing one client across loops fails with "Future attached to a different loop". `_clients: dict[int, Redis]` keyed by `id(asyncio.get_running_loop())` gives each loop its own client transparently.

**`decode_responses=False`** — returns bytes. Consumers encode their own payloads.

**Self-registers `shutdown()`** with both web and worker shutdown registries at import time. No explicit caller required. See [patterns.md § Two process lifecycles, two registries](patterns.md).

