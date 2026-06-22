"""Shutdown helper that closes every cached Redis client and pub/sub instance."""

from app.core.redis.pubsub import shutdown as _bus_shutdown
from app.core.redis.service import shutdown as _client_shutdown


async def shutdown() -> None:
    """Close every cached client and drop the pub/sub instance.

    Registered on both web and worker shutdown registries. Idempotent.
    """
    await _client_shutdown()
    await _bus_shutdown()
