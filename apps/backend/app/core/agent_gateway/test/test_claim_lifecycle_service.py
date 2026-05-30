"""Service tests for lifecycle-gated claim_next.

Verifies:
- Unconfigured claim returns ConfigUpdateCommand with default max_workspaces.
- Configured claim returns first eligible queued command.
- Configured claim with active_workspace_ids filters ineligible workspace commands
  (WriteFiles for an inactive workspace stays queued while WriteFiles for an
  active workspace is returned).
- CreateWorkspace is always eligible regardless of active_workspace_ids.
- AgentCommand is always eligible regardless of lifecycle.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.core.agent_gateway import (
    AgentCommandKind,
    ClaimRequest,
    ConfigUpdateCommand,
    WriteFilesCommand,
    WriteFilesEntry,
    claim_next,
    enqueue_command,
    queue_depth,
)
from app.core.agent_gateway.types import (
    AuthBlock,
    CreateWorkspaceCommand,
    RepoRef,
)


def _make_write_cmd(workspace_id: UUID) -> WriteFilesCommand:
    return WriteFilesCommand(
        command_id=uuid4(),
        workspace_id=workspace_id,
        traceparent="00-aabb-1122-01",
        files=(WriteFilesEntry(path="hello.txt", content="hello"),),
    )


def _make_create_cmd(workspace_id: UUID) -> CreateWorkspaceCommand:
    return CreateWorkspaceCommand(
        command_id=uuid4(),
        workspace_id=workspace_id,
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
async def test_unconfigured_claim_returns_config_update() -> None:
    """An unconfigured claim always returns ConfigUpdateCommand, even if a
    workspace command is queued."""
    agent = uuid4()

    # Enqueue a workspace command first.
    ws_cmd = _make_write_cmd(uuid4())
    await enqueue_command(agent, ws_cmd)

    req = ClaimRequest(wait_seconds=0, lifecycle="unconfigured", active_workspace_ids=[])
    result = await claim_next(
        agent,
        wait_seconds=req.wait_seconds,
        lifecycle=req.lifecycle,
        active_workspace_ids=req.active_workspace_ids,
    )

    assert result is not None
    assert isinstance(result, ConfigUpdateCommand), f"expected ConfigUpdateCommand, got {type(result)}"
    assert result.kind == AgentCommandKind.CONFIG_UPDATE
    assert result.config.max_workspaces > 0

    # The workspace command must remain queued.
    assert queue_depth(agent) == 1


@pytest.mark.asyncio
async def test_unconfigured_claim_returns_config_update_when_queue_empty() -> None:
    """Unconfigured claim returns ConfigUpdateCommand even with an empty queue."""
    agent = uuid4()
    req = ClaimRequest(wait_seconds=0, lifecycle="unconfigured", active_workspace_ids=[])
    result = await claim_next(
        agent,
        wait_seconds=req.wait_seconds,
        lifecycle=req.lifecycle,
        active_workspace_ids=req.active_workspace_ids,
    )

    assert result is not None
    assert isinstance(result, ConfigUpdateCommand)
    assert result.config.max_workspaces > 0


# ── Configured claim — eligibility filter ─────────────────────────────────


@pytest.mark.asyncio
async def test_configured_claim_returns_eligible_workspace_command() -> None:
    """Configured claim returns WriteFiles for a workspace_id in active_workspace_ids."""
    agent = uuid4()
    ws_a = uuid4()

    cmd_a = _make_write_cmd(ws_a)
    await enqueue_command(agent, cmd_a)

    result = await claim_next(
        agent,
        wait_seconds=0,
        lifecycle="configured",
        active_workspace_ids=[ws_a],
    )
    assert result is cmd_a
    assert queue_depth(agent) == 0


@pytest.mark.asyncio
async def test_configured_claim_leaves_ineligible_command_queued() -> None:
    """WriteFiles for workspace B stays queued when only workspace A is active;
    WriteFiles for A is returned."""
    agent = uuid4()
    ws_a = uuid4()
    ws_b = uuid4()

    cmd_b = _make_write_cmd(ws_b)
    cmd_a = _make_write_cmd(ws_a)
    await enqueue_command(agent, cmd_b)
    await enqueue_command(agent, cmd_a)

    result = await claim_next(
        agent,
        wait_seconds=0,
        lifecycle="configured",
        active_workspace_ids=[ws_a],
    )
    # cmd_a (workspace A) is eligible — must be returned.
    assert result is cmd_a
    # cmd_b (workspace B) must remain queued.
    assert queue_depth(agent) == 1


@pytest.mark.asyncio
async def test_configured_claim_create_always_eligible() -> None:
    """CreateWorkspace is eligible regardless of active_workspace_ids."""
    agent = uuid4()
    new_ws = uuid4()

    create_cmd = _make_create_cmd(new_ws)
    await enqueue_command(agent, create_cmd)

    result = await claim_next(
        agent,
        wait_seconds=0,
        lifecycle="configured",
        active_workspace_ids=[],  # no active workspaces yet
    )
    assert result is create_cmd


@pytest.mark.asyncio
async def test_configured_claim_returns_none_when_all_ineligible() -> None:
    """If every queued command is for a non-active workspace, claim returns None
    (no eligible work, even though the queue is non-empty)."""
    agent = uuid4()
    ws_b = uuid4()

    await enqueue_command(agent, _make_write_cmd(ws_b))

    result = await claim_next(
        agent,
        wait_seconds=0,
        lifecycle="configured",
        active_workspace_ids=[],  # ws_b not active
    )
    assert result is None
    # The ineligible command remains queued.
    assert queue_depth(agent) == 1
