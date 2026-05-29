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
    pick_agent_for_org,
    queue_depth,
    record_heartbeat,
)
from app.core.workspace.remote_provider import (
    RemoteAgentWorkspaceProvider,
    dispatch_create_workspace,
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
    pod_id = uuid4()
    agent_id = await ensure_agent_row(
        org_id=org_id,
        agent_pod_id=pod_id,
        iam_arn="arn:aws:iam::123456789012:role/yaaos-agent",
        version="0.0.1",
        session=db_session,
    )
    info = await get_agent_info(agent_id, session=db_session)
    assert info is not None
    assert info["org_id"] == org_id
    assert info["agent_pod_id"] == pod_id
    assert info["state"] == "reachable"
    assert info["last_heartbeat_at"] is not None


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
    assert picked.agent_id == seeded["id"]


@pytest.mark.asyncio
async def test_pick_agent_prefers_least_loaded_pod(db_session) -> None:
    """Two reachable pods, both within the heartbeat cutoff. The one
    with the smaller in-process queue depth wins, regardless of which
    has the more recent heartbeat. Multi-pod load balancing."""
    from app.core.agent_gateway import CleanupWorkspaceCommand, enqueue_command  # noqa: PLC0415

    org_id = uuid4()
    busy = await _seed_reachable_agent(db_session, org_id=org_id, heartbeat_age_seconds=5)
    idle = await _seed_reachable_agent(db_session, org_id=org_id, heartbeat_age_seconds=15)

    # Load up `busy` with two queued cleanup commands. `idle` stays at 0.
    for _ in range(2):
        await enqueue_command(
            busy["id"],
            CleanupWorkspaceCommand(
                command_id=uuid4(),
                workspace_id=uuid4(),
                traceparent="00-aabb-1122-01",
                auth=AuthBlock(kind="github_installation", token="x"),
            ),
        )
    assert queue_depth(busy["id"]) == 2
    assert queue_depth(idle["id"]) == 0

    picked = await pick_agent_for_org(org_id, session=db_session)
    assert picked is not None
    assert picked.agent_id == idle["id"], "least-loaded pod should win despite older heartbeat"


@pytest.mark.asyncio
async def test_pick_agent_tie_breaks_on_recent_heartbeat(db_session) -> None:
    """Two idle reachable pods (queue depth 0 each): the more-recent
    heartbeat wins. Stale-but-reachable pods lose to fresh ones at the
    same load."""
    org_id = uuid4()
    stale = await _seed_reachable_agent(db_session, org_id=org_id, heartbeat_age_seconds=60)
    fresh = await _seed_reachable_agent(db_session, org_id=org_id, heartbeat_age_seconds=2)

    assert queue_depth(stale["id"]) == 0
    assert queue_depth(fresh["id"]) == 0

    picked = await pick_agent_for_org(org_id, session=db_session)
    assert picked is not None
    assert picked.agent_id == fresh["id"]


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
    # The command landed on the picked pod's queue (keyed by agent_pod_id).
    assert queue_depth(seeded["agent_pod_id"]) == 1


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
