"""core/sse_pubsub — pub/sub for ActivityEvent fanout.

Phase 8b foundations ships an in-memory backend that's adequate for
single-instance backends + every test. The Redis-backed variant lands
alongside the worker process in the Phase 8b follow-on (wires
`settings.redis_url`). Channel name shape: `activity:{workflow_execution_id}`.
"""

from app.core.sse_pubsub.service import (
    InMemoryPubsub,
    _reset_for_tests,
    channel_for,
    get_pubsub,
    publish,
    subscribe,
    subscriber_count,
)

__all__ = [
    "InMemoryPubsub",
    "_reset_for_tests",
    "channel_for",
    "get_pubsub",
    "publish",
    "subscribe",
    "subscriber_count",
]
