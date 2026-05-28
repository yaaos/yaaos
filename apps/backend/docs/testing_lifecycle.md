# testing/lifecycle

> Session-end shutdown aggregator that calls web and worker shutdown hooks once per test session.

## Purpose

Provides a single `shutdown_runtime()` call for the test suite's session-scoped autouse fixture, mirroring the production lifespan teardown without duplicating hook lists.

## Public interface

- `shutdown_runtime()` — awaitable. Iterates `iter_web_shutdown_hooks()` then `iter_worker_shutdown_hooks()`; calls each once; swallows exceptions so a failing hook does not abort the sequence.

No HTTP routes; no tables.

## Module architecture

Thin aggregator. Reads from `core/webserver.iter_web_shutdown_hooks` and `core/tasks.iter_worker_shutdown_hooks`. Web hooks before worker hooks — mirrors production shutdown order.

## How it's tested

`test/test_shutdown_runtime.py` — registers stub hooks, asserts both called once, asserts a raising hook does not abort subsequent hooks.
