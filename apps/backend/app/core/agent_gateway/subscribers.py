"""Demand-pull subscriber registry — only forward activity events when a
UI is watching.

`SubscriberRegistry.track(run_id, workspace_id, agent_id)`
is called by an SSE handler when a client connects. The registry writes
shared memberships and a route record through core/cache, then publishes
an `AgentWsControlMessage` on `agent_ws_control:{agent_id}`. The pod whose
WS sender is registered for `agent_id` forwards it to the agent.
`untrack(...)` removes one connection; the last removal publishes `unsubscribe`.

`_senders` stays process-local: it is a live transport handle (the WebSocket
send callable) and can only live where the WS terminates. Cross-pod routing
uses cache pub/sub as the backplane.

`SubscriberReconciler` runs as a background loop on the WS-owning pod every
`_RECONCILE_INTERVAL_SECONDS` seconds. It iterates each known agent's route
memberships, checks subscriber counts, and re-publishes subscribe/unsubscribe
when local state diverges from shared presence — the safety net against pub/sub's
at-most-once delivery.

`subscriber_sweeper` is a `@scheduled` worker task that prunes stale
memberships every minute.

The active `SubscriberRegistry` instance is held in a ContextVar with a lazy
default — production never calls a bind function. `set_subscriber_registry_for_tests`
is the test seam; the `subscriber_registry_isolation` fixture in
`app/testing/isolation` uses it to bind a fresh instance per test.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal
from uuid import UUID

import structlog
from pydantic import BaseModel

from app.core.cache import (
    count_members,
    delete_record,
    list_keys,
    list_members,
    prune_members,
    publish,
    read_record,
    remove_member,
    subscribe,
    touch_member,
    update_record,
)
from app.core.shutdown_registry import register_web_shutdown_hook
from app.core.tasks import scheduled

log = structlog.get_logger("core.agent_gateway.subscribers")

# ── Wire payload ────────────────────────────────────────────────────────────


class AgentWsControlMessage(BaseModel):
    """Typed pub/sub envelope sent between pods on `agent_ws_control:{agent_id}`."""

    type: Literal["subscribe", "unsubscribe"]
    workspace_id: UUID
    run_id: UUID


class RunRouteFields(BaseModel):
    """Gateway-owned routing fields persisted for an active run."""

    workspace_id: UUID
    agent_id: UUID


# ── Constants ────────────────────────────────────────────────────────────────

_RECONCILE_INTERVAL_SECONDS = 5
_SSE_HEARTBEAT_INTERVAL_SECONDS = 30
_SUBSCRIBER_STALE_THRESHOLD_SECONDS = 60

# ── Cache key helpers ─────────────────────────────────────────────────────────


def _run_subscribers_key(run_id: UUID) -> str:
    return f"run_subscribers:{run_id}"


def _run_route_key(run_id: UUID) -> str:
    return f"run_route:{run_id}"


def _agent_routes_key(agent_id: UUID) -> str:
    return f"agent_routes:{agent_id}"


def _control_channel(agent_id: UUID) -> str:
    return f"agent_ws_control:{agent_id}"


# A sender takes the message dict to push to the agent over its WebSocket.
# Returns when the send completes (or raises if the connection is dead).
Sender = Callable[[dict[str, Any]], Awaitable[None]]


class SubscriberRegistry:
    """Tracks shared UI presence and emits typed subscribe/unsubscribe controls.

    Cross-pod state lives in the cache. `_senders` is process-local because the
    WebSocket send callable only exists on the pod where the WS terminates.
    """

    def __init__(self) -> None:
        # Stable identity for this registry instance (= this "pod").
        # Used as the first component of members: "{pod_id}:{conn_id}".
        self._pod_id: str = str(uuid.uuid4())

        # agent_id → sender callable (live transport handle — process-local only).
        self._senders: dict[UUID, Sender] = {}
        # agent_id → asyncio.Task running the control subscription loop.
        self._subscribe_tasks: dict[UUID, asyncio.Task[None]] = {}
        # run_id → set of members tracked by THIS pod (per-connection).
        # Same pod may hold multiple concurrent SSE subscribers to the same run_id;
        # each call to track() gets a unique conn_id so multiple subscriptions are
        # counted independently (matches the agent-side cardinality contract).
        self._connections: dict[UUID, set[str]] = {}
        # agent_id → set of run_ids currently streaming (in-memory tracking
        # for the reconciler to know what the agent is currently sending).
        self._streaming: dict[UUID, set[UUID]] = {}

        self._lock = asyncio.Lock()

    # ── Senders (WebSocket lifecycle) ────────────────────────────────────────

    async def register_sender(self, agent_id: UUID, sender: Sender) -> None:
        """Register the sender callable for an agent's live WebSocket.

        Starts the pub/sub consumer task FIRST and waits for it to
        actually subscribe to `agent_ws_control:{agent_id}` before snapshotting
        state for replay. This ordering ensures any concurrent `track()` from
        another pod that publishes between sender-registration and snapshot is
        delivered via pub/sub rather than lost.

        Then runs an initial reconciliation pass: reads `agent_routes:{agent_id}`
        and for each run_id with subscribers sends the subscribe message directly
        so the agent picks up where it left off on reconnect. Duplicate subscribes
        that arrive via both replay and pub/sub are harmless — the agent handles
        subscribe idempotently.
        """
        async with self._lock:
            self._senders[agent_id] = sender
            self._streaming.setdefault(agent_id, set())

        # Start the pub/sub consumer task BEFORE snapshotting state.
        # Wait until it can receive from the channel so any concurrent
        # publish lands in the subscriber queue rather than the void.
        subscribed_event: asyncio.Event = asyncio.Event()
        task = asyncio.create_task(
            self._run_control_subscriber(agent_id, sender, subscribed_event),
            name=f"subscriber_control:{agent_id}",
        )
        async with self._lock:
            self._subscribe_tasks[agent_id] = task
        try:
            await asyncio.wait_for(subscribed_event.wait(), timeout=5.0)
        except TimeoutError:
            log.warning(
                "subscribers.subscriber_subscribe_timeout",
                agent_id=str(agent_id),
            )

        # Snapshot shared state for replay. Any track() that happened
        # between subscription readiness and this snapshot is delivered
        # via _run_control_subscriber's pub/sub consumer. Cache failure here is
        # non-fatal — the reconciler will pick up state within
        # _RECONCILE_INTERVAL_SECONDS.
        replay: list[AgentWsControlMessage] = []
        try:
            routes = await list_members(_agent_routes_key(agent_id))
            for membership in routes:
                try:
                    run_id = UUID(membership.member)
                except ValueError:
                    continue
                count = await count_members(_run_subscribers_key(run_id))
                if count >= 1:
                    route = await read_record(_run_route_key(run_id))
                    if "workspace_id" in route and "agent_id" in route:
                        try:
                            replay.append(
                                AgentWsControlMessage(
                                    type="subscribe",
                                    workspace_id=UUID(route["workspace_id"]),
                                    run_id=run_id,
                                )
                            )
                        except ValueError:
                            pass
        except Exception as exc:
            log.warning(
                "subscribers.sender_registered_cache_unavailable",
                agent_id=str(agent_id),
                error_type=type(exc).__name__,
            )

        log.debug(
            "subscribers.sender_registered",
            agent_id=str(agent_id),
            resubscribed_count=len(replay),
        )

        # Send replay messages outside the lock.
        for msg in replay:
            try:
                await sender(msg.model_dump(mode="json"))
                async with self._lock:
                    self._streaming.setdefault(agent_id, set()).add(msg.run_id)
            except Exception as exc:
                log.warning(
                    "subscribers.resubscribe_send_failed",
                    agent_id=str(agent_id),
                    workspace_id=str(msg.workspace_id),
                    error_type=type(exc).__name__,
                )

    async def _run_control_subscriber(
        self,
        agent_id: UUID,
        sender: Sender,
        subscribed_event: asyncio.Event | None = None,
    ) -> None:
        """Background task: read `agent_ws_control:{agent_id}` pub/sub and
        dispatch each `AgentWsControlMessage` to the local sender.

        Sets `subscribed_event` once the subscription can receive publications
        so the caller can ensure messages published after `register_sender`
        returns are received. Exits cleanly when the task is cancelled.
        """
        try:
            async for event in subscribe(_control_channel(agent_id), on_subscribed=subscribed_event):
                try:
                    msg = AgentWsControlMessage.model_validate(event)
                except Exception:
                    log.warning(
                        "subscribers.malformed_control_message",
                        agent_id=str(agent_id),
                    )
                    continue

                # Update in-memory streaming state.
                async with self._lock:
                    streaming = self._streaming.setdefault(agent_id, set())
                    if msg.type == "subscribe":
                        streaming.add(msg.run_id)
                    else:
                        streaming.discard(msg.run_id)

                try:
                    await sender(msg.model_dump(mode="json"))
                except Exception as exc:
                    log.warning(
                        "subscribers.control_send_failed",
                        agent_id=str(agent_id),
                        msg_type=msg.type,
                        error_type=type(exc).__name__,
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning(
                "subscribers.control_loop_error",
                agent_id=str(agent_id),
                error_type=type(exc).__name__,
            )

    def unregister_sender(self, agent_id: UUID) -> None:
        """Unregister the sender for `agent_id` and cancel the subscription task.

        Synchronous so the WS handler's `finally` block can call it without
        `await` — dict pops are GIL-protected and must be visible immediately
        after the WebSocket disconnect, before any event-loop yielding that
        could race with a test assertion on `has_sender`. The subscribe task
        is cancelled fire-and-forget; it catches CancelledError and exits cleanly.
        """
        self._senders.pop(agent_id, None)
        self._streaming.pop(agent_id, None)
        task = self._subscribe_tasks.pop(agent_id, None)
        if task is not None:
            task.cancel()
        log.debug("subscribers.sender_unregistered", agent_id=str(agent_id))

    # ── Subscriber lifecycle ─────────────────────────────────────────────────

    async def track(
        self,
        *,
        run_id: UUID,
        workspace_id: UUID,
        agent_id: UUID,
    ) -> str:
        """Record one UI subscriber for `run_id`.

        Returns the `conn_id` minted for this subscription. The caller MUST
        pass the same `conn_id` to `untrack()` when the subscription ends so
        the corresponding member is removed and reference counts stay accurate
        when multiple subscribers share a run_id on the same pod.

        Refreshes subscriber and agent-route memberships, writes the typed
        run route, then publishes a subscribe envelope on the agent's channel.
        """
        conn_id = str(uuid.uuid4())
        member = f"{self._pod_id}:{conn_id}"

        async with self._lock:
            self._connections.setdefault(run_id, set()).add(member)

        last_seen = time.time()
        await touch_member(_run_subscribers_key(run_id), member, last_seen=last_seen)
        await update_record(
            _run_route_key(run_id),
            fields=RunRouteFields(workspace_id=workspace_id, agent_id=agent_id),
        )
        await touch_member(_agent_routes_key(agent_id), str(run_id), last_seen=last_seen)

        msg = AgentWsControlMessage(
            type="subscribe",
            workspace_id=workspace_id,
            run_id=run_id,
        )
        try:
            await publish(_control_channel(agent_id), msg.model_dump(mode="json"))
        except Exception as exc:
            log.warning(
                "subscribers.subscribe_publish_failed",
                agent_id=str(agent_id),
                workspace_id=str(workspace_id),
                error_type=type(exc).__name__,
            )

        return conn_id

    async def untrack(
        self,
        *,
        run_id: UUID,
        conn_id: str,
    ) -> None:
        """Remove ONE UI subscriber for `run_id`, identified by
        the `conn_id` minted at track() time.

        Removes the specific member for (pod_id, conn_id). If none remain,
        resolves both route identities, publishes unsubscribe, deletes the
        route record and removes the agent's route membership.

        No-op if this pod has no member registered for this (run_id, conn_id).
        """
        target_member = f"{self._pod_id}:{conn_id}"
        async with self._lock:
            members = self._connections.get(run_id)
            if members is None or target_member not in members:
                return
            members.discard(target_member)
            if not members:
                del self._connections[run_id]

        await remove_member(_run_subscribers_key(run_id), target_member)
        count = await count_members(_run_subscribers_key(run_id))

        if count == 0:
            route = await read_record(_run_route_key(run_id))
            if route:
                try:
                    agent_id = UUID(route["agent_id"])
                    workspace_id = UUID(route["workspace_id"])
                except KeyError, ValueError:
                    agent_id = None
                    workspace_id = None

                if agent_id is not None and workspace_id is not None:
                    msg = AgentWsControlMessage(
                        type="unsubscribe",
                        workspace_id=workspace_id,
                        run_id=run_id,
                    )
                    try:
                        await publish(_control_channel(agent_id), msg.model_dump(mode="json"))
                    except Exception as exc:
                        log.warning(
                            "subscribers.unsubscribe_publish_failed",
                            agent_id=str(agent_id),
                            error_type=type(exc).__name__,
                        )
                    await delete_record(_run_route_key(run_id))
                    await remove_member(_agent_routes_key(agent_id), str(run_id))

    async def heartbeat(
        self,
        *,
        run_id: UUID,
        conn_id: str,
        agent_id: UUID,
    ) -> None:
        """Refresh both last-seen times to keep healthy subscribers from eviction.

        Called by the owning SSE generator every heartbeat interval. Failures
        are best-effort; the reconciler checks shared state independently.
        """
        member = f"{self._pod_id}:{conn_id}"
        last_seen = time.time()
        try:
            await touch_member(_run_subscribers_key(run_id), member, last_seen=last_seen)
            await touch_member(_agent_routes_key(agent_id), str(run_id), last_seen=last_seen)
        except Exception as exc:
            log.warning(
                "subscribers.heartbeat_failed",
                run_id=str(run_id),
                agent_id=str(agent_id),
                error_type=type(exc).__name__,
            )

    # ── Diagnostics ─────────────────────────────────────────────────────────

    def has_sender(self, agent_id: UUID) -> bool:
        return agent_id in self._senders

    def is_streaming(self, agent_id: UUID, run_id: UUID) -> bool:
        """Return True if this pod believes the agent is streaming `run_id`."""
        return run_id in self._streaming.get(agent_id, set())


# ── Reconciler ────────────────────────────────────────────────────────────────


class SubscriberReconciler:
    """Safety net against pub/sub at-most-once delivery.

    Runs every `_RECONCILE_INTERVAL_SECONDS` on the WS-owning pod. For each
    agent with a registered sender, reads `agent_routes:{agent_id}` and for
    each run_id checks shared presence:
    - Members present and agent not streaming → publish subscribe
    - No members and agent is streaming → publish unsubscribe
    """

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)
            if stop_event.is_set():
                break
            try:
                await self._reconcile_once()
            except Exception as exc:
                log.warning("subscribers.reconcile_error", error_type=type(exc).__name__)

    async def _reconcile_once(self) -> None:
        registry = _get()
        async with registry._lock:
            agent_ids = list(registry._senders.keys())

        for agent_id in agent_ids:
            routes = await list_members(_agent_routes_key(agent_id))
            for membership in routes:
                run_id_str = membership.member
                try:
                    run_id = UUID(run_id_str)
                except ValueError:
                    continue

                count = await count_members(_run_subscribers_key(run_id))
                is_streaming = registry.is_streaming(agent_id, run_id)

                if count >= 1 and not is_streaming:
                    # Agent should be streaming but isn't — re-publish subscribe.
                    route = await read_record(_run_route_key(run_id))
                    if not route:
                        continue
                    try:
                        workspace_id = UUID(route["workspace_id"])
                    except KeyError, ValueError:
                        continue
                    msg = AgentWsControlMessage(
                        type="subscribe",
                        workspace_id=workspace_id,
                        run_id=run_id,
                    )
                    try:
                        await publish(_control_channel(agent_id), msg.model_dump(mode="json"))
                    except Exception as exc:
                        log.warning(
                            "subscribers.reconcile_subscribe_failed",
                            agent_id=str(agent_id),
                            run_id=run_id_str,
                            error_type=type(exc).__name__,
                        )

                elif count == 0 and is_streaming:
                    # Agent is streaming but nobody is watching — publish unsubscribe.
                    route = await read_record(_run_route_key(run_id))
                    if not route:
                        # Route already gone: untrack() already published the
                        # canonical unsubscribe with the real workspace_id. Nothing
                        # useful to send — don't synthesize a placeholder identity.
                        log.debug(
                            "subscribers.reconcile_skip_no_route",
                            agent_id=str(agent_id),
                            run_id=run_id_str,
                        )
                        continue
                    try:
                        workspace_id = UUID(route["workspace_id"])
                    except KeyError, ValueError:
                        log.warning(
                            "subscribers.reconcile_bad_route",
                            agent_id=str(agent_id),
                            run_id=run_id_str,
                        )
                        continue

                    msg = AgentWsControlMessage(
                        type="unsubscribe",
                        workspace_id=workspace_id,
                        run_id=run_id,
                    )
                    try:
                        await publish(_control_channel(agent_id), msg.model_dump(mode="json"))
                    except Exception as exc:
                        log.warning(
                            "subscribers.reconcile_unsubscribe_failed",
                            agent_id=str(agent_id),
                            run_id=run_id_str,
                            error_type=type(exc).__name__,
                        )


# ── Subscriber sweeper (GC for stale memberships) ─────────────────────────────


async def _run_subscriber_sweeper() -> None:
    """Prune memberships at or before the stale cutoff in both literal namespaces.

    Healthy connections refresh last-seen times through heartbeat(). Each
    key is isolated so incompatible stored state cannot abort the sweep.
    """
    cutoff = time.time() - _SUBSCRIBER_STALE_THRESHOLD_SECONDS
    for prefix in ("run_subscribers:", "agent_routes:"):
        for key in await list_keys(prefix=prefix):
            try:
                await prune_members(key, cutoff=cutoff)
            except Exception as exc:
                log.warning(
                    "subscribers.sweeper_key_skip",
                    key=key,
                    error_type=type(exc).__name__,
                )


subscriber_sweeper = scheduled(
    name="subscriber_sweeper",
    cron="* * * * *",
    queue="default",
    max_retries=1,
)(_run_subscriber_sweeper)


# ── ContextVar binding ───────────────────────────────────────────────────────


_registry_var: ContextVar[SubscriberRegistry | None] = ContextVar("_registry_var", default=None)


def _get() -> SubscriberRegistry:
    val = _registry_var.get()
    if val is None:
        val = SubscriberRegistry()
        _registry_var.set(val)
    return val


@contextmanager
def set_subscriber_registry_for_tests() -> Iterator[SubscriberRegistry]:
    """Context manager: bind a fresh `SubscriberRegistry` for the duration.

    Unlike plugin registries, does NOT copy the current default — subscriber
    registries hold asyncio state (running WS connections) that tests must not
    share. Each test gets a clean slate. Restores the prior binding on exit.
    """
    instance = SubscriberRegistry()
    token = _registry_var.set(instance)
    try:
        yield instance
    finally:
        _registry_var.reset(token)


# ── Module lifecycle ──────────────────────────────────────────────────────────

_stop_event: asyncio.Event = asyncio.Event()
_reconciler_task: asyncio.Task[None] | None = None


def set_reconciler_task(task: asyncio.Task[None]) -> None:
    """Store the reconciler task handle so `shutdown()` can cancel + await it.

    The web composition root calls this after spawning the reconciler, instead
    of writing the module global directly.
    """
    global _reconciler_task
    _reconciler_task = task


async def shutdown() -> None:
    """Stop the reconciler loop and drop the registry binding.

    Called by the web-process shutdown registry on SIGTERM. Cancels the
    reconciler task first so its `asyncio.sleep` is interrupted immediately
    rather than blocking shutdown for up to a full reconcile interval.
    """
    _stop_event.set()
    if _reconciler_task is not None:
        _reconciler_task.cancel()
        try:
            await _reconciler_task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass


register_web_shutdown_hook(shutdown)
