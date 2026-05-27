"""core/events — in-process pub/sub for SSE broadcasting."""

from app.core.events import web  # noqa: F401 — registers SSE route at import time
from app.core.events.service import (
    Event,
    EventFilter,
    publish,
    publish_after_commit,
    serialize_for_sse,
    stream_events_for_filter,
    subscribe,
    subscriber_count,
)

__all__ = [
    "Event",
    "EventFilter",
    "publish",
    "publish_after_commit",
    "serialize_for_sse",
    "stream_events_for_filter",
    "subscribe",
    "subscriber_count",
]
