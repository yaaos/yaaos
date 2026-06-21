"""Service tests: engine discriminates commands via isinstance, not category enum.

Checks:
1. `ProvisionWorkspace`, `CleanupWorkspace`, `RefreshWorkspaceAuth` are
   `isinstance(cmd, AgentDispatchCommand)` == True and `isinstance(cmd, HITLCommand)` == False.
2. A plain local command is NOT an instance of AgentDispatchCommand.
3. `CleanupWorkspace` and `RefreshWorkspaceAuth` are also `isinstance(cmd, WorkspaceOpCommand)`.
4. `ProvisionWorkspace` is NOT an instance of `WorkspaceOpCommand`
   (it inherits AgentDispatchCommand directly).
"""

from __future__ import annotations

import pytest


@pytest.mark.service
def test_provision_workspace_is_agent_dispatch_command() -> None:
    from app.core.workflow import AgentDispatchCommand, HITLCommand  # noqa: PLC0415
    from app.core.workspace import ProvisionWorkspace  # noqa: PLC0415

    cmd = ProvisionWorkspace()
    assert isinstance(cmd, AgentDispatchCommand), "ProvisionWorkspace must be AgentDispatchCommand"
    assert not isinstance(cmd, HITLCommand), "ProvisionWorkspace must NOT be HITLCommand"


@pytest.mark.service
def test_cleanup_workspace_is_agent_dispatch_command_and_workspace_op() -> None:
    from app.core.workflow import AgentDispatchCommand, HITLCommand  # noqa: PLC0415
    from app.core.workspace import CleanupWorkspace, WorkspaceOpCommand  # noqa: PLC0415

    cmd = CleanupWorkspace()
    assert isinstance(cmd, AgentDispatchCommand), "CleanupWorkspace must be AgentDispatchCommand"
    assert isinstance(cmd, WorkspaceOpCommand), "CleanupWorkspace must be WorkspaceOpCommand"
    assert not isinstance(cmd, HITLCommand), "CleanupWorkspace must NOT be HITLCommand"


@pytest.mark.service
def test_refresh_workspace_auth_is_agent_dispatch_command_and_workspace_op() -> None:
    from app.core.workflow import AgentDispatchCommand, HITLCommand  # noqa: PLC0415
    from app.core.workspace import RefreshWorkspaceAuth, WorkspaceOpCommand  # noqa: PLC0415

    cmd = RefreshWorkspaceAuth()
    assert isinstance(cmd, AgentDispatchCommand), "RefreshWorkspaceAuth must be AgentDispatchCommand"
    assert isinstance(cmd, WorkspaceOpCommand), "RefreshWorkspaceAuth must be WorkspaceOpCommand"
    assert not isinstance(cmd, HITLCommand), "RefreshWorkspaceAuth must NOT be HITLCommand"


@pytest.mark.service
def test_provision_workspace_not_workspace_op_command() -> None:
    """ProvisionWorkspace inherits AgentDispatchCommand directly — not WorkspaceOpCommand."""
    from app.core.workspace import ProvisionWorkspace, WorkspaceOpCommand  # noqa: PLC0415

    cmd = ProvisionWorkspace()
    assert not isinstance(cmd, WorkspaceOpCommand), (
        "ProvisionWorkspace must NOT be WorkspaceOpCommand (inherits AgentDispatchCommand directly)"
    )


@pytest.mark.service
def test_plain_local_command_not_agent_dispatch_command() -> None:
    """A plain local command (no inheritance) is not AgentDispatchCommand."""
    from pydantic import BaseModel  # noqa: PLC0415

    from app.core.workflow import AgentDispatchCommand, Empty, Outcome  # noqa: PLC0415

    class _Local:
        kind = "TestLocal"
        Inputs = Empty
        Outputs = Empty

        async def execute(self, inputs: BaseModel, ctx, *, session=None) -> Outcome:
            return Outcome.success()

    cmd = _Local()
    assert not isinstance(cmd, AgentDispatchCommand), "plain local command must not be AgentDispatchCommand"


@pytest.mark.service
def test_coding_agent_command_is_agent_dispatch_command() -> None:
    """A CodingAgentCommand subclass (stub) is AgentDispatchCommand."""
    from pydantic import BaseModel  # noqa: PLC0415

    from app.core.coding_agent import CodingAgentCommand  # noqa: PLC0415
    from app.core.workflow import AgentDispatchCommand  # noqa: PLC0415

    class _StubInputs(BaseModel):
        workspace_id: str = "ws"

    class _StubCodeReview(CodingAgentCommand):
        kind = "StubCodeReview"
        plugin_id = "claude_code"
        Inputs = _StubInputs

        async def build_invocation(self, inputs, ctx, *, session):  # type: ignore[override]
            raise NotImplementedError

    cmd = _StubCodeReview()
    assert isinstance(cmd, AgentDispatchCommand), "CodingAgentCommand subclass must be AgentDispatchCommand"
    assert isinstance(cmd, CodingAgentCommand), "CodingAgentCommand subclass must be CodingAgentCommand"
