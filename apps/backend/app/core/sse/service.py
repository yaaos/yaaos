"""General-event and workspace-activity pipelines — channel naming + JSON
encode/decode over `core/redis`.

General-event pipeline: `publish_general` / `subscribe_general` use a
per-org channel (`{org_id}:general`) with typed `GeneralEventKind`
discriminators. `publish_general_after_commit` stashes events on
`session.info` and flushes them on SQLAlchemy `after_commit` — rollbacks
silently discard stashed events so rolled-back transactions never emit
SPA events.

Workspace-activity pipeline: `publish_workspace_activity` /
`subscribe_workspace_activity` use a per-org-per-workflow channel
(`{org_id}:workspace_activity:{workflow_execution_id}`). Raw agent event
dict passed through unchanged — no envelope, no `ts` stamping.

`serialize_for_sse(payload)` formats any dict as an HTTP `text/event-stream`
frame (`data: <json>\n\n`). Both general and activity subscribers use this
before writing to the HTTP response.

The Pydantic-encoded payload crosses the seam as a `dict[str, Any]`
serialized to JSON; this module owns the encode/decode. `core/redis`
handles connection management — there's no Redis client construction
here anymore.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from enum import StrEnum
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core import redis as redis_client

log = structlog.get_logger("core.sse")


# ---------------------------------------------------------------------------
# GeneralEventKind — closed enum of org-scoped SSE event discriminators
# ---------------------------------------------------------------------------


class GeneralEventKind(StrEnum):
    """Closed set of kinds carried on the general org-scoped SSE channel."""

    TICKET_STATUS_CHANGED = "ticket_status_changed"
    REVIEW_REQUESTED = "review_requested"
    REVIEW_STARTED = "review_started"
    REVIEW_COMPLETED = "review_completed"
    REVIEW_FAILED = "review_failed"
    REVIEW_SUPERSEDED = "review_superseded"
    FINDING_RAISED = "finding_raised"
    FINDING_RE_OBSERVED = "finding_re_observed"
    FINDING_ANCHOR_UPDATED = "finding_anchor_updated"
    FINDING_STATE_CHANGED = "finding_state_changed"
    FINDING_ACKNOWLEDGED = "finding_acknowledged"
    FINDING_RESOLUTION_DETECTED = "finding_resolution_detected"
    FINDING_STALE_DETECTED = "finding_stale_detected"
    COMMENT_REPLY_RECEIVED = "comment_reply_received"
    AGENT_REPLY_POSTED = "agent_reply_posted"


# ---------------------------------------------------------------------------
# General-event after-commit helpers
# ---------------------------------------------------------------------------

_GENERAL_AFTER_COMMIT_KEY = "yaaos_sse_general_pending"

# Strong refs to in-flight publish_general() tasks so asyncio doesn't GC them
# mid-fan-out (Python's event loop only holds weak refs to create_task tasks).
_inflight_general_tasks: set[asyncio.Task[None]] = set()


def _channel_for_general(org_id: UUID) -> str:
    """Internal: per-org channel name for general events. NOT in __all__."""
    return f"{org_id}:general"


class RedisPubsub:
    """Redis pub/sub bus. Channel naming + JSON encode/decode on top
    of `core/redis`. `subscriber_count` is local-process — Redis's
    `PUBSUB NUMSUB` is cluster-wide and not what callers want.
    """

    def __init__(self) -> None:
        self._local_counts: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        self._local_counts.clear()

    async def publish(self, channel: str, event: dict[str, Any]) -> int:
        """Publish `event` (JSON-serialized) on `channel`. Returns the
        cluster-wide delivery count. Returns 0 when nobody is listening."""
        payload = json.dumps(event).encode()
        return await redis_client.publish(channel, payload)

    async def subscribe(self, channel: str) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over events on `channel`. Local subscriber count
        is incremented on entry, decremented on iterator close."""
        async with self._lock:
            self._local_counts[channel] = self._local_counts.get(channel, 0) + 1
        try:
            async for payload in redis_client.subscribe(channel):
                try:
                    yield json.loads(payload.decode())
                except json.JSONDecodeError:
                    log.warning("sse.malformed_payload", channel=channel)
                    continue
        finally:
            async with self._lock:
                cur = self._local_counts.get(channel, 0)
                if cur <= 1:
                    self._local_counts.pop(channel, None)
                else:
                    self._local_counts[channel] = cur - 1

    def subscriber_count(self, channel: str) -> int:
        """Local-process subscriber count for diagnostics / tests."""
        return self._local_counts.get(channel, 0)


_singleton: RedisPubsub | None = None


def get_pubsub() -> RedisPubsub:
    """Process-singleton pub/sub."""
    global _singleton
    if _singleton is None:
        _singleton = RedisPubsub()
    return _singleton


async def shutdown() -> None:
    """Drop the singleton. Called by web- and worker-process shutdown registries."""
    global _singleton
    if _singleton is not None:
        await _singleton.aclose()
    _singleton = None


def reset_pubsub() -> None:
    """Drop the singleton synchronously. For test isolation only — production
    code uses the async `shutdown()` via the web shutdown registry."""
    global _singleton
    _singleton = None


def subscriber_count(channel: str) -> int:
    return get_pubsub().subscriber_count(channel)


# ---------------------------------------------------------------------------
# General-event public helpers
# ---------------------------------------------------------------------------


async def publish_general(
    *,
    org_id: UUID,
    kind: GeneralEventKind,
    payload: dict[str, Any],
) -> None:
    """Publish a general org-scoped event to all subscribers on that org's channel.

    Stamps `ts` server-side (ISO UTC). Redis semantics: fire-and-forget, no persistence.
    """
    ts = datetime.datetime.now(datetime.UTC).isoformat()
    event: dict[str, Any] = {"kind": kind.value, "ts": ts, **payload}
    await get_pubsub().publish(_channel_for_general(org_id), event)


def publish_general_after_commit(
    session: AsyncSession,
    *,
    org_id: UUID,
    kind: GeneralEventKind,
    payload: dict[str, Any],
) -> None:
    """Queue a general event to publish when this session commits.

    Rollback silently discards the stashed entry — rolled-back transactions
    never emit SPA events. No await needed at the call site; the flush runs
    on the next event-loop tick after commit via `asyncio.create_task`.
    """
    pending: list[tuple[UUID, GeneralEventKind, dict[str, Any]]] = session.sync_session.info.setdefault(
        _GENERAL_AFTER_COMMIT_KEY, []
    )
    pending.append((org_id, kind, payload))


@sa_event.listens_for(Session, "after_commit")
def _flush_general_pending(sync_session: Session) -> None:
    pending: list[tuple[UUID, GeneralEventKind, dict[str, Any]]] | None = sync_session.info.pop(
        _GENERAL_AFTER_COMMIT_KEY, None
    )
    if not pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — should not happen in production (AsyncSession.commit
        # is awaited). Drop with a warning rather than crash the commit path.
        log.warning("sse.general.flush.no_loop", count=len(pending))
        return
    for org_id, kind, payload in pending:
        task = loop.create_task(publish_general(org_id=org_id, kind=kind, payload=payload))
        _inflight_general_tasks.add(task)
        task.add_done_callback(_inflight_general_tasks.discard)


def subscribe_general(org_id: UUID) -> AsyncIterator[dict[str, Any]]:
    """Async iterator over general org-scoped events for `org_id`.

    Returns an async iterator — consumers do
    `async for event in subscribe_general(org_id)`.
    """
    return get_pubsub().subscribe(_channel_for_general(org_id))


# ---------------------------------------------------------------------------
# Workspace-activity pipeline
# ---------------------------------------------------------------------------


def _channel_for_workspace_activity(org_id: UUID, workflow_execution_id: UUID) -> str:
    """Internal: per-org per-workflow channel name for workspace-activity events. NOT in __all__."""
    return f"{org_id}:workspace_activity:{workflow_execution_id}"


async def publish_workspace_activity(
    *,
    org_id: UUID,
    workflow_execution_id: UUID,
    payload: dict[str, Any],
) -> None:
    """Publish a workspace-activity event for a specific org + workflow execution.

    Passes `payload` through unchanged — no envelope, no `ts` stamping.
    Redis fire-and-forget semantics apply.
    """
    await get_pubsub().publish(_channel_for_workspace_activity(org_id, workflow_execution_id), payload)


def subscribe_workspace_activity(org_id: UUID, workflow_execution_id: UUID) -> AsyncIterator[dict[str, Any]]:
    """Async iterator over workspace-activity events for `org_id` + `workflow_execution_id`.

    Returns an async iterator — consumers do
    `async for event in subscribe_workspace_activity(org_id, wfx_id)`.
    """
    return get_pubsub().subscribe(_channel_for_workspace_activity(org_id, workflow_execution_id))


# ---------------------------------------------------------------------------
# SSE wire formatter
# ---------------------------------------------------------------------------


def serialize_for_sse(payload: dict[str, Any]) -> str:
    """Format `payload` as an HTTP `text/event-stream` data frame.

    Returns `data: <json>\\n\\n`. Both general and workspace-activity
    subscribers use this before writing to the HTTP response.
    """
    return f"data: {json.dumps(payload)}\n\n"


# ---------------------------------------------------------------------------
# Workspace-activity ownership registrar (boot-time dependency injection)
# ---------------------------------------------------------------------------

# `core/sse` must not import `domain/*`. The workspace_activity HTTP route
# nonetheless needs a 404-on-cross-org ownership check that knows about
# tickets + workflow rows (domain concerns). The control-plane bootstrap
# registers the dep at startup; the route consumes it via a Depends thunk.
_workspace_activity_ownership_check: Callable[[UUID], Awaitable[None]] | None = None


def register_workspace_activity_ownership_check(
    check: Callable[[UUID], Awaitable[None]],
) -> None:
    """Boot-time registrar for the workspace-activity ownership check.

    Idempotent for the same callable (re-registering is a no-op so worker +
    web boots can both wire it). Registering a *different* callable while
    one is already set raises — catches double-registration in tests.
    """
    global _workspace_activity_ownership_check
    if _workspace_activity_ownership_check is None:
        _workspace_activity_ownership_check = check
        return
    if _workspace_activity_ownership_check is check:
        return
    raise RuntimeError(
        "register_workspace_activity_ownership_check: a different callable is already registered"
    )


async def verify_workspace_activity_ownership(workflow_execution_id: UUID) -> None:
    """Delegate to the registered ownership check; raise if unregistered.

    Pure (framework-agnostic) so it stays in the service layer. `core/sse/web`
    wraps it in a FastAPI `Depends` thunk that plucks `workflow_execution_id`
    from the path.
    """
    if _workspace_activity_ownership_check is None:
        raise RuntimeError(
            "workspace_activity ownership check is not registered; "
            "bootstrap must call register_workspace_activity_ownership_check"
        )
    await _workspace_activity_ownership_check(workflow_execution_id)


def reset_workspace_activity_ownership_check() -> None:
    """Drop the registered ownership check. For test isolation only."""
    global _workspace_activity_ownership_check
    _workspace_activity_ownership_check = None
