"""Service test: CodeReview.dispatch via new coding_agent.dispatch_invocation path.

Drives `CodeReview.dispatch` directly (bypassing the workflow engine) and asserts:
- An `agent_commands` row is created with the correct `workflow_execution_id`.
- The workspace claim is acquired (workspace holds the returned `command_id`).
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.core import byok
from app.core.audit_log import Actor
from app.core.workflow import CommandContext
from app.core.workspace import get_workspace_command_state
from app.domain.orgs import create_org
from app.domain.reviewer.commands import CodeReview, CodeReviewInputs
from app.testing.e2e_setup import seed_agent as _seed_agent
from app.testing.e2e_setup import seed_workspace as _seed_workspace

pytestmark = pytest.mark.service


@pytest.mark.asyncio
async def test_code_review_dispatch_new_path(
    db_session,
    plugin_registries_isolation,
) -> None:
    """CodeReview.dispatch inserts agent_commands + coding_agent_runs rows and claims
    the workspace via the new coding_agent.dispatch_invocation path."""
    wfx_id = uuid4()

    # Seed a real org row — CodeReview.dispatch loads the Anthropic key from
    # byok at dispatch time, and the byok_keys FK requires an existing org.
    org = await create_org(db_session, slug=f"t-{uuid4().hex[:8]}", display_name="t")
    org_id = org.id

    # Seed a reachable agent + workspace so dispatch's ownership guard passes.
    agent_row = await _seed_agent(org_id=org_id)
    agent_id = agent_row["id"]
    ws_id_str = await _seed_workspace(
        org_id=org_id,
        provider_id="in_process",
        sha="deadbeef",
        agent_id=agent_id,
    )
    ws_id = UUID(str(ws_id_str))

    # CodeReview.dispatch loads the Anthropic key from byok before building
    # the invocation; seed one so dispatch doesn't bail with a missing-key error.
    await byok.set(org_id, "anthropic", "sk-test-key", actor=Actor.system(), session=db_session)
    await db_session.commit()

    # Build typed inputs directly — no context provider needed.
    inputs = CodeReviewInputs(
        workspace_id=ws_id,
        org_id=org_id,
        repo_external_id="owner/repo",
        pr_external_id="42",
        head_sha="deadbeef",
        base_sha="babecafe",
    )
    ctx = CommandContext(
        ticket_id=str(uuid4()),
        workflow_execution_id=str(wfx_id),
        step_id="review",
        attempt=0,
    )

    cmd = CodeReview()
    command_id = await cmd.dispatch(
        inputs,
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

    # Workspace claim was acquired — get_workspace_command_state returns the
    # command_id that currently claims the workspace.
    claim_state = await get_workspace_command_state(ws_id, db_session)
    assert claim_state is not None, "workspace command state not found"
    assert claim_state.current_command_id == command_id, (
        f"workspace claim not acquired; current_command_id={claim_state.current_command_id!r}"
    )
