"""Demand-pull subscriber registry — only forward activity events when a
UI is watching.

`SubscriberRegistry.track(run_id, workspace_id, agent_id)`
is called by an SSE handler when a client connects. The registry writes to
Redis (`run_subscribers:{run_id}` ZSET, `run_route:{run_id}` HASH,
`agent_routes:{agent_id}` ZSET) and publishes an `AgentWsControlMessage` on
the `agent_ws_control:{agent_id}` pub/sub channel. The pod whose WS sender
is registered for `agent_id` receives the control message and forwards it
to the agent. Symmetrically, `untrack(...)` removes the ZSET member and
when ZCARD drops to 0 publishes `unsubscribe`.

`_senders` stays process-local: it is a live transport handle (the WebSocket
send callable) and can only live where the WS terminates. Cross-pod routing
uses Redis pub/sub as the backplane.

`SubscriberReconciler` runs as a background loop on the WS-owning pod every
`_RECONCILE_INTERVAL_SECONDS` seconds. It iterates each known agent's route
set, checks ZCARD truth, and re-publishes subscribe/unsubscribe when the
in-memory state diverges from Redis — the safety net against pub/sub's
at-most-once delivery.

`subscriber_sweeper` is a `@scheduled` worker task that GCs stale ZSET
entries every minute via `ZREMRANGEBYSCORE`.

The active `SubscriberRegistry` instance is held in a ContextVar with an eager
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

from app.core.redis import (
    hash_delete,
    hash_get_all,
    hash_set,
    publish,
    scan_keys,
    subscribe,
    zset_add_member,
    zset_card,
    zset_members,
    zset_remove_by_score,
    zset_remove_member,
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


# ── Constants ────────────────────────────────────────────────────────────────

_RECONCILE_INTERVAL_SECONDS = 5
_SSE_HEARTBEAT_INTERVAL_SECONDS = 30
_SUBSCRIBER_STALE_THRESHOLD_SECONDS = 60

# ── Redis key helpers ─────────────────────────────────────────────────────────


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
    """Tracks UI subscriber presence in Redis and emits typed subscribe /
    unsubscribe control messages to the agent that owns the associated
    workspace's WebSocket — across pods.

    Cross-pod state lives in Redis. `_senders` is process-local because the
    WebSocket send callable only exists on the pod where the WS terminates.
    """

    def __init__(self) -> None:
        # Stable identity for this registry instance (= this "pod").
        # Used as the first component of ZSET members: "{pod_id}:{conn_id}".
        self._pod_id: str = str(uuid.uuid4())

        # agent_id → sender callable (live transport handle — process-local only).
        self._senders: dict[UUID, Sender] = {}
        # agent_id → asyncio.Task running the Redis subscribe consumer loop.
        self._subscribe_tasks: dict[UUID, asyncio.Task[None]] = {}
        # run_id → set of ZSET members tracked by THIS pod (per-connection).
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

        Starts the Redis pub/sub consumer task FIRST and waits for it to
        actually subscribe to `agent_ws_control:{agent_id}` before snapshotting
        state for replay. This ordering ensures any concurrent `track()` from
        another pod that publishes between sender-registration and snapshot is
        delivered via pub/sub rather than lost.

        Then runs an initial reconciliation pass: reads `agent_routes:{agent_id}`
        and for each run_id with ZCARD ≥ 1 sends the subscribe message directly
        so the agent picks up where it left off on reconnect. Duplicate subscribes
        that arrive via both replay and pub/sub are harmless — the agent handles
        subscribe idempotently.
        """
        async with self._lock:
            self._senders[agent_id] = sender
            self._streaming.setdefault(agent_id, set())

        # 1. Start the Redis pub/sub consumer task BEFORE snapshotting state.
        # Wait until it has actually SUBSCRIBED to the channel so any concurrent
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

        # 2. Now snapshot Redis state for replay. Any track() that happened
        # between the subscribe handshake and this snapshot will be delivered
        # via _run_control_subscriber's pub/sub consumer. Redis failure here is
        # non-fatal — the reconciler will pick up state within
        # _RECONCILE_INTERVAL_SECONDS.
        replay: list[AgentWsControlMessage] = []
        try:
            run_ids = await zset_members(_agent_routes_key(agent_id))
            for run_id_str in run_ids:
                try:
                    run_id = UUID(run_id_str)
                except ValueError:
                    continue
                card = await zset_card(_run_subscribers_key(run_id))
                if card >= 1:
                    route = await hash_get_all(_run_route_key(run_id))
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
                "subscribers.sender_registered_redis_unavailable",
                agent_id=str(agent_id),
                err=str(exc),
            )

        log.debug(
            "subscribers.sender_registered",
            agent_id=str(agent_id),
            resubscribed_count=len(replay),
        )

        # 3. Send replay messages outside the lock.
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
                    err=str(exc),
                )

    async def _run_control_subscriber(
        self,
        agent_id: UUID,
        sender: Sender,
        subscribed_event: asyncio.Event | None = None,
    ) -> None:
        """Background task: read `agent_ws_control:{agent_id}` pub/sub and
        dispatch each `AgentWsControlMessage` to the local sender.

        Sets `subscribed_event` once the Redis SUBSCRIBE handshake completes
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
                        event=event,
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
                        err=str(exc),
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.warning(
                "subscribers.control_loop_error",
                agent_id=str(agent_id),
                err=str(exc),
            )

    def unregister_sender(self, agent_id: UUID) -> None:
        """Unregister the sender for `agent_id` and cancel the Redis subscribe task.

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
        the corresponding ZSET member is removed and reference counts stay
        accurate when multiple subscribers share a run_id on the same pod.

        Writes to Redis:
        - ZADD `run_subscribers:{run_id}` member=`{pod_id}:{conn_id}` score=now
        - HSET `run_route:{run_id}` {workspace_id, agent_id}
        - ZADD `agent_routes:{agent_id}` member={run_id} score=now

        Then PUBLISH `agent_ws_control:{agent_id}` with a subscribe envelope.
        """
        conn_id = str(uuid.uuid4())
        member = f"{self._pod_id}:{conn_id}"

        async with self._lock:
            self._connections.setdefault(run_id, set()).add(member)

        score = time.time()
        await zset_add_member(_run_subscribers_key(run_id), member, score)
        await hash_set(
            _run_route_key(run_id),
            {
                "workspace_id": str(workspace_id),
                "agent_id": str(agent_id),
            },
        )
        await zset_add_member(_agent_routes_key(agent_id), str(run_id), score)

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
                err=str(exc),
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

        Removes the specific member for (pod_id, conn_id) from the ZSET. If
        ZCARD drops to 0:
        - reads `run_route:{run_id}` to resolve the agent_id
        - publishes unsubscribe envelope on `agent_ws_control:{agent_id}`
        - DEL `run_route:{run_id}`
        - ZREM `agent_routes:{agent_id} {run_id}`

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

        await zset_remove_member(_run_subscribers_key(run_id), target_member)
        card = await zset_card(_run_subscribers_key(run_id))

        if card == 0:
            route = await hash_get_all(_run_route_key(run_id))
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
                            err=str(exc),
                        )
                    await hash_delete(_run_route_key(run_id))
                    await zset_remove_member(_agent_routes_key(agent_id), str(run_id))

    async def heartbeat(
        self,
        *,
        run_id: UUID,
        conn_id: str,
        agent_id: UUID,
    ) -> None:
        """Re-stamp this connection's ZSET scores so the sweeper doesn't falsely
        evict a healthy long-lived subscriber.

        Called periodically (every `_SSE_HEARTBEAT_INTERVAL_SECONDS`) by the
        SSE generator that owns the subscription. Touches the same two ZSETs
        that `track()` wrote: `run_subscribers:{run_id}` and
        `agent_routes:{agent_id}`. No-op (logged) if Redis is unavailable —
        the reconciler will pick up state divergence within
        `_RECONCILE_INTERVAL_SECONDS`.
        """
        member = f"{self._pod_id}:{conn_id}"
        score = time.time()
        try:
            await zset_add_member(_run_subscribers_key(run_id), member, score)
            await zset_add_member(_agent_routes_key(agent_id), str(run_id), score)
        except Exception as exc:
            log.warning(
                "subscribers.heartbeat_failed",
                run_id=str(run_id),
                agent_id=str(agent_id),
                err=str(exc),
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
    each run_id checks ZCARD truth:
    - ZCARD ≥ 1 and agent not streaming → publish subscribe
    - ZCARD == 0 and agent is streaming → publish unsubscribe
    """

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await asyncio.sleep(_RECONCILE_INTERVAL_SECONDS)
            if stop_event.is_set():
                break
            try:
                await self._reconcile_once()
            except Exception as exc:
                log.warning("subscribers.reconcile_error", err=str(exc))

    async def _reconcile_once(self) -> None:
        registry = _get()
        async with registry._lock:
            agent_ids = list(registry._senders.keys())

        for agent_id in agent_ids:
            run_ids_str = await zset_members(_agent_routes_key(agent_id))
            for run_id_str in run_ids_str:
                try:
                    run_id = UUID(run_id_str)
                except ValueError:
                    continue

                card = await zset_card(_run_subscribers_key(run_id))
                is_streaming = registry.is_streaming(agent_id, run_id)

                if card >= 1 and not is_streaming:
                    # Agent should be streaming but isn't — re-publish subscribe.
                    route = await hash_get_all(_run_route_key(run_id))
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
                            err=str(exc),
                        )

                elif card == 0 and is_streaming:
                    # Agent is streaming but nobody is watching — publish unsubscribe.
                    route = await hash_get_all(_run_route_key(run_id))
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
                            err=str(exc),
                        )


# ── Subscriber sweeper (GC for stale ZSET entries) ───────────────────────────


async def _run_subscriber_sweeper() -> None:
    """Garbage-collect stale ZSET entries.

    Scans both `run_subscribers:*` (per-run subscriber memberships) and
    `agent_routes:*` (per-agent route memberships). For each key removes
    entries older than `_SUBSCRIBER_STALE_THRESHOLD_SECONDS`. Healthy live
    connections re-stamp their scores via `SubscriberRegistry.heartbeat()`
    on a `_SSE_HEARTBEAT_INTERVAL_SECONDS` cadence; only members for
    connections that died without untracking get reaped.
    """
    now = time.time()
    cutoff = now - _SUBSCRIBER_STALE_THRESHOLD_SECONDS
    for pattern in ("run_subscribers:*", "agent_routes:*"):
        for key in await scan_keys(pattern):
            try:
                await zset_remove_by_score(key, 0, cutoff)
            except Exception as exc:
                # Skip keys of the wrong type (e.g. plain SETs left by a prior
                # deployment before the SET→ZSET migration). Log and continue so
                # one bad key doesn't abort the full sweep.
                log.warning(
                    "subscribers.sweeper_key_skip",
                    key=key,
                    err=str(exc),
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
