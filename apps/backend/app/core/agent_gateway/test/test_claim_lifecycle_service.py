"""Service tests for lifecycle-gated claim_next.

Verifies:
- Unconfigured claim returns ConfigUpdateCommand with default max_workspaces.
- Configured claim returns a ProvisionWorkspace command when new_workspaces > 0.
- Configured claim with workspace_ids returns the pending command for a named workspace.
- Unconfigured claim leaves DB rows untouched.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.agent_gateway.models import AgentCommandRow
from app.core.agent_gateway.service import (
    DEFAULT_MAX_WORKSPACES,
    claim_next,
    enqueue_command,
)
from app.core.agent_gateway.types import (
    AgentCommandKind,
    AuthBlock,
    ConfigUpdateCommand,
    ProvisionWorkspaceCommand,
    RepoRef,
    WriteFilesCommand,
    WriteFilesEntry,
)
from app.testing.seed import seed_agent


async def _make_agent(db_session, *, org_id: UUID | None = None) -> UUID:
    result = await seed_agent(org_id=org_id or uuid4(), session=db_session)
    return UUID(str(result["id"]))


def _make_write_cmd(workspace_id: UUID) -> WriteFilesCommand:
    return WriteFilesCommand(
        command_id=uuid4(),
        workspace_id=workspace_id,
        traceparent="00-aabb-1122-01",
        files=(WriteFilesEntry(path="hello.txt", content="hello"),),
    )


def _make_provision_cmd(workspace_id: UUID | None = None) -> ProvisionWorkspaceCommand:
    return ProvisionWorkspaceCommand(
        command_id=uuid4(),
        workspace_id=workspace_id or uuid4(),
        traceparent="00-aabb-1122-01",
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


# ── Unconfigured claim ─────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_unconfigured_claim_returns_config_update(db_session) -> None:
    """An unconfigured claim always returns ConfigUpdateCommand."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    ws_cmd = _make_write_cmd(uuid4())
    await enqueue_command(org_id=org_id, command=ws_cmd, session=db_session)
    await db_session.flush()

    command = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert command is not None
    assert isinstance(command, ConfigUpdateCommand)
    assert command.kind == AgentCommandKind.CONFIG_UPDATE
    assert command.config.max_workspaces == DEFAULT_MAX_WORKSPACES

    # The workspace command must remain pending.
    row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == ws_cmd.command_id))
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "pending"


@pytest.mark.asyncio
@pytest.mark.service
async def test_unconfigured_claim_returns_config_update_when_queue_empty(db_session) -> None:
    """Unconfigured claim returns ConfigUpdateCommand even with no pending rows."""
    agent_id = await _make_agent(db_session)
    command = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert command is not None
    assert isinstance(command, ConfigUpdateCommand)
    assert command.config.max_workspaces > 0


# ── Configured claim ───────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_configured_claim_returns_provision_workspace(db_session) -> None:
    """Configured claim with new_workspaces=1 returns one ProvisionWorkspace."""
    org_id = uuid4()
    agent_id = await _make_agent(db_session, org_id=org_id)
    cmd = _make_provision_cmd()
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    command = await claim_next(
        agent_id,
        lifecycle="configured",
        new_workspaces=1,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert command is not None
    assert command.command_id == cmd.command_id


@pytest.mark.asyncio
@pytest.mark.service
async def test_configured_claim_returns_none_when_empty(db_session) -> None:
    """Configured claim with nothing enqueued returns None."""
    agent_id = await _make_agent(db_session)
    command = await claim_next(
        agent_id,
        lifecycle="configured",
        new_workspaces=4,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert command is None
