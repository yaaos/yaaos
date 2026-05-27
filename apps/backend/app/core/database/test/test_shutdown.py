"""core.database.shutdown — closes engine (public API smoke test)."""

from __future__ import annotations

import pytest

from app.core.database.service import shutdown


@pytest.mark.asyncio
async def test_shutdown_clears_engine_singletons() -> None:
    """After shutdown() the engine and sessionmaker singletons are None."""
    from app.core.database.service import get_engine, get_sessionmaker  # noqa: PLC0415

    get_engine()  # materialize lazy singleton
    get_sessionmaker()

    await shutdown()

    import app.core.database.service as svc  # noqa: PLC0415

    assert svc._engine is None
    assert svc._sessionmaker is None


@pytest.mark.asyncio
async def test_shutdown_is_idempotent() -> None:
    """Calling shutdown() twice does not raise."""
    await shutdown()
    await shutdown()
