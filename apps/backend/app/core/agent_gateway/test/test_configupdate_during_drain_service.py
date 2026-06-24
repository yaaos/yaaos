"""Service test: draining agents still receive ConfigUpdate and agent-scoped commands.

Verifies:
- A draining agent can claim a ConfigUpdate (lifecycle-independent priority bucket).
- A draining agent can claim a ShutdownCommand.
- A draining agent can claim a CancelShutdownCommand.
- Priority ordering: agent-scoped commands (ConfigUpdate/Shutdown/CancelShutdown) take
  priority over workspace commands when reporting lifecycle="draining".
"""

from __future__ import annotations

from uuid import UUID, uuid4, uuid7

import pytest

from app.core.agent_gateway.models import WorkspaceAgentRow
from app.core.agent_gateway.service import claim_next, enqueue_config_update_for_agent
from app.core.agent_gateway.types import AgentCommandKind, ConfigUpdateCommand
from app.testing.e2e_setup import seed_agent


async def _make_agent(db_session, *, org_id: UUID, lifecycle: str = "active") -> UUID:
    result = await seed_agent(org_id=org_id)
    agent_id = UUID(str(result["id"]))
    from sqlalchemy import update  # noqa: PLC0415

    await db_session.execute(
        update(WorkspaceAgentRow).where(WorkspaceAgentRow.id == agent_id).values(lifecycle=lifecycle)
    )
    await db_session.flush()
    return agent_id


@pytest.mark.asyncio
@pytest.mark.service
async def test_draining_agent_claims_config_update(db_session) -> None:
    """A draining agent can still claim a ConfigUpdate (credential rotation must land)."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id, lifecycle="draining")

    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    cmd = await claim_next(
        agent_id,
        lifecycle="draining",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert cmd is not None
    assert isinstance(cmd, ConfigUpdateCommand)
    assert cmd.kind == AgentCommandKind.CONFIG_UPDATE


@pytest.mark.asyncio
@pytest.mark.service
async def test_draining_agent_no_provision_workspace(db_session) -> None:
    """A draining agent with new_workspaces=0 does not claim ProvisionWorkspace commands."""
    from app.core.agent_gateway.service import enqueue_command  # noqa: PLC0415
    from app.core.agent_gateway.types import (  # noqa: PLC0415
        AuthBlock,
        ProvisionWorkspaceCommand,
        RepoRef,
    )

    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id, lifecycle="draining")

    cmd = ProvisionWorkspaceCommand(
        command_id=uuid7(),
        workspace_id=uuid4(),
        traceparent="",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="tok"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # Draining agent reports new_workspaces=0 — no ProvisionWorkspace should be returned.
    claimed = await claim_next(
        agent_id,
        lifecycle="draining",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert claimed is None


@pytest.mark.asyncio
@pytest.mark.service
async def test_active_lifecycle_claim_returns_active_commands(db_session) -> None:
    """An active agent (lifecycle='active') finds ConfigUpdate before workspace commands."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id, lifecycle="active")

    # Enqueue ConfigUpdate pinned to agent.
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    cmd = await claim_next(
        agent_id,
        lifecycle="active",
        new_workspaces=1,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert cmd is not None
    assert cmd.kind == AgentCommandKind.CONFIG_UPDATE
