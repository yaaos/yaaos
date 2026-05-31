"""Lifecycle commands — `CleanupWorkspace`, `ProvisionWorkspace`, `RefreshWorkspaceAuth`.

Covers:
- CleanupWorkspace: missing workspace_id (idempotent success), invalid
  uuid (failure), happy-path close that flips the WorkspaceRow status
  to `expired`, unknown id (idempotent).
- ProvisionWorkspace: no provider registered (failure), ticket not found
  (failure), happy-path creates a WorkspaceRow via the registered stub
  provider with spec built from the ticket context.
- RefreshWorkspaceAuth: execute() returns success (engine dispatches the
  AgentCommand on the remote path; inline returns success).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.core.plugin_kit import PluginMeta
from app.core.workflow import CommandContext
from app.core.workspace import (
    WorkspaceTicketContext,
    register_workflow_context_provider,
    register_workspace_provider,
)
from app.core.workspace.commands import CleanupWorkspace, ProvisionWorkspace, RefreshWorkspaceAuth
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import WorkspaceStatus


def _ctx() -> CommandContext:
    return CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(uuid4()),
        step_id="cleanup",
        attempt=0,
    )


async def test_cleanup_with_no_workspace_id_succeeds_silently() -> None:
    """Provision failed mid-workflow → cleanup runs with no workspace_id.
    Treat as success so the workflow drains rather than re-failing."""
    outcome = await CleanupWorkspace().execute({}, _ctx())
    assert outcome.label == "success"


async def test_cleanup_with_invalid_workspace_id_fails() -> None:
    outcome = await CleanupWorkspace().execute({"workspace_id": "not-a-uuid"}, _ctx())
    assert outcome.label == "failure"
    assert "invalid workspace_id" in (outcome.failure_reason or "")


async def test_cleanup_flips_row_to_expired(db_session) -> None:  # type: ignore[no-untyped-def]
    org_id = uuid4()
    ws_id = uuid4()
    db_session.add(
        WorkspaceRow(
            id=ws_id,
            org_id=org_id,
            provider_id="remote_agent",
            spec={"sha": "deadbeef"},
            status=WorkspaceStatus.ACTIVE.value,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
    )
    await db_session.commit()

    outcome = await CleanupWorkspace().execute({"workspace_id": str(ws_id)}, _ctx())
    assert outcome.label == "success"

    row = (await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == ws_id))).scalar_one()
    assert row.status == WorkspaceStatus.EXPIRED.value


async def test_cleanup_unknown_workspace_succeeds_silently(db_session) -> None:  # type: ignore[no-untyped-def]
    """Phantom workspace_id (never existed, or already destroyed): close_workspace
    is a no-op on rows that aren't in active/creating. CleanupWorkspace returns
    success — idempotent."""
    _ = db_session  # ensure schema exists for the close_workspace path
    outcome = await CleanupWorkspace().execute({"workspace_id": str(uuid4())}, _ctx())
    assert outcome.label == "success"


# ── ProvisionWorkspace ─────────────────────────────────────────────────


class _StubContextProvider:
    """Stub WorkflowContextProvider that returns a fixed context (or None)."""

    def __init__(self, context: WorkspaceTicketContext | None) -> None:
        self._context = context

    async def get_workspace_ticket_context(self, ticket_id):  # type: ignore[no-untyped-def]
        del ticket_id
        return self._context


class _StubWorkspaceProvider:
    """Tiny WorkspaceProvider stub. Doesn't clone anything — just returns
    a fake plugin_state so create_workspace() succeeds end-to-end."""

    meta = PluginMeta(id="stub_ws", type="workspace", display_name="stub-workspace")

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {"working_dir": "/tmp/stub", "sha": spec.sha}

    async def destroy(self, plugin_state):  # type: ignore[no-untyped-def]
        return None

    async def health_check(self, plugin_state):  # type: ignore[no-untyped-def]
        del plugin_state
        return None

    async def run_coding_agent_cli(self, plugin_state, argv, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, plugin_state, path):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, plugin_state, path, content):  # type: ignore[no-untyped-def]
        return None


@pytest.fixture
def _stub_workspace_plugin(workspace_providers_isolation):
    register_workspace_provider(_StubWorkspaceProvider())


async def test_provision_fails_when_no_provider_registered(
    workspace_providers_isolation, workflow_context_provider_isolation
) -> None:
    """No provider registered → ProvisionWorkspace returns failure so the
    workflow fails loudly rather than silently skipping provision."""
    register_workflow_context_provider(
        _StubContextProvider(
            context=WorkspaceTicketContext(
                org_id=uuid4(),
                plugin_id="github",
                repo_external_id="me/repo",
                payload={"head_sha": "abc"},
            )
        )
    )
    outcome = await ProvisionWorkspace().execute({}, _ctx())
    assert outcome.label == "failure"
    assert "no workspace provider" in (outcome.failure_reason or "")


async def test_provision_fails_when_ticket_not_found(
    workspace_providers_isolation, workflow_context_provider_isolation
) -> None:
    register_workspace_provider(_StubWorkspaceProvider())
    register_workflow_context_provider(_StubContextProvider(context=None))
    outcome = await ProvisionWorkspace().execute({}, _ctx())
    assert outcome.label == "failure"
    assert "not found" in (outcome.failure_reason or "")


async def test_provision_creates_workspace_with_spec(
    db_session, _stub_workspace_plugin, workflow_context_provider_isolation
) -> None:  # type: ignore[no-untyped-def]
    ticket_id = uuid4()
    org_id = uuid4()
    register_workflow_context_provider(
        _StubContextProvider(
            context=WorkspaceTicketContext(
                org_id=org_id,
                plugin_id="github",
                repo_external_id="me/repo",
                payload={"head_sha": "deadbeefcafef00d", "base_sha": "babecafe"},
            )
        )
    )
    ctx = CommandContext(
        workflow_execution_id=str(uuid4()),
        ticket_id=str(ticket_id),
        step_id="provision",
        attempt=0,
    )
    outcome = await ProvisionWorkspace().execute({}, ctx)
    assert outcome.label == "success"
    workspace_id = outcome.outputs.get("workspace_id")
    assert workspace_id is not None

    row = (
        await db_session.execute(select(WorkspaceRow).where(WorkspaceRow.id == UUID(workspace_id)))
    ).scalar_one()
    assert row.org_id == org_id
    assert row.provider_id == "stub_ws"
    assert row.status == WorkspaceStatus.ACTIVE.value
    assert row.spec["sha"] == "deadbeefcafef00d"
    assert row.spec["base_sha"] == "babecafe"


# ── RefreshWorkspaceAuth ────────────────────────────────────────────────


async def test_refresh_workspace_auth_execute_returns_success() -> None:
    """RefreshWorkspaceAuth.execute() returns success. On the remote path
    the engine dispatches the AgentCommand; the inline body is a stub for
    test providers."""
    outcome = await RefreshWorkspaceAuth().execute({}, _ctx())
    assert outcome.label == "success"
