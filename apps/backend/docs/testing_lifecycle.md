# testing/lifecycle

> Session-end shutdown aggregator that calls web and worker shutdown hooks once per test session.

## Purpose

Provides a single `shutdown_runtime()` call for the test suite's session-scoped autouse fixture. Iterates all registered web hooks then all registered worker hooks, calling each exactly once, so test-session teardown mirrors the production lifespan teardown without duplicating the hook lists.

## Public interface

- `shutdown_runtime()` — awaitable. Iterates `iter_web_shutdown_hooks()` then `iter_worker_shutdown_hooks()`; calls each hook; swallows exceptions so a failing hook does not abort the sequence. Returns `None`.

No HTTP routes; no tables.

## Module architecture

Thin aggregator. No registry of its own — reads from `core/webserver.iter_web_shutdown_hooks` and `core/tasks.iter_worker_shutdown_hooks`. The ordering (web hooks before worker hooks) mirrors the production shutdown sequence.

## Data owned

None.

## How it's tested

`test/test_shutdown_runtime.py` — registers stub web and worker hooks, calls `shutdown_runtime()`, asserts both were called once each. Also asserts a hook that raises does not prevent subsequent hooks from running.

The module is exercised in production at session end via the session-scoped autouse fixture in `apps/backend/conftest.py`.
