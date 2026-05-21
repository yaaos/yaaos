"""RemoteAgentWorkspaceProvider + heartbeat persistence + connection-status."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.agent_gateway import (
    AuthBlock,
    HeartbeatRequest,
    RepoRef,
    _reset_queues_for_tests,
    queue_depth,
    record_heartbeat,
)
from app.core.agent_gateway.models import WorkspaceAgentRow
from app.core.agent_gateway.service import (
    connection_status_for_org,
    ensure_agent_row,
)
from app.core.workspace.remote_provider import (
    RemoteAgentWorkspaceProvider,
    dispatch_create_workspace,
    pick_agent_for_org,
)


@pytest.fixture(autouse=True)
def _isolate_queues() -> None:
    _reset_queues_for_tests()
    yield
    _reset_queues_for_tests()


async def _seed_reachable_agent(
    db_session,
    *,
    org_id=None,
    heartbeat_age_seconds: int = 0,
) -> WorkspaceAgentRow:
    org_id = org_id or uuid4()
    row = WorkspaceAgentRow(
        org_id=org_id,
        agent_pod_id=uuid4(),
        iam_arn="arn:aws:iam::123456789012:role/yaaos-agent",
        version="0.0.1",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=heartbeat_age_seconds),
        state="reachable",
    )
    db_session.add(row)
    await db_session.flush()
    return row


# ── ensure_agent_row ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_agent_row_inserts_on_first_exchange(db_session) -> None:
    org_id = uuid4()
    pod_id = uuid4()
    agent_id = await ensure_agent_row(
        org_id=org_id,
        agent_pod_id=pod_id,
        iam_arn="arn:aws:iam::123456789012:role/yaaos-agent",
        version="0.0.1",
        session=db_session,
    )
    row = await db_session.get(WorkspaceAgentRow, agent_id)
    assert row is not None
    assert row.org_id == org_id
    assert row.agent_pod_id == pod_id
    assert row.state == "reachable"
    assert row.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_ensure_agent_row_updates_existing(db_session) -> None:
    org_id = uuid4()
    pod_id = uuid4()
    first_id = await ensure_agent_row(
        org_id=org_id,
        agent_pod_id=pod_id,
        iam_arn="arn-1",
        version="0.0.1",
        session=db_session,
    )
    second_id = await ensure_agent_row(
        org_id=org_id,
        agent_pod_id=pod_id,
        iam_arn="arn-2",
        version="0.0.2",
        session=db_session,
    )
    assert first_id == second_id  # same row updated
    row = await db_session.get(WorkspaceAgentRow, first_id)
    assert row.iam_arn == "arn-2"
    assert row.version == "0.0.2"


# ── record_heartbeat persistence ──────────────────────────────────────


@pytest.mark.asyncio
async def test_record_heartbeat_bumps_last_heartbeat_at(db_session) -> None:
    row = await _seed_reachable_agent(db_session, heartbeat_age_seconds=60)
    before = row.last_heartbeat_at

    response = await record_heartbeat(
        row.id, HeartbeatRequest(reported_at=datetime.now(UTC), workspaces=()), session=db_session
    )
    await db_session.flush()
    await db_session.refresh(row)
    assert row.last_heartbeat_at > before
    assert row.state == "reachable"
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


# ── pick_agent_for_org + dispatch_create_workspace ────────────────────


@pytest.mark.asyncio
async def test_pick_agent_returns_none_when_no_recent_heartbeat(db_session) -> None:
    org_id = uuid4()
    await _seed_reachable_agent(db_session, org_id=org_id, heartbeat_age_seconds=180)
    assert (await pick_agent_for_org(org_id, session=db_session)) is None


@pytest.mark.asyncio
async def test_pick_agent_returns_recent_pod(db_session) -> None:
    org_id = uuid4()
    seeded = await _seed_reachable_agent(db_session, org_id=org_id, heartbeat_age_seconds=5)
    picked = await pick_agent_for_org(org_id, session=db_session)
    assert picked is not None
    assert picked.id == seeded.id


@pytest.mark.asyncio
async def test_dispatch_create_workspace_enqueues_for_picked_agent(db_session) -> None:
    org_id = uuid4()
    seeded = await _seed_reachable_agent(db_session, org_id=org_id, heartbeat_age_seconds=5)
    workspace_id = uuid4()

    command_id = await dispatch_create_workspace(
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
    assert command_id is not None
    # The command landed on the picked pod's queue.
    assert queue_depth(seeded.agent_pod_id) == 1


@pytest.mark.asyncio
async def test_dispatch_create_workspace_returns_none_when_no_agent(db_session) -> None:
    org_id = uuid4()
    # No agents seeded — caller is expected to handle None as "not reachable".
    command_id = await dispatch_create_workspace(
        org_id,
        uuid4(),
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
    assert command_id is None


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
