"""ProvisionWorkspace — workspace-create AgentDispatchCommand.

Inherits directly from `AgentDispatchCommand` rather than `WorkspaceOpCommand`
because no workspace row exists yet when this command runs — `dispatch_via_workspace`
(Layer 2) requires a row to look up `org_id` and `owning_agent_id`, so this
command uses `dispatch_provision_workspace` (Layer 1) directly. The workspace
row is created lean on the agent's first workspace event by the sink in
`agent_report.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid7

import structlog
from pydantic import BaseModel, ConfigDict, model_validator

from app.core.agent_gateway import (
    AuthBlock,
    RepoRef,
)
from app.core.workflow import AgentDispatchCommand, CommandContext, Outcome
from app.core.workspace.remote_provider import dispatch_provision_workspace
from app.core.workspace.service import list_workspace_providers

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("core.workspace.commands.provision")


class ProvisionWorkspaceInputs(BaseModel):
    """Typed inputs for the ProvisionWorkspace step.

    Supplied by the workflow's inputs_factory lambda, which reads fields from
    the workflow-input snapshot (TicketSnapshot or equivalent).
    """

    model_config = ConfigDict(frozen=True)
    org_id: UUID
    plugin_id: str
    repo_external_id: str
    # Checkout instruction: exactly one of head_sha (detached pin — every
    # review-flow caller today) or branch_name (named work branch) must be
    # set. Mirrors RepoRef's own exactly-one-of contract in agent_gateway.
    head_sha: str | None = None
    branch_name: str | None = None
    base_sha: str | None = None

    @model_validator(mode="after")
    def _check_checkout_mode(self) -> ProvisionWorkspaceInputs:
        if bool(self.head_sha) == bool(self.branch_name):
            raise ValueError("ProvisionWorkspaceInputs requires exactly one of head_sha or branch_name")
        return self


class ProvisionWorkspaceOutputs(BaseModel):
    """Typed outputs written to step_state when the ProvisionWorkspace
    AgentCommand completes. `workspace_id` is the agent's lifecycle handle."""

    model_config = ConfigDict(frozen=True)
    workspace_id: UUID


class ProvisionWorkspace(AgentDispatchCommand):
    """Provision a workspace for a ticket.

    The engine's AgentDispatch branch always parks the execution in
    `awaiting_agent` and dispatches a `ProvisionWorkspace` AgentCommand over
    the wire; the remote agent performs the actual provisioning.

    `dispatch` mints a `workspace_id` UUID, fetches install credentials,
    enqueues the command, and returns the `command_id`. `execute` is not
    called by the engine and returns failure unconditionally to surface
    any mistaken direct calls.
    """

    kind = "ProvisionWorkspace"
    Inputs = ProvisionWorkspaceInputs
    Outputs = ProvisionWorkspaceOutputs
    restart_safe = True

    async def execute(self, inputs: ProvisionWorkspaceInputs, ctx: CommandContext) -> Outcome:
        del inputs, ctx
        return Outcome.failure(
            reason="ProvisionWorkspace.execute is not the dispatch path for remote provisioning"
        )

    async def dispatch(
        self,
        inputs: ProvisionWorkspaceInputs,
        ctx: CommandContext,
        *,
        session: AsyncSession,
    ) -> UUID:
        """Mint a `workspace_id` UUID (no row yet), fetch install credentials,
        enqueue a `ProvisionWorkspace` AgentCommand durably inside the caller's
        transaction, and return its command_id. The `workspaces` row is created
        lean on the agent's first workspace event (`created` or `ready`) by the
        sink in `agent_report.py`.

        `clone_url` and `installation_token` come from `core/vcs.get_install_credentials`
        so credentials are fetched at dispatch time. Commit identity
        (`git_user_name`/`git_user_email`) is not set here — it defaults to the
        backend-supplied constants on `ProvisionWorkspaceCommand` itself.
        Raises `VcsInstallNotFound` when the org has no active VCS App installation.
        """
        import app.core.vcs as _vcs  # noqa: PLC0415

        providers = list_workspace_providers()
        if not providers:
            raise RuntimeError("no workspace provider registered")

        ws_id = uuid7()

        creds = await _vcs.get_install_credentials(
            inputs.plugin_id,
            inputs.org_id,
            inputs.repo_external_id,
        )

        repo = RepoRef(
            plugin_id=inputs.plugin_id,
            external_id=inputs.repo_external_id,
            clone_url=creds.clone_url,
            head_sha=inputs.head_sha,
            branch_name=inputs.branch_name,
            base_sha=inputs.base_sha,
        )
        auth = AuthBlock(
            kind="github_installation",
            token=creds.installation_token.get_secret_value(),
        )
        result = await dispatch_provision_workspace(
            inputs.org_id,
            ws_id,
            repo=repo,
            auth=auth,
            traceparent="",
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
        )
        log.debug(
            "provision_workspace.dispatched",
            workflow_execution_id=ctx.workflow_execution_id,
            workspace_id=str(ws_id),
            command_id=str(result.command_id),
        )
        return result.command_id
