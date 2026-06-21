"""core/redis pub/sub eager-default ContextVar contract.

Verifies: `_get()` returns a valid instance without any explicit bind call
(eager default); `set_pubsub_for_tests` provides per-block isolation; two
consecutive CM invocations in different contexts are independent.
"""

from __future__ import annotations

import contextvars

from app.core.redis.pubsub import _get, _RedisPubsub, set_pubsub_for_tests


def test_get_returns_eager_default() -> None:
    """_get() works without any explicit bind — the eager default is active."""
    instance = _get()
    assert isinstance(instance, _RedisPubsub)


def test_set_pubsub_for_tests_yields_fresh_instance() -> None:
    """set_pubsub_for_tests() installs a fresh _RedisPubsub inside the block
    and restores the prior value on exit."""
    outer = _get()
    with set_pubsub_for_tests() as inner:
        assert isinstance(inner, _RedisPubsub)
        assert _get() is inner
        assert inner is not outer
    # Restored on exit.
    assert _get() is outer


def test_nested_set_pubsub_isolates_independently() -> None:
    """Two nested set_pubsub_for_tests blocks each install their own instance
    and restore correctly on exit — ContextVar stacking works."""
    outer = _get()
    with set_pubsub_for_tests() as mid:
        assert _get() is mid
        with set_pubsub_for_tests() as inner:
            assert _get() is inner
            assert inner is not mid
        # Restored to mid after inner exits.
        assert _get() is mid
    # Restored to outer after mid exits.
    assert _get() is outer


def test_two_separate_contexts_isolate_bindings() -> None:
    """Bindings in two separate Context copies are independent."""
    ctx_a = contextvars.copy_context()
    ctx_b = contextvars.copy_context()

    result: dict[str, _RedisPubsub | None] = {"a": None, "b": None}

    def _run_a() -> None:
        with set_pubsub_for_tests() as inst:
            result["a"] = inst

    def _run_b() -> None:
        with set_pubsub_for_tests() as inst:
            result["b"] = inst

    ctx_a.run(_run_a)
    ctx_b.run(_run_b)

    assert result["a"] is not None
    assert result["b"] is not None
    assert result["a"] is not result["b"]
