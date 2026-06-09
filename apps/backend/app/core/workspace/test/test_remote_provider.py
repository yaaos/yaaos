"""RemoteAgentWorkspaceProvider + heartbeat persistence + connection-status."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.agent_gateway import (
    AuthBlock,
    HeartbeatRequest,
    RepoRef,
    connection_status_for_org,
    ensure_agent_row,
    get_agent_info,
    record_heartbeat,
)
from app.core.workspace.remote_provider import (
    RemoteAgentWorkspaceProvider,
    dispatch_provision_workspace,
)
from app.testing.seed import seed_agent


async def _seed_reachable_agent(
    db_session,
    *,
    org_id=None,
    heartbeat_age_seconds: int = 0,
) -> dict:
    org_id = org_id or uuid4()
    return await seed_agent(
        org_id=org_id,
        session=db_session,
        heartbeat_age_seconds=heartbeat_age_seconds,
    )


# ── ensure_agent_row ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_agent_row_inserts_on_first_exchange(db_session) -> None:
    org_id = uuid4()
    instance_id = f"test-task-{uuid4().hex[:8]}"
    agent_id = await ensure_agent_row(
        org_id=org_id,
        instance_id=instance_id,
        iam_arn="arn:aws:iam::123456789012:role/yaaos-agent",
        version="0.0.1",
        session=db_session,
    )
    info = await get_agent_info(agent_id, session=db_session)
    assert info is not None
    assert info["org_id"] == org_id
    assert info["instance_id"] == instance_id
    assert info["state"] == "reachable"
    assert info["last_heartbeat_at"] is not None


@pytest.mark.asyncio
async def test_ensure_agent_row_updates_existing(db_session) -> None:
    org_id = uuid4()
    instance_id = f"test-task-{uuid4().hex[:8]}"
    first_id = await ensure_agent_row(
        org_id=org_id,
        instance_id=instance_id,
        iam_arn="arn-1",
        version="0.0.1",
        session=db_session,
    )
    second_id = await ensure_agent_row(
        org_id=org_id,
        instance_id=instance_id,
        iam_arn="arn-2",
        version="0.0.2",
        session=db_session,
    )
    assert first_id == second_id  # same row updated
    info = await get_agent_info(first_id, session=db_session)
    assert info is not None
    assert info["iam_arn"] == "arn-2"
    assert info["version"] == "0.0.2"


# ── record_heartbeat persistence ──────────────────────────────────────


@pytest.mark.asyncio
async def test_record_heartbeat_bumps_last_heartbeat_at(db_session) -> None:
    seeded = await _seed_reachable_agent(db_session, heartbeat_age_seconds=60)
    agent_id = seeded["id"]
    info_before = await get_agent_info(agent_id, session=db_session)
    assert info_before is not None
    before = info_before["last_heartbeat_at"]

    response = await record_heartbeat(
        agent_id, HeartbeatRequest(reported_at=datetime.now(UTC), workspaces=()), session=db_session
    )
    await db_session.flush()
    info_after = await get_agent_info(agent_id, session=db_session)
    assert info_after is not None
    assert info_after["last_heartbeat_at"] > before
    assert info_after["state"] == "reachable"
    assert response.forgotten_workspaces == ()


# ── connection_status_for_org ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_connection_status_not_configured(db_session) -> None:
    org_id = uuid4()
    status = await connection_status_for_org(org_id, session=db_session)
    assert status["state"] == "not_configured"
    assert status["pod_count"] == 0
    assert status["latest_heartbeat_at"] is None


@pytest.mark.asyncio
async def test_connection_status_connected(db_session) -> None:
    org_id = uuid4()
    await _seed_reachable_agent(db_session, org_id=org_id, heartbeat_age_seconds=5)
    status = await connection_status_for_org(org_id, session=db_session)
    assert status["state"] == "connected"
    assert status["pod_count"] == 1
    assert status["latest_heartbeat_at"] is not None


@pytest.mark.asyncio
async def test_connection_status_lost_when_heartbeat_stale(db_session) -> None:
    org_id = uuid4()
    await _seed_reachable_agent(db_session, org_id=org_id, heartbeat_age_seconds=180)
    status = await connection_status_for_org(org_id, session=db_session)
    assert status["state"] == "lost"
    assert status["pod_count"] == 1


# ── dispatch_provision_workspace ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_provision_workspace_enqueues_pending_row(db_session) -> None:
    """dispatch_provision_workspace enqueues a pending row claimable by any agent."""
    from app.core.agent_gateway import claim_next  # noqa: PLC0415

    org_id = uuid4()
    seeded = await _seed_reachable_agent(db_session, org_id=org_id, heartbeat_age_seconds=5)
    workspace_id = uuid4()

    result = await dispatch_provision_workspace(
        org_id,
        workspace_id,
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        auth=AuthBlock(kind="github_installation", token="tok"),
        traceparent="00-aabb-1122-01",
        session=db_session,
    )
    # Verify the command is claimable (it was inserted as pending).
    command = await claim_next(
        seeded["id"],
        lifecycle="configured",
        new_workspaces=1,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert command is not None
    assert command.command_id == result.command_id


# ── Provider health_check ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_provider_health_check_healthy_with_reachable_pods(db_session) -> None:
    await _seed_reachable_agent(db_session, heartbeat_age_seconds=5)
    await db_session.commit()
    provider = RemoteAgentWorkspaceProvider()
    status = await provider.health_check()
    assert status.healthy is True


@pytest.mark.asyncio
async def test_provider_health_check_unhealthy_when_no_pods(db_session) -> None:
    # Deliberately don't seed any pods.
    await db_session.commit()
    provider = RemoteAgentWorkspaceProvider()
    status = await provider.health_check()
    assert status.healthy is False
