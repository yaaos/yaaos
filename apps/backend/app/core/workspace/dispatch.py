"""Single-flight claim for workspace AgentCommands.

The workspace state machine has one in-flight AgentCommand at a time.
`try_claim()` atomically assigns `current_command_id` to a workspace ONLY if no
other command holds it; it's the engine's gate into the wire protocol.
`release_claim()` clears the claim after the terminal event has been observed
(failure-report-precedes-disposal).

`dispatch_via_workspace` is the Layer 2 dispatch helper — it looks up the
workspace row, enqueues the command, pins to the owning agent, and optionally
claims the workspace. All workspace dispatch helpers except `ProvisionWorkspace`
(which has no row yet) route through this function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid7

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import enqueue_command, pin_command_to_agent
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import WorkspaceClaimFailed, WorkspaceNotFoundError

if TYPE_CHECKING:
    from app.core.agent_gateway import AgentCommand, DispatchContext

log = structlog.get_logger("core.workspace.dispatch")


async def try_claim(
    workspace_id: UUID,
    *,
    command_id: UUID,
    workflow_execution_id: UUID,
    agent_id: UUID | None = None,
    session: AsyncSession,
) -> bool:
    """Atomically claim `workspace_id` for `command_id`.

    Returns True iff the row had `current_command_id IS NULL` AND was
    `status='active'`. False otherwise — caller MUST treat as "busy" and
    not dispatch. The conditional UPDATE is the single-flight gate; a
    second concurrent caller racing on the same row sees rowcount=0 and
    backs off.

    `agent_id` (the owning `WorkspaceAgentRow.id`) is written as `owning_agent_id`
    onto the row in the same UPDATE when supplied — post-provision commands pass it
    to hard-tie the workspace to the pod that ran `ProvisionWorkspace`.
    Lean-created rows already carry `owning_agent_id` from the first workspace
    event; legacy in-process rows omit it, leaving `WorkspaceRow.owning_agent_id` NULL.

    `workflow_execution_id` is accepted for API compatibility but no longer written
    to the workspace row — correlation lives exclusively on
    `agent_commands.workflow_execution_id`.

    Caller commits; the outbox row enqueueing the AgentCommand should go
    in the same transaction so claim + dispatch land atomically.
    """
    values: dict[str, UUID] = {
        "current_command_id": command_id,
    }
    if agent_id is not None:
        values["owning_agent_id"] = agent_id
    result = await session.execute(
        update(WorkspaceRow)
        .where(
            WorkspaceRow.id == workspace_id,
            WorkspaceRow.current_command_id.is_(None),
            WorkspaceRow.status == "active",
        )
        .values(**values)
    )
    claimed = bool(result.rowcount)
    if not claimed:
        log.debug(
            "workspace.claim.busy_or_inactive",
            workspace_id=str(workspace_id),
            workflow_execution_id=str(workflow_execution_id),
        )
    return claimed


async def release_claim(
    workspace_id: UUID,
    *,
    command_id: UUID,
    session: AsyncSession,
) -> bool:
    """Release the claim if-and-only-if `command_id` still owns it. Returns
    True if the claim was released. Idempotent — second release for the
    same command_id is a no-op."""
    result = await session.execute(
        update(WorkspaceRow)
        .where(
            WorkspaceRow.id == workspace_id,
            WorkspaceRow.current_command_id == command_id,
        )
        .values(current_command_id=None)
    )
    return bool(result.rowcount)


async def dispatch_via_workspace(
    *,
    command: AgentCommand,
    workspace_id: UUID,
    ctx: DispatchContext,
    session: AsyncSession,
    claim_workspace: bool = False,
) -> UUID:
    """Enqueue `command` durably inside the caller's transaction (Layer 2).

    Loads the workspace row to get `org_id` + `owning_agent_id`, calls
    `enqueue_command`, pins the command to the owning agent when one is set,
    and — when `claim_workspace=True` — atomically claims the workspace via
    `try_claim`.

    Raises:
        `WorkspaceNotFoundError` — workspace row absent.
        `WorkspaceClaimFailed` — `claim_workspace=True` but workspace busy
            (current_command_id IS NOT NULL) or inactive (status != 'active').
    """
    ws_row = (
        await session.execute(select(WorkspaceRow).where(WorkspaceRow.id == workspace_id))
    ).scalar_one_or_none()
    if ws_row is None:
        raise WorkspaceNotFoundError(f"workspace {workspace_id} not found")

    await enqueue_command(
        org_id=ws_row.org_id,
        command=command,
        session=session,
        workflow_execution_id=ctx.run_id,
    )
    if ws_row.owning_agent_id is not None:
        await pin_command_to_agent(command.command_id, ws_row.owning_agent_id, session=session)

    if claim_workspace:
        claimed = await try_claim(
            workspace_id,
            command_id=command.command_id,
            workflow_execution_id=ctx.run_id,
            session=session,
        )
        if not claimed:
            raise WorkspaceClaimFailed(f"workspace {workspace_id} is busy or inactive")

    return command.command_id


# ---------------------------------------------------------------------------
# Plain dispatch functions — extracted from the lifecycle commands' enqueue
# bodies (`commands/provision.py`, `cleanup.py`, `refresh_auth.py`, since
# deleted) so `domain/pipelines`' run engine can dispatch the same
# operations. `ctx: DispatchContext` carries only the generic correlation
# fields (`run_id`, `ticket_id`, `stage_execution_id`, `attempt`,
# `traceparent`) the dispatch layer needs.
# ---------------------------------------------------------------------------


class ProvisionWorkspaceSpec(BaseModel):
    """Everything `dispatch_provision` needs to enqueue a `ProvisionWorkspace`
    AgentCommand. `workspace_id` is caller-minted — no workspace row exists
    yet at provision time, so the caller (which needs the id immediately, to
    reference before the terminal event arrives) mints it rather than this
    function minting one internally."""

    model_config = ConfigDict(frozen=True)
    workspace_id: UUID
    org_id: UUID
    plugin_id: str
    repo_external_id: str
    # Checkout instruction: exactly one of head_sha (detached pin) or
    # branch_name (named work branch) must be set — mirrors RepoRef's own
    # exactly-one-of contract.
    head_sha: str | None = None
    branch_name: str | None = None
    base_sha: str | None = None


async def dispatch_provision(
    spec: ProvisionWorkspaceSpec,
    ctx: DispatchContext,
    *,
    session: AsyncSession,
) -> UUID:
    """Fetch install credentials and enqueue a `ProvisionWorkspace`
    AgentCommand for `spec.workspace_id`. The `workspaces` row itself is
    still created lean on the agent's first workspace event — this
    function only enqueues the command.

    Raises `VcsInstallNotFound` when the org has no active VCS App
    installation; `RuntimeError` when no workspace provider is registered.
    """
    import app.core.vcs as _vcs  # noqa: PLC0415
    from app.core.agent_gateway import AuthBlock, RepoRef  # noqa: PLC0415
    from app.core.workspace.remote_provider import dispatch_provision_workspace  # noqa: PLC0415
    from app.core.workspace.service import list_workspace_providers  # noqa: PLC0415

    if not list_workspace_providers():
        raise RuntimeError("no workspace provider registered")

    creds = await _vcs.get_install_credentials(spec.plugin_id, spec.org_id, spec.repo_external_id)
    repo = RepoRef(
        plugin_id=spec.plugin_id,
        external_id=spec.repo_external_id,
        clone_url=creds.clone_url,
        head_sha=spec.head_sha,
        branch_name=spec.branch_name,
        base_sha=spec.base_sha,
    )
    auth = AuthBlock(kind="github_installation", token=creds.installation_token.get_secret_value())
    result = await dispatch_provision_workspace(
        spec.org_id,
        spec.workspace_id,
        repo=repo,
        auth=auth,
        traceparent=ctx.traceparent or "",
        session=session,
        workflow_execution_id=ctx.run_id,
    )
    return result.command_id


async def dispatch_cleanup(workspace_id: UUID, ctx: DispatchContext, *, session: AsyncSession) -> UUID:
    """Enqueue a `CleanupWorkspace` AgentCommand for `workspace_id`. Never
    claims (cleanup runs regardless of who currently holds the workspace)."""
    from app.core.agent_gateway import CleanupWorkspaceCommand  # noqa: PLC0415

    cmd = CleanupWorkspaceCommand(
        command_id=uuid7(), workspace_id=workspace_id, traceparent=ctx.traceparent or ""
    )
    return await dispatch_via_workspace(
        command=cmd, workspace_id=workspace_id, ctx=ctx, session=session, claim_workspace=False
    )


async def dispatch_auth_refresh(workspace_id: UUID, ctx: DispatchContext, *, session: AsyncSession) -> UUID:
    """Enqueue the auth-refresh recovery AgentCommand for `workspace_id` —
    dispatches a placeholder `CleanupWorkspaceCommand` wire payload; a real
    `RefreshWorkspaceAuth` AgentCommand type doesn't exist yet."""
    from app.core.agent_gateway import CleanupWorkspaceCommand  # noqa: PLC0415

    cmd = CleanupWorkspaceCommand(
        command_id=uuid7(), workspace_id=workspace_id, traceparent=ctx.traceparent or ""
    )
    return await dispatch_via_workspace(
        command=cmd, workspace_id=workspace_id, ctx=ctx, session=session, claim_workspace=False
    )


async def dispatch_push(workspace_id: UUID, ctx: DispatchContext, *, session: AsyncSession) -> UUID:
    """Enqueue a bare `PushBranch` re-push AgentCommand for `workspace_id` —
    push-failure recovery only: a re-push of the workspace's current HEAD
    after a `refresh-auth` credential rotation, so claude is never re-run
    just to retry a push."""
    from app.core.agent_gateway import PushBranchCommand  # noqa: PLC0415

    cmd = PushBranchCommand(command_id=uuid7(), workspace_id=workspace_id, traceparent=ctx.traceparent or "")
    return await dispatch_via_workspace(
        command=cmd, workspace_id=workspace_id, ctx=ctx, session=session, claim_workspace=False
    )
