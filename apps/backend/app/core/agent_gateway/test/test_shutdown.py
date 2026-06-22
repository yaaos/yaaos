"""core.agent_gateway.shutdown — cancels the reconciler task; does not drop
the subscriber-registry ContextVar binding (the registry remains accessible
after shutdown)."""

from __future__ import annotations

import pytest

from app.core.agent_gateway.subscribers import (
    _get,
    shutdown,
)


@pytest.mark.asyncio
async def test_shutdown_does_not_crash() -> None:
    """shutdown() on a fresh registry (no reconciler task) does not raise."""
    # The autouse fixture already bound a fresh registry — verify it's accessible.
    _get()
    await shutdown()


@pytest.mark.asyncio
async def test_shutdown_is_idempotent() -> None:
    """Calling shutdown() twice does not raise."""
    await shutdown()
    await shutdown()


@pytest.mark.asyncio
async def test_registry_accessible_after_shutdown() -> None:
    """After shutdown() the subscriber registry is still accessible — the
    ContextVar is not cleared (only the reconciler task is cancelled)."""
    _get()
    await shutdown()
    # Still accessible — no RuntimeError.
    _get()
