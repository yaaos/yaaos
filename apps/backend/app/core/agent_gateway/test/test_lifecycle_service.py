"""Service tests: workspace-agent lifecycle column + mark_agent_configured + heartbeat SSE.

Tests: (a) fresh ensure_agent_row → lifecycle='unconfigured'; (b) UPSERT preserves
'draining'; (c) UPSERT from 'shutdown' resets to 'unconfigured'; (d) mark_agent_configured
CAS flips unconfigured → active and publishes agent_changed; (e) CAS is a no-op on active;
(f) record_agent_event on ConfigUpdate completed_success flips lifecycle to 'active';
(g) handle_heartbeat publishes agent_changed.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest
from sqlalchemy import select

from app.core.agent_gateway.models import WorkspaceAgentRow

# ── Helpers ───────────────────────────────────────────────────────────────


async def _create_agent_row(
    db_session,
    *,
    org_id: UUID,
    lifecycle: str = "unconfigured",
) -> WorkspaceAgentRow:
    """Insert a minimal WorkspaceAgentRow directly for precise lifecycle testing."""
    iid = f"test-lc-{uuid4().hex[:8]}"
    row = WorkspaceAgentRow(
        org_id=org_id,
        instance_id=iid,
        iam_arn=f"arn:aws:iam::123456789012:role/test-{iid}",
        version="0.0.1",
        state="reachable",
        lifecycle=lifecycle,
        claimed_workspace_count=0,
        last_heartbeat_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.flush()
    return row


# ── Tests: ensure_agent_row lifecycle preserve ───────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_ensure_agent_row_preserves_lifecycle_on_reauth(db_session) -> None:
    """(a) fresh insert → unconfigured; (b) UPSERT preserves draining;
    (c) UPSERT from shutdown → unconfigured."""
    from app.core.agent_gateway.service import ensure_agent_row  # noqa: PLC0415

    org_id = uuid4()
    instance_id = f"preserve-{uuid4().hex[:8]}"
    iam_arn = "arn:aws:iam::123456789012:role/yaaos"

    # (a) Fresh insert: column DEFAULT applies → lifecycle='unconfigured'.
    agent_id = await ensure_agent_row(
        org_id=org_id,
        instance_id=instance_id,
        iam_arn=iam_arn,
        version="0.0.1",
        session=db_session,
    )
    await db_session.commit()

    row = (
        await db_session.execute(select(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id))
    ).scalar_one()
    assert row.lifecycle == "unconfigured", f"expected unconfigured, got {row.lifecycle}"

    # (b) Set lifecycle to 'draining' externally then re-exchange — must be preserved.
    row.lifecycle = "draining"
    await db_session.flush()
    await db_session.commit()

    await ensure_agent_row(
        org_id=org_id,
        instance_id=instance_id,
        iam_arn=iam_arn,
        version="0.0.2",  # version changes on re-exchange
        session=db_session,
    )
    await db_session.commit()

    await db_session.refresh(row)
    assert row.lifecycle == "draining", f"expected draining preserved, got {row.lifecycle}"

    # (c) Set lifecycle to 'shutdown' then re-exchange — must reset to 'unconfigured'
    # (treat reconnect of a terminated identity as a fresh agent).
    row.lifecycle = "shutdown"
    await db_session.flush()
    await db_session.commit()

    await ensure_agent_row(
        org_id=org_id,
        instance_id=instance_id,
        iam_arn=iam_arn,
        version="0.0.3",
        session=db_session,
    )
    await db_session.commit()

    await db_session.refresh(row)
    assert row.lifecycle == "unconfigured", f"expected reset to unconfigured, got {row.lifecycle}"


# ── Tests: mark_agent_configured ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_mark_agent_configured_cas_flips_unconfigured_to_active(db_session) -> None:
    """CAS flips lifecycle unconfigured → active."""
    from app.core.agent_gateway.service import mark_agent_configured  # noqa: PLC0415

    org_id = uuid4()
    row = await _create_agent_row(db_session, org_id=org_id, lifecycle="unconfigured")
    await db_session.commit()

    await mark_agent_configured(agent_id=row.id, session=db_session)
    await db_session.commit()

    await db_session.refresh(row)
    assert row.lifecycle == "active"


@pytest.mark.asyncio
@pytest.mark.service
async def test_mark_agent_configured_noop_on_active(db_session) -> None:
    """CAS is a no-op when lifecycle is already active."""
    from app.core.agent_gateway.service import mark_agent_configured  # noqa: PLC0415

    org_id = uuid4()
    row = await _create_agent_row(db_session, org_id=org_id, lifecycle="active")
    await db_session.commit()

    await mark_agent_configured(agent_id=row.id, session=db_session)
    await db_session.commit()

    await db_session.refresh(row)
    assert row.lifecycle == "active"  # unchanged


@pytest.mark.asyncio
@pytest.mark.service
async def test_mark_agent_configured_noop_on_draining(db_session) -> None:
    """CAS is a no-op when lifecycle is draining (must not cancel a drain)."""
    from app.core.agent_gateway.service import mark_agent_configured  # noqa: PLC0415

    org_id = uuid4()
    row = await _create_agent_row(db_session, org_id=org_id, lifecycle="draining")
    await db_session.commit()

    await mark_agent_configured(agent_id=row.id, session=db_session)
    await db_session.commit()

    await db_session.refresh(row)
    assert row.lifecycle == "draining"  # unchanged


@pytest.mark.asyncio
@pytest.mark.service
async def test_mark_agent_configured_publishes_agent_changed_on_cas_win(db_session, redis_or_skip) -> None:
    """CAS win publishes agent_changed SSE; no-op does not publish."""
    from app.core.agent_gateway.service import mark_agent_configured  # noqa: PLC0415
    from app.core.redis import shutdown as redis_shutdown  # noqa: PLC0415
    from app.core.sse import GeneralEventKind, subscribe_general  # noqa: PLC0415

    await redis_shutdown()

    org_id = uuid4()
    row = await _create_agent_row(db_session, org_id=org_id, lifecycle="unconfigured")
    await db_session.commit()

    sub = subscribe_general(org_id)
    received: list[dict] = []

    async def _drain() -> None:
        async for evt in sub:
            received.append(evt)
            if len(received) >= 1:
                return

    drainer = asyncio.create_task(_drain())
    await asyncio.sleep(0)

    await mark_agent_configured(agent_id=row.id, session=db_session)
    await db_session.commit()

    await asyncio.sleep(0.05)
    drainer.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(drainer), timeout=0.1)
    except asyncio.CancelledError, TimeoutError:
        pass

    assert any(e.get("kind") == GeneralEventKind.AGENT_CHANGED for e in received)
    agent_events = [e for e in received if e.get("kind") == GeneralEventKind.AGENT_CHANGED]
    assert agent_events[0].get("agent_id") == str(row.id)


# ── Tests: record_agent_event ConfigUpdate marks configured ───────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_record_agent_event_configupdate_marks_configured(db_session) -> None:
    """completed_success on a ConfigUpdate command CAS-flips lifecycle to active."""
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415
    from app.core.agent_gateway.service import record_agent_event  # noqa: PLC0415
    from app.core.agent_gateway.types import AgentCommandKind, AgentEvent, AgentEventKind  # noqa: PLC0415

    org_id = uuid4()
    # Create agent row with lifecycle='unconfigured'.
    agent_row = await _create_agent_row(db_session, org_id=org_id, lifecycle="unconfigured")
    agent_id = agent_row.id

    # Directly insert a delivered ConfigUpdate command with agent_id already stamped
    # (simulates claim_next assigning the command to this agent).
    # completion_token_hash=None → the token check in record_agent_event is skipped.
    # Must be uuid7 to satisfy the ck_agent_commands_id_uuidv7 CHECK constraint.
    cmd_id = uuid7()
    cmd_row = AgentCommandRow(
        id=cmd_id,
        org_id=org_id,
        workspace_id=None,
        workflow_execution_id=None,
        command_kind=AgentCommandKind.CONFIG_UPDATE,
        payload={},
        status="delivered",
        agent_id=agent_id,
        completion_token_hash=None,
    )
    db_session.add(cmd_row)
    await db_session.flush()
    await db_session.commit()

    # Post a completed_success event — should CAS lifecycle to 'active'.
    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        completion_token=None,
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
    )
    await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.commit()

    await db_session.refresh(agent_row)
    assert agent_row.lifecycle == "active", f"expected active, got {agent_row.lifecycle}"


# ── Tests: handle_heartbeat publishes agent_changed ───────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_record_heartbeat_publishes_agent_changed(db_session, redis_or_skip) -> None:
    """record_heartbeat publishes agent_changed SSE after commit."""
    from app.core.agent_gateway.service import record_heartbeat  # noqa: PLC0415
    from app.core.agent_gateway.types import HeartbeatRequest  # noqa: PLC0415
    from app.core.redis import shutdown as redis_shutdown  # noqa: PLC0415
    from app.core.sse import GeneralEventKind, subscribe_general  # noqa: PLC0415

    await redis_shutdown()

    org_id = uuid4()
    row = await _create_agent_row(db_session, org_id=org_id, lifecycle="active")
    await db_session.commit()

    sub = subscribe_general(org_id)
    received: list[dict] = []

    async def _drain() -> None:
        async for evt in sub:
            received.append(evt)
            if len(received) >= 1:
                return

    drainer = asyncio.create_task(_drain())
    await asyncio.sleep(0)

    hb_request = HeartbeatRequest(reported_at=datetime.now(UTC), workspaces=())
    await record_heartbeat(row.id, hb_request, session=db_session)
    await db_session.commit()

    await asyncio.sleep(0.05)
    drainer.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(drainer), timeout=0.1)
    except asyncio.CancelledError, TimeoutError:
        pass

    assert any(e.get("kind") == GeneralEventKind.AGENT_CHANGED for e in received)
