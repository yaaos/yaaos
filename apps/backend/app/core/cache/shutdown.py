"""Shared web/worker lifecycle cleanup for the bound cache provider."""

from app.core.cache.service import _get, _operation


async def shutdown() -> None:
    """Close resources without opening unused connections or clearing shared data."""
    with _operation():
        await _get().shutdown()
