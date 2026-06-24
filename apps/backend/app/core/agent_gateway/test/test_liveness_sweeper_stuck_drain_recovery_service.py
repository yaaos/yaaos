"""Service tests: stuck-drain recovery inside the liveness sweeper.

When a `draining` agent goes offline (heartbeat age > 5 min), the liveness
sweeper calls `mark_agent_shutdown_complete` to complete the shutdown
atomically: lifecycle → 'shutdown', bearers revoked, audit row written.

Verifies:
- Draining agent that goes offline gets lifecycle='shutdown' written by sweeper.
- Active agent that goes offline does NOT flip lifecycle (unchanged 'active').
- Unconfigured agent that goes offline does NOT flip lifecycle.
- Draining agent that is still reachable does NOT trigger early completion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.agent_gateway.models import WorkspaceAgentRow

_OFFLINE_AGE_SECONDS = 6 * 60  # > 5 min threshold


async def _make_agent(db_session, *, lifecycle: str, heartbeat_age_seconds: float) -> WorkspaceAgentRow:
    now = datetime.now(UTC)
    last_heartbeat = now - timedelta(seconds=heartbeat_age_seconds)
    row = WorkspaceAgentRow(
        org_id=uuid4(),
        instance_id=f"test-sw-{uuid4().hex[:8]}",
        iam_arn=f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}",
        version="0.0.1",
        state="reachable",
        lifecycle=lifecycle,
        claimed_workspace_count=0,
        last_heartbeat_at=last_heartbeat,
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.mark.asyncio
@pytest.mark.service
async def test_sweeper_completes_stuck_drain(db_session) -> None:
    """Draining agent past offline threshold → lifecycle flips to 'shutdown'."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415

    agent = await _make_agent(db_session, lifecycle="draining", heartbeat_age_seconds=_OFFLINE_AGE_SECONDS)
    await db_session.commit()

    now = datetime.now(UTC)
    await compute_agent_liveness_transitions(now, session=db_session)
    await db_session.commit()

    await db_session.refresh(agent)
    assert agent.lifecycle == "shutdown", (
        f"expected lifecycle='shutdown' after stuck-drain recovery, got '{agent.lifecycle}'"
    )


@pytest.mark.asyncio
@pytest.mark.service
async def test_sweeper_completes_stuck_drain_writes_audit(db_session) -> None:
    """Draining agent offline → workspace_agent.shutdown_complete audit written by sweeper."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415
    from app.core.audit_log import list_for_entity  # noqa: PLC0415

    agent = await _make_agent(db_session, lifecycle="draining", heartbeat_age_seconds=_OFFLINE_AGE_SECONDS)
    org_id = agent.org_id
    await db_session.commit()

    now = datetime.now(UTC)
    await compute_agent_liveness_transitions(now, session=db_session)
    await db_session.commit()

    entries = await list_for_entity("workspace_agent", agent.id, org_id=org_id)
    kinds = [e.kind for e in entries]
    assert "workspace_agent.shutdown_complete" in kinds


@pytest.mark.asyncio
@pytest.mark.service
async def test_sweeper_active_offline_does_not_flip_lifecycle(db_session) -> None:
    """Active agent going offline: lifecycle stays 'active' (only state changes)."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415

    agent = await _make_agent(db_session, lifecycle="active", heartbeat_age_seconds=_OFFLINE_AGE_SECONDS)
    await db_session.commit()

    now = datetime.now(UTC)
    await compute_agent_liveness_transitions(now, session=db_session)
    await db_session.commit()

    await db_session.refresh(agent)
    assert agent.lifecycle == "active"
    assert agent.state == "offline"


@pytest.mark.asyncio
@pytest.mark.service
async def test_sweeper_unconfigured_offline_does_not_flip_lifecycle(db_session) -> None:
    """Unconfigured agent going offline: lifecycle stays 'unconfigured'."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415

    agent = await _make_agent(
        db_session, lifecycle="unconfigured", heartbeat_age_seconds=_OFFLINE_AGE_SECONDS
    )
    await db_session.commit()

    now = datetime.now(UTC)
    await compute_agent_liveness_transitions(now, session=db_session)
    await db_session.commit()

    await db_session.refresh(agent)
    assert agent.lifecycle == "unconfigured"
    assert agent.state == "offline"


@pytest.mark.asyncio
@pytest.mark.service
async def test_sweeper_draining_reachable_does_not_complete(db_session) -> None:
    """Draining agent that is still heartbeating: lifecycle unchanged (drain is in progress)."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415

    agent = await _make_agent(
        db_session,
        lifecycle="draining",
        heartbeat_age_seconds=5,  # still reachable
    )
    await db_session.commit()

    now = datetime.now(UTC)
    await compute_agent_liveness_transitions(now, session=db_session)
    await db_session.commit()

    await db_session.refresh(agent)
    assert agent.lifecycle == "draining"
    assert agent.state == "reachable"


@pytest.mark.asyncio
@pytest.mark.service
async def test_sweeper_already_shutdown_stays_shutdown(db_session) -> None:
    """Already-shutdown agent offline: CAS is a no-op, lifecycle stays 'shutdown'."""
    from app.core.agent_gateway.service import compute_agent_liveness_transitions  # noqa: PLC0415

    agent = await _make_agent(db_session, lifecycle="shutdown", heartbeat_age_seconds=_OFFLINE_AGE_SECONDS)
    await db_session.commit()

    now = datetime.now(UTC)
    await compute_agent_liveness_transitions(now, session=db_session)
    await db_session.commit()

    await db_session.refresh(agent)
    assert agent.lifecycle == "shutdown"
