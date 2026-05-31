"""Service-level coverage of the ContextVar-based subscriber-registry binding."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.agent_gateway import (
    SubscriberRegistry,
    bind_subscriber_registry,
    get_subscriber_registry,
)

# ── SubscriberRegistry binding ─────────────────────────────────────────


def test_bind_subscriber_registry_produces_fresh_registry() -> None:
    """After the autouse fixture binds a fresh registry, there are no senders."""
    registry = get_subscriber_registry()
    # A freshly-bound registry has no senders for an arbitrary agent_id.
    assert not registry.has_sender(uuid4())


def test_fresh_bind_hides_prior_registry_state() -> None:
    """Binding a new SubscriberRegistry after mutations hides prior state."""
    # Inject a dummy agent_id into the current registry's internal structure.
    registry = get_subscriber_registry()
    fake_agent = uuid4()
    registry._senders[fake_agent] = lambda _: None  # type: ignore[assignment]
    assert registry.has_sender(fake_agent)

    # Rebind to a fresh registry — state is gone.
    bind_subscriber_registry(SubscriberRegistry())
    assert not get_subscriber_registry().has_sender(fake_agent)


def test_get_registry_raises_before_bind() -> None:
    """Deliberately unbind and verify the fail-fast RuntimeError fires."""
    from app.core.agent_gateway.subscribers import _registry_var  # noqa: PLC0415

    token = _registry_var.set(None)
    try:
        with pytest.raises(RuntimeError, match="subscriber registry not bound"):
            get_subscriber_registry()
    finally:
        _registry_var.reset(token)
        bind_subscriber_registry(SubscriberRegistry())
