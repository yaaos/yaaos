"""Service-level coverage of the eager-default subscriber-registry pattern.

The ContextVar holds a `SubscriberRegistry` instance by default — no explicit
bind call is required. `set_subscriber_registry_for_tests` is the per-block
seam used by the `subscriber_registry_isolation` autouse fixture.
"""

from __future__ import annotations

from uuid import uuid4

from app.core.agent_gateway import set_subscriber_registry_for_tests
from app.core.agent_gateway.subscribers import SubscriberRegistry, _get


def test_subscriber_registry_autouse_provides_fresh_instance() -> None:
    """The autouse subscriber_registry_isolation fixture provides a fresh
    SubscriberRegistry with no senders for an arbitrary agent_id."""
    registry = _get()
    assert not registry.has_sender(uuid4())


def test_set_subscriber_registry_for_tests_swaps_instance() -> None:
    """set_subscriber_registry_for_tests() binds a fresh instance inside the
    block and restores the prior instance on exit."""
    outer = _get()
    with set_subscriber_registry_for_tests() as inner:
        assert isinstance(inner, SubscriberRegistry)
        assert _get() is inner
        assert inner is not outer
    # Restored on exit.
    assert _get() is outer


def test_mutations_inside_block_are_isolated() -> None:
    """Mutations to a registry inside a set_subscriber_registry_for_tests block
    do not leak into the outer context."""
    outer = _get()
    fake_agent = uuid4()

    with set_subscriber_registry_for_tests() as inner:
        # Mutate the inner registry.
        inner._senders[fake_agent] = lambda _: None  # type: ignore[assignment]
        assert inner.has_sender(fake_agent)
        # Outer not affected.
        assert not outer.has_sender(fake_agent)

    # Restored to outer — mutation is gone from the current context.
    assert not _get().has_sender(fake_agent)
