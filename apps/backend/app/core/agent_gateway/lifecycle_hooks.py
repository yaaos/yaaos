"""Activity-subscriber lifecycle hook implementations.

Three callables — `on_attach`, `on_heartbeat`, `on_detach` — wired into
`core/sse.register_activity_subscriber_lifecycle` at `agent_gateway` import
time. They bridge the SSE stream lifecycle to the `SubscriberRegistry` so the
registry accurately reflects which runs have a live UI subscriber, allowing
demand-pull WS control messages to the agent.

All three are async and never raise — failures are logged and swallowed so
a Redis blip or a missing route never kills the SSE stream.

Token format: `{conn_id}|{agent_id}` — a pipe-joined opaque string that
encodes both pieces needed for heartbeat and detach. Chosen because neither
`conn_id` (UUID4 string, no pipe) nor `agent_id` (UUID4 string) contains a
pipe character.
"""

from __future__ import annotations

from uuid import UUID

import structlog

from app.core.agent_gateway.service import resolve_run_route
from app.core.agent_gateway.subscribers import _get as _get_registry
from app.core.database import session as db_session

log = structlog.get_logger("core.agent_gateway.lifecycle_hooks")


async def on_attach(org_id: UUID, run_id: UUID) -> str | None:
    """Track one SSE subscriber for `run_id` in the `SubscriberRegistry`.

    Opens a read-only DB session to resolve `(workspace_id, agent_id)` from
    the most recent `agent_commands` row correlated to `run_id`. Returns an
    opaque token `{conn_id}|{agent_id}` that `on_heartbeat` / `on_detach`
    echo back. Returns `None` when no route exists yet (no `InvokeClaudeCode`
    command dispatched for this run, or the run is agent-scoped) — the SSE
    stream still serves frames; the registry simply has no entry to maintain.

    Failures are swallowed — a registry miss is not fatal to the stream.
    """
    try:
        async with db_session() as session:
            route = await resolve_run_route(run_id, session=session)
        if route is None:
            return None
        workspace_id, agent_id = route
        registry = _get_registry()
        conn_id = await registry.track(
            run_id=run_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
        )
        return f"{conn_id}|{agent_id}"
    except Exception as exc:
        log.warning(
            "lifecycle_hooks.on_attach_failed",
            run_id=str(run_id),
            err=str(exc),
        )
        return None


async def on_heartbeat(run_id: UUID, token: str) -> None:
    """Re-stamp ZSET scores for this connection so the sweeper skips it.

    Parses `token` as `{conn_id}|{agent_id}` (minted by `on_attach`).
    Failures are swallowed — the reconciler picks up divergence within
    its `_RECONCILE_INTERVAL_SECONDS` cadence.
    """
    parts = token.split("|", 1)
    if len(parts) != 2:
        return
    conn_id, agent_id_str = parts
    try:
        agent_id = UUID(agent_id_str)
    except ValueError:
        return
    try:
        registry = _get_registry()
        await registry.heartbeat(run_id=run_id, conn_id=conn_id, agent_id=agent_id)
    except Exception as exc:
        log.warning(
            "lifecycle_hooks.on_heartbeat_failed",
            run_id=str(run_id),
            err=str(exc),
        )


async def on_detach(run_id: UUID, token: str) -> None:
    """Remove one SSE subscriber from the `SubscriberRegistry`.

    Parses `token` as `{conn_id}|{agent_id}` (minted by `on_attach`).
    Calls `registry.untrack(run_id=…, conn_id=…)` which decrements the
    ZSET entry and publishes `unsubscribe` when ZCARD drops to zero.
    Failures are swallowed — `untrack` is best-effort; the sweeper GCs
    stale entries within `_SUBSCRIBER_STALE_THRESHOLD_SECONDS`.
    """
    parts = token.split("|", 1)
    if len(parts) != 2:
        return
    conn_id = parts[0]
    try:
        registry = _get_registry()
        await registry.untrack(run_id=run_id, conn_id=conn_id)
    except Exception as exc:
        log.warning(
            "lifecycle_hooks.on_detach_failed",
            run_id=str(run_id),
            err=str(exc),
        )
