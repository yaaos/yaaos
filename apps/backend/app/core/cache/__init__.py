"""Provider-neutral records, presence, replay claims, rate limits, and events."""

from app.core.cache.service import (
    allow_hit,
    claim_once,
    clear_prefix,
    count_members,
    delete_record,
    is_healthy,
    list_keys,
    list_members,
    prune_members,
    publish,
    read_record,
    remove_member,
    set_cache_for_tests,
    subscribe,
    subscriber_count,
    touch_member,
    update_record,
)
from app.core.cache.shutdown import shutdown
from app.core.cache.types import CacheError, CacheValidationError, Membership
from app.core.shutdown_registry import register_web_shutdown_hook, register_worker_shutdown_hook

__all__ = [
    "CacheError",
    "CacheValidationError",
    "Membership",
    "allow_hit",
    "claim_once",
    "clear_prefix",
    "count_members",
    "delete_record",
    "is_healthy",
    "list_keys",
    "list_members",
    "prune_members",
    "publish",
    "read_record",
    "remove_member",
    "set_cache_for_tests",
    "shutdown",
    "subscribe",
    "subscriber_count",
    "touch_member",
    "update_record",
]

register_web_shutdown_hook(shutdown)
register_worker_shutdown_hook(shutdown)
