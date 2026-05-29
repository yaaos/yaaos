"""taskiq broker — Redis Streams-backed AsyncBroker.

Lazy-constructed singleton so importing this module doesn't touch Redis.
Worker process boots the broker + registers every `@task` body during
`core.tasks.worker.run()`. Web process never starts the broker — domain
callers' `enqueue()` writes outbox rows; the worker's drain pushes them
to Redis.
"""

from __future__ import annotations

from taskiq import AsyncBroker
from taskiq_redis import ListQueueBroker

from app.core.config import get_settings

_broker: AsyncBroker | None = None


def get_broker() -> AsyncBroker:
    """Return the singleton broker. Construction is lazy and does NOT
    open a connection — `await broker.startup()` does that (called from
    the worker entrypoint).

    taskiq-redis builds its own connection pool from a URL, independent of
    `core/redis`'s client, so the broker reads `settings.redis_url` directly
    rather than going through `core/redis`.
    """
    global _broker
    if _broker is None:
        _broker = ListQueueBroker(url=get_settings().redis_url)
    return _broker


def shutdown() -> None:
    """Drop the cached broker singleton.

    Called from `core.tasks.shutdown()` during process teardown, and by
    tests that construct their own broker (or skip the broker entirely).
    """
    global _broker
    _broker = None
