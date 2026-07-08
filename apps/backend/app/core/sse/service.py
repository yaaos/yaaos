"""General-event and workspace-activity pipelines — channel naming + SSE
semantics over the `core/redis` JSON pub/sub bus.

General-event pipeline: `publish_general` / `subscribe_general` use a
per-org channel (`{org_id}:general`) with typed `GeneralEventKind`
discriminators. `publish_general_after_commit` stashes events on
`session.info` and flushes them on SQLAlchemy `after_commit` — rollbacks
silently discard stashed events so rolled-back transactions never emit
SPA events.

Workspace-activity pipeline: `publish_workspace_activity` /
`subscribe_workspace_activity` use a per-org-per-run channel
(`{org_id}:workspace_activity:{run_id}`). Raw agent event
dict passed through unchanged — no envelope, no `ts` stamping.

`serialize_for_sse(payload)` formats any dict as an HTTP `text/event-stream`
frame (`data: <json>\n\n`). Both general and activity subscribers use this
before writing to the HTTP response.

This module owns channel naming and event shapes only. JSON encode/decode,
the pub/sub singleton, and connection management all live in `core/redis`.
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

import app.core.redis as redis_client
from app.core.observability import spawn

log = structlog.get_logger("core.sse")


# ---------------------------------------------------------------------------
# GeneralEventKind — closed enum of org-scoped SSE event discriminators
# ---------------------------------------------------------------------------


class GeneralEventKind(StrEnum):
    """Closed set of kinds carried on the general org-scoped SSE channel."""

    TICKET_STATUS_CHANGED = "ticket_status_changed"
    RUN_STATE_CHANGED = "run_state_changed"
    STAGE_STATE_CHANGED = "stage_state_changed"
    ARTIFACT_STORED = "artifact_stored"
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
    AGENT_CHANGED = "agent_changed"


# ---------------------------------------------------------------------------
# General-event after-commit helpers
# ---------------------------------------------------------------------------

_GENERAL_AFTER_COMMIT_KEY = "yaaos_sse_general_pending"


def _channel_for_general(org_id: UUID) -> str:
    """Internal: per-org channel name for general events. NOT in __all__."""
    return f"{org_id}:general"


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
    await redis_client.publish(_channel_for_general(org_id), event)


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
    fire-and-forget via `spawn()` on the next event-loop tick after commit.
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
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — should not happen in production (AsyncSession.commit
        # is awaited). Drop with a warning rather than crash the commit path.
        log.warning("sse.general.flush.no_loop", count=len(pending))
        return
    for org_id, kind, payload in pending:
        spawn("sse.publish_general", publish_general(org_id=org_id, kind=kind, payload=payload))


def subscribe_general(org_id: UUID) -> AsyncIterator[dict[str, Any]]:
    """Async iterator over general org-scoped events for `org_id`.

    Returns an async iterator — consumers do
    `async for event in subscribe_general(org_id)`.
    """
    return redis_client.subscribe(_channel_for_general(org_id))


# ---------------------------------------------------------------------------
# Workspace-activity pipeline
# ---------------------------------------------------------------------------


def _channel_for_workspace_activity(org_id: UUID, run_id: UUID) -> str:
    """Internal: per-org per-run channel name for workspace-activity events. NOT in __all__."""
    return f"{org_id}:workspace_activity:{run_id}"


async def publish_workspace_activity(
    *,
    org_id: UUID,
    run_id: UUID,
    payload: dict[str, Any],
) -> None:
    """Publish a workspace-activity event for a specific org + pipeline run.

    Passes `payload` through unchanged — no envelope, no `ts` stamping.
    Redis fire-and-forget semantics apply.
    """
    await redis_client.publish(_channel_for_workspace_activity(org_id, run_id), payload)


def subscribe_workspace_activity(
    org_id: UUID,
    run_id: UUID,
    on_subscribed: asyncio.Event | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async iterator over workspace-activity events for `org_id` + `run_id`.

    `on_subscribed`, when provided, is set after the Redis SUBSCRIBE handshake
    completes — before the first message is delivered. Callers that need to
    guarantee no messages are lost between subscription setup and a subsequent
    publish should pass an event and await it before publishing.

    Returns an async iterator — consumers do
    `async for event in subscribe_workspace_activity(org_id, run_id)`.
    """
    return redis_client.subscribe(
        _channel_for_workspace_activity(org_id, run_id),
        on_subscribed=on_subscribed,
    )


# ---------------------------------------------------------------------------
# Activity-subscriber lifecycle hooks — demand-pull wiring without an
# import cycle (`core/sse` cannot import `core/agent_gateway`; see
# `register_pipeline_lookup` in `domain/repos` for the same pattern).
# ---------------------------------------------------------------------------

OnActivitySubscriberAttach = Callable[[UUID, UUID], Awaitable[str | None]]
OnActivitySubscriberHeartbeat = Callable[[UUID, str], Awaitable[None]]
OnActivitySubscriberDetach = Callable[[UUID, str], Awaitable[None]]

# Registered once, at `core/agent_gateway` import time. Re-registering
# overwrites (mirrors `core/byok.register_validator`'s reload tolerance).
_on_activity_subscriber_attach: OnActivitySubscriberAttach | None = None
_on_activity_subscriber_heartbeat: OnActivitySubscriberHeartbeat | None = None
_on_activity_subscriber_detach: OnActivitySubscriberDetach | None = None


def register_activity_subscriber_lifecycle(
    *,
    on_attach: OnActivitySubscriberAttach,
    on_heartbeat: OnActivitySubscriberHeartbeat,
    on_detach: OnActivitySubscriberDetach,
) -> None:
    """Register the workspace-activity SSE subscriber lifecycle hooks.

    Called once, at `core/agent_gateway` import time, so the demand-pull
    `SubscriberRegistry` (owned by `core/agent_gateway`) learns about every
    SSE attach/heartbeat/detach without `core/sse` importing `agent_gateway`
    (that edge would cycle — `agent_gateway` already imports `core/sse` to
    publish activity frames).

    `on_attach(org_id, run_id)` returns an opaque token (or `None` when the
    run has no resolvable route) that `on_heartbeat`/`on_detach` echo back.
    Before registration (or when `on_attach` returns `None`), the
    workspace-activity stream serves frames exactly as it did before this
    seam existed — no attach, no heartbeat, no detach.
    """
    global _on_activity_subscriber_attach, _on_activity_subscriber_heartbeat, _on_activity_subscriber_detach
    _on_activity_subscriber_attach = on_attach
    _on_activity_subscriber_heartbeat = on_heartbeat
    _on_activity_subscriber_detach = on_detach


async def _attach_activity_subscriber(org_id: UUID, run_id: UUID) -> str | None:
    """Call the registered `on_attach` hook, or no-op (`None`) when unregistered.

    Internal — NOT in `__all__`. Used by `web.py`'s `_workspace_activity_stream`
    (intra-module submodule import).
    """
    if _on_activity_subscriber_attach is None:
        return None
    return await _on_activity_subscriber_attach(org_id, run_id)


async def _heartbeat_activity_subscriber(run_id: UUID, token: str) -> None:
    """Call the registered `on_heartbeat` hook, or no-op when unregistered.

    Internal — NOT in `__all__`.
    """
    if _on_activity_subscriber_heartbeat is not None:
        await _on_activity_subscriber_heartbeat(run_id, token)


async def _detach_activity_subscriber(run_id: UUID, token: str) -> None:
    """Call the registered `on_detach` hook, or no-op when unregistered.

    Internal — NOT in `__all__`.
    """
    if _on_activity_subscriber_detach is not None:
        await _on_activity_subscriber_detach(run_id, token)


# ---------------------------------------------------------------------------
# SSE wire formatter
# ---------------------------------------------------------------------------


def serialize_for_sse(payload: dict[str, Any]) -> str:
    """Format `payload` as an HTTP `text/event-stream` data frame.

    Returns `data: <json>\\n\\n`. Both general and workspace-activity
    subscribers use this before writing to the HTTP response.
    """
    return f"data: {json.dumps(payload)}\n\n"


def sse_prelude() -> str:
    """Initial SSE comment frame yielded on connect, before any event.

    Flushes the response headers so the client's `EventSource` transitions to
    OPEN and fires `onopen` immediately — without it a stream that blocks
    waiting for its first event never flushes, so a client that missed the
    triggering event (Redis pub/sub has no replay) never learns it is
    connected and never reconciles. Comment frames (lines starting with `:`)
    are ignored by `EventSource` message handlers, so this never surfaces as
    a spurious event.
    """
    return ": connected\n\n"
