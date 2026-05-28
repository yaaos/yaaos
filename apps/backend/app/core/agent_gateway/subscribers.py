"""Demand-pull subscriber registry — only forward activity events when a
UI is watching.

`SubscriberRegistry.track(workflow_execution_id, workspace_id, agent_id, sender)`
is called by an SSE handler when a client connects. The registry
increments a per-workflow counter; on the `0 → 1` transition it dispatches
`{type: "subscribe", workspace_id: ..., workflow_execution_id: ...}` to
the WebSocket whose ID matches `agent_id`. The agent caches the
`workspace_id → workflow_execution_id` mapping so its outbound
`activity_batch` frames carry the right workflow id. Symmetrically,
`untrack(...)` decrements; on the `1 → 0` transition it dispatches an
`unsubscribe` with the same key shape.

The actual WebSocket send is parameterized via `sender: Sender` (an async
callable). That keeps `subscribers.py` free of FastAPI / Starlette
imports and lets tests inject a list-collecting fake.

In-process. Multi-instance backends route the subscribe / unsubscribe
via Redis pub/sub, the same mechanism as the Redis backend for `core/sse`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

import structlog

log = structlog.get_logger("core.agent_gateway.subscribers")


# A sender takes the message dict to push to the agent over its WebSocket.
# Returns when the send completes (or raises if the connection is dead).
Sender = Callable[[dict[str, Any]], Awaitable[None]]


class SubscriberRegistry:
    """Tracks UI subscriber counts per workflow_execution_id and emits
    subscribe / unsubscribe control messages to the agent that owns the
    associated workspace on the 0→1 / 1→0 boundary.

    Process-local.
    """

    def __init__(self) -> None:
        # workflow_execution_id → count of UI subscribers attached.
        self._counts: dict[UUID, int] = {}
        # workflow_execution_id → (workspace_id, agent_id) so we know who
        # to send subscribe / unsubscribe to. Set on first track(); cleared
        # when count returns to 0.
        self._routes: dict[UUID, tuple[UUID, UUID]] = {}
        # agent_id → sender for the agent's live WebSocket. Set when the
        # WS endpoint registers the agent; cleared on disconnect.
        self._senders: dict[UUID, Sender] = {}
        self._lock = asyncio.Lock()

    # ── Senders (WebSocket lifecycle) ──────────────────────────────────

    async def register_sender(self, agent_id: UUID, sender: Sender) -> None:
        """Register the sender callable for an agent's live WebSocket.
        Called by the WS endpoint after upgrade succeeds.

        Reconnect handling (PHASES item 185): if there are already active
        routes whose `agent_id` matches, re-emit `subscribe` messages on
        the new sender so the agent's reconstructed SubscriptionSet
        picks up where the old connection left off. Without this,
        progress events for already-watching UIs would drop until each
        UI detached + re-attached.
        """
        replay: list[dict[str, Any]] = []
        async with self._lock:
            self._senders[agent_id] = sender
            for wfx_id, (workspace_id, route_agent) in self._routes.items():
                if route_agent != agent_id:
                    continue
                replay.append(
                    {
                        "type": "subscribe",
                        "workspace_id": str(workspace_id),
                        "workflow_execution_id": str(wfx_id),
                    }
                )
            log.info(
                "subscribers.sender_registered",
                agent_id=str(agent_id),
                resubscribed_count=len(replay),
            )
        # Send outside the lock so a slow agent can't block other registry ops.
        for message in replay:
            try:
                await sender(message)
            except Exception as exc:
                log.warning(
                    "subscribers.resubscribe_send_failed",
                    agent_id=str(agent_id),
                    workspace_id=message["workspace_id"],
                    err=str(exc),
                )

    async def unregister_sender(self, agent_id: UUID) -> None:
        async with self._lock:
            self._senders.pop(agent_id, None)
            log.info("subscribers.sender_unregistered", agent_id=str(agent_id))

    # ── Subscriber lifecycle ───────────────────────────────────────────

    async def track(
        self,
        *,
        workflow_execution_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
    ) -> None:
        """Increment the count for `workflow_execution_id`. On 0→1 send
        `subscribe` to the workspace's owning agent."""
        send: Sender | None = None
        message: dict[str, Any] | None = None
        async with self._lock:
            count = self._counts.get(workflow_execution_id, 0)
            self._counts[workflow_execution_id] = count + 1
            self._routes[workflow_execution_id] = (workspace_id, agent_id)
            if count == 0:
                send = self._senders.get(agent_id)
                # Agent caches workspace_id → workflow_execution_id from
                # this payload so its outbound `activity_batch` can carry
                # the right `workflow_execution_id` keyed by the
                # `workspace_id` it learned at subscribe time. Without
                # this, the agent would have no way to populate the
                # workflow id on its outbound frames.
                message = {
                    "type": "subscribe",
                    "workspace_id": str(workspace_id),
                    "workflow_execution_id": str(workflow_execution_id),
                }
        if send is not None and message is not None:
            try:
                await send(message)
            except Exception as exc:
                log.warning(
                    "subscribers.subscribe_send_failed",
                    agent_id=str(agent_id),
                    workspace_id=str(workspace_id),
                    err=str(exc),
                )

    async def untrack(self, *, workflow_execution_id: UUID) -> None:
        """Decrement the count for `workflow_execution_id`. On 1→0 send
        `unsubscribe` to the workspace's owning agent. No-op if already 0."""
        send: Sender | None = None
        message: dict[str, Any] | None = None
        async with self._lock:
            count = self._counts.get(workflow_execution_id, 0)
            if count <= 0:
                return
            self._counts[workflow_execution_id] = count - 1
            if count - 1 == 0:
                route = self._routes.pop(workflow_execution_id, None)
                self._counts.pop(workflow_execution_id, None)
                if route is not None:
                    workspace_id, agent_id = route
                    send = self._senders.get(agent_id)
                    # Mirror `subscribe` payload shape so the agent can
                    # drop the cached mapping on the same key it used to
                    # add it.
                    message = {
                        "type": "unsubscribe",
                        "workspace_id": str(workspace_id),
                        "workflow_execution_id": str(workflow_execution_id),
                    }
        if send is not None and message is not None:
            try:
                await send(message)
            except Exception as exc:
                log.warning(
                    "subscribers.unsubscribe_send_failed",
                    err=str(exc),
                )

    # ── Diagnostics ────────────────────────────────────────────────────

    def count(self, workflow_execution_id: UUID) -> int:
        return self._counts.get(workflow_execution_id, 0)

    def has_sender(self, agent_id: UUID) -> bool:
        return agent_id in self._senders


_singleton: SubscriberRegistry | None = None


def get_registry() -> SubscriberRegistry:
    global _singleton
    if _singleton is None:
        _singleton = SubscriberRegistry()
    return _singleton


async def shutdown() -> None:
    """Drop the subscriber registry singleton. Called by the web-process shutdown registry."""
    global _singleton
    _singleton = None


def _reset_subscriber_singleton_for_tests() -> None:
    """Drop the subscriber-registry singleton synchronously. Intra-module test
    helper — reach for it via direct submodule import from this module's own
    `test/` directory. Not part of the public interface."""
    global _singleton
    _singleton = None
