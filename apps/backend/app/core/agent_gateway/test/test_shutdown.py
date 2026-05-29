"""core.agent_gateway.shutdown — drops subscriber registry ContextVar binding."""

from __future__ import annotations

import pytest

from app.core.agent_gateway.subscribers import (
    _registry_var,
    get_registry,
    shutdown,
)


@pytest.mark.asyncio
async def test_shutdown_drops_binding() -> None:
    """After shutdown() the ContextVar holds None; get_registry() raises."""
    # The autouse fixture already bound a fresh registry — verify it's accessible.
    get_registry()
    assert _registry_var.get() is not None

    await shutdown()
    assert _registry_var.get() is None


@pytest.mark.asyncio
async def test_shutdown_is_idempotent() -> None:
    """Calling shutdown() twice does not raise."""
    await shutdown()
    await shutdown()


@pytest.mark.asyncio
async def test_get_registry_raises_after_shutdown() -> None:
    """Once shutdown drops the binding, get_registry() raises RuntimeError."""
    await shutdown()
    with pytest.raises(RuntimeError, match="subscriber registry not bound"):
        get_registry()
