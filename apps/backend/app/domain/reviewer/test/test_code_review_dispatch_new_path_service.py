"""Service test: CodeReview.dispatch via new coding_agent.dispatch_invocation path.

Drives `CodeReview.dispatch` directly (bypassing the workflow engine) and asserts:
- An `agent_commands` row is created with the correct `workflow_execution_id`.
- A `coding_agent_runs` row is created with `plugin_id="claude_code"` and the
  matching `agent_command_id`.
- The workspace claim is acquired (workspace holds the returned `command_id`).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.core.coding_agent import get_run_id_for_command, get_run_id_for_workflow_step
from app.core.workflow import CommandContext
from app.core.workspace import (
    WorkspaceTicketContext,
    get_workspace_command_state,
    register_workflow_context_provider,
)
from app.domain.reviewer.commands import CodeReview
from app.testing.seed import seed_agent as _seed_agent
from app.testing.seed import seed_workspace as _seed_workspace

pytestmark = pytest.mark.service


class _StaticTicketContextProvider:
    def __init__(self, ctx: WorkspaceTicketContext) -> None:
        self._ctx = ctx

    async def get_workspace_ticket_context(self, ticket_id: UUID) -> WorkspaceTicketContext:
        del ticket_id
        return self._ctx


@pytest.mark.asyncio
async def test_code_review_dispatch_new_path(
    db_session,
    workflow_context_provider_isolation,
    plugin_registries_isolation,
) -> None:
    """CodeReview.dispatch inserts agent_commands + coding_agent_runs rows and claims
    the workspace via the new coding_agent.dispatch_invocation path."""
    org_id = uuid4()
    wfx_id = uuid4()

    # Seed a reachable agent + workspace so dispatch's ownership guard passes.
    agent_row = await _seed_agent(org_id=org_id, session=db_session)
    agent_id = agent_row["id"]
    ws_id_str = await _seed_workspace(
        org_id=org_id,
        provider_id="in_process",
        sha="deadbeef",
        agent_id=agent_id,
        caller_session=db_session,
    )
    ws_id = UUID(str(ws_id_str))
    await db_session.commit()

    # Install a context provider that returns a minimal valid WorkspaceTicketContext.
    register_workflow_context_provider(
        _StaticTicketContextProvider(
            WorkspaceTicketContext(
                org_id=org_id,
                plugin_id="github",
                repo_external_id="owner/repo",
                payload={
                    "head_sha": "deadbeef",
                    "base_sha": "babecafe",
                    "pr_external_id": "42",
                },
            )
        )
    )

    ctx = CommandContext(
        ticket_id=str(uuid4()),
        workflow_execution_id=str(wfx_id),
        step_id="review",
        attempt=0,
    )

    cmd = CodeReview()
    command_id = await cmd.dispatch(
        {"workspace_id": str(ws_id)},
        ctx,
        session=db_session,
    )
    await db_session.commit()

    # agent_commands row was created — verify via the workflow_execution_id lookup.
    from app.core.agent_gateway import get_command_workflow_execution_id  # noqa: PLC0415

    resolved_wfx_id = await get_command_workflow_execution_id(command_id, session=db_session)
    assert resolved_wfx_id == wfx_id, (
        f"agent_commands workflow_execution_id mismatch: expected {wfx_id}, got {resolved_wfx_id}"
    )

    # coding_agent_runs row was created — get_run_id_for_command returns a UUID iff
    # an InvokeClaudeCode command has a run row (the only kind that gets one).
    run_id = await get_run_id_for_command(command_id, session=db_session)
    assert run_id is not None, "coding_agent_runs row not found for the dispatched command"

    # Step-keyed lookup also resolves — confirms step_id="review" on the run row.
    step_run_id = await get_run_id_for_workflow_step(wfx_id, "review", session=db_session)
    assert step_run_id == run_id, (
        f"get_run_id_for_workflow_step(wfx_id, 'review') returned {step_run_id!r}; expected {run_id!r}"
    )

    # Workspace claim was acquired — get_workspace_command_state returns the
    # command_id that currently claims the workspace.
    claim_state = await get_workspace_command_state(ws_id, db_session)
    assert claim_state is not None, "workspace command state not found"
    assert claim_state.current_command_id == command_id, (
        f"workspace claim not acquired; current_command_id={claim_state.current_command_id!r}"
    )
