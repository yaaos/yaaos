"""Service tests for coding-agent run lifecycle.

Covers:
- `create_run` at dispatch writes a row with status=running.
- `finalize_run` at terminal event writes status/exit_code/duration_ms.
- The run-sink fires only for InvokeClaudeCode terminal events; all other
  command kinds are no-ops.
- `reviews.run_id` is populated when `PostFindings` runs after a CodeReview.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

import app.web  # noqa: F401 — registers all models so FK metadata resolves correctly
from app.domain.coding_agent.models import CodingAgentRunRow
from app.domain.coding_agent.run_service import (
    create_run,
    finalize_run,
    get_run_id_for_command,
    get_run_id_for_workflow_step,
)
from app.domain.coding_agent.run_sink_impl import CodingAgentRunSinkImpl

# ── helpers ────────────────────────────────────────────────────────────────────


async def _seed_run(
    db_session,
    *,
    org_id: uuid.UUID,
    command_kind: str = "review",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Insert a coding_agent_run with status=running. Returns (run_id, wfe_id, cmd_id, agent_cmd_id)."""
    wfe_id = uuid.uuid4()
    cmd_id = uuid.uuid4()
    run_id = await create_run(
        org_id=org_id,
        workflow_execution_id=wfe_id,
        step_id="review",
        agent_command_id=cmd_id,
        command_kind=command_kind,
        session=db_session,
    )
    return run_id, wfe_id, cmd_id, cmd_id


# ── create_run ─────────────────────────────────────────────────────────────────


@pytest.mark.service
@pytest.mark.asyncio
async def test_create_run_inserts_running_row(db_session) -> None:
    """create_run inserts a row with status=running and correct fields."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()
    cmd_id = uuid.uuid4()

    run_id = await create_run(
        org_id=org_id,
        workflow_execution_id=wfe_id,
        step_id="review",
        agent_command_id=cmd_id,
        command_kind="review",
        session=db_session,
    )

    assert run_id is not None
    row = (
        await db_session.execute(select(CodingAgentRunRow).where(CodingAgentRunRow.id == run_id))
    ).scalar_one_or_none()
    assert row is not None
    assert row.status == "running"
    assert row.org_id == org_id
    assert row.workflow_execution_id == wfe_id
    assert row.step_id == "review"
    assert row.agent_command_id == cmd_id
    assert row.command_kind == "review"
    assert row.tokens_in is None
    assert row.tokens_out is None
    assert row.exit_code is None
    assert row.duration_ms is None
    assert row.completed_at is None


# ── finalize_run ───────────────────────────────────────────────────────────────


@pytest.mark.service
@pytest.mark.asyncio
async def test_finalize_run_writes_status_exit_code_duration(db_session) -> None:
    """finalize_run writes status, exit_code, and a non-negative duration_ms."""
    org_id = uuid.uuid4()
    run_id, *_ = await _seed_run(db_session, org_id=org_id)

    await finalize_run(
        run_id,
        usage=None,
        activity=None,
        exit_code=0,
        status="success",
        session=db_session,
    )

    row = (
        await db_session.execute(select(CodingAgentRunRow).where(CodingAgentRunRow.id == run_id))
    ).scalar_one()
    assert row.status == "success"
    assert row.exit_code == 0
    assert row.duration_ms is not None
    assert row.duration_ms >= 0
    assert row.completed_at is not None
    # Token usage is NULL until usage parsing is wired.
    assert row.tokens_in is None
    assert row.tokens_out is None


@pytest.mark.service
@pytest.mark.asyncio
async def test_finalize_run_failure_status(db_session) -> None:
    """finalize_run writes status=failure + non-zero exit_code."""
    org_id = uuid.uuid4()
    run_id, *_ = await _seed_run(db_session, org_id=org_id)

    await finalize_run(
        run_id,
        usage=None,
        activity=None,
        exit_code=1,
        status="failure",
        session=db_session,
    )

    row = (
        await db_session.execute(select(CodingAgentRunRow).where(CodingAgentRunRow.id == run_id))
    ).scalar_one()
    assert row.status == "failure"
    assert row.exit_code == 1


# ── run-sink ───────────────────────────────────────────────────────────────────


@pytest.mark.service
@pytest.mark.asyncio
async def test_run_sink_fires_only_for_invoke_claude_code(db_session) -> None:
    """The run-sink is a no-op for non-InvokeClaudeCode command kinds."""
    org_id = uuid.uuid4()
    run_id, _, _cmd_id, _ = await _seed_run(db_session, org_id=org_id, command_kind="review")

    sink = CodingAgentRunSinkImpl()

    # Fire for ProvisionWorkspace — should be a no-op (no run row for this cmd).
    prov_cmd_id = uuid.uuid4()
    await sink.handle_terminal_event(
        command_id=prov_cmd_id,
        command_kind="ProvisionWorkspace",
        event_kind="completed_success",
        outputs={},
        session=db_session,
    )

    # Run row should be unmodified (still "running").
    row = (
        await db_session.execute(select(CodingAgentRunRow).where(CodingAgentRunRow.id == run_id))
    ).scalar_one()
    assert row.status == "running"


@pytest.mark.service
@pytest.mark.asyncio
async def test_run_sink_finalizes_invoke_claude_code_success(db_session) -> None:
    """The run-sink finalizes the run row on InvokeClaudeCode completed_success."""
    org_id = uuid.uuid4()
    run_id, _, cmd_id, _ = await _seed_run(db_session, org_id=org_id, command_kind="review")

    sink = CodingAgentRunSinkImpl()
    await sink.handle_terminal_event(
        command_id=cmd_id,
        command_kind="InvokeClaudeCode",
        event_kind="completed_success",
        outputs={"exit_code": 0, "stdout": ""},
        session=db_session,
    )

    row = (
        await db_session.execute(select(CodingAgentRunRow).where(CodingAgentRunRow.id == run_id))
    ).scalar_one()
    assert row.status == "success"
    assert row.exit_code == 0


@pytest.mark.service
@pytest.mark.asyncio
async def test_run_sink_finalizes_invoke_claude_code_failure(db_session) -> None:
    """The run-sink finalizes the run row on InvokeClaudeCode completed_failure."""
    org_id = uuid.uuid4()
    run_id, _, cmd_id, _ = await _seed_run(db_session, org_id=org_id, command_kind="review")

    sink = CodingAgentRunSinkImpl()
    await sink.handle_terminal_event(
        command_id=cmd_id,
        command_kind="InvokeClaudeCode",
        event_kind="completed_failure",
        outputs={"exit_code": 1, "stdout": ""},
        session=db_session,
    )

    row = (
        await db_session.execute(select(CodingAgentRunRow).where(CodingAgentRunRow.id == run_id))
    ).scalar_one()
    assert row.status == "failure"
    assert row.exit_code == 1


# ── reviews.run_id ─────────────────────────────────────────────────────────────


@pytest.mark.service
@pytest.mark.asyncio
async def test_reviews_run_id_populated(db_session) -> None:
    """reviews.run_id FK is set when publish_findings is called with a run_id.

    Uses zero findings so the VCS post_finding path is not exercised (no
    VCS plugin registration needed).
    """
    from app.domain.reviewer import publish_findings  # noqa: PLC0415

    org_id = uuid.uuid4()
    pr_id = uuid.uuid4()
    run_id, *_ = await _seed_run(db_session, org_id=org_id, command_kind="review")

    # Seed a minimal ticket row (required FK from pull_requests.ticket_id NOT NULL).
    # Include every NOT NULL column without a server_default (Python defaults are
    # app-side only and don't apply to raw SQL inserts).
    ticket_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO tickets"
            " (id, org_id, source, source_external_id, title, status)"
            " VALUES (:id, :org_id, 'github_pr', 'pr:1', 'Test ticket', 'open')"
        ),
        {"id": ticket_id, "org_id": org_id},
    )

    # Seed a minimal pull_requests row so publish_findings can insert the review.
    pr_ext = f"acme/repo#{uuid.uuid4().hex[:8]}"
    await db_session.execute(
        text(
            "INSERT INTO pull_requests"
            " (id, org_id, ticket_id, plugin_id, external_id, repo_external_id,"
            "  number, title, body, author_login, author_type, base_branch,"
            "  head_branch, base_sha, head_sha, is_draft, is_fork, state, html_url)"
            " VALUES (:id, :org_id, :ticket_id, 'github', :pr_ext, 'acme/repo',"
            "  1, 'Test PR', '', 'dev', 'user', 'main',"
            "  'feat', 'base', 'head', false, false, 'open', 'https://x')"
        ),
        {"id": pr_id, "org_id": org_id, "ticket_id": ticket_id, "pr_ext": pr_ext},
    )

    # Zero findings — VCS post_finding is never called, no plugin required.
    review, admitted = await publish_findings(
        pr_id=pr_id,
        org_id=org_id,
        pr_external_id=pr_ext,
        vcs_plugin_id="github",
        findings=[],
        run_id=run_id,
        session=db_session,
    )

    assert admitted == []
    # Verify the review row has run_id set.
    result = await db_session.execute(
        text("SELECT run_id FROM reviews WHERE id = :id"),
        {"id": review.id},
    )
    row = result.one_or_none()
    assert row is not None
    assert row[0] == run_id


# ── get_run_id_for_command / get_run_id_for_workflow_step ──────────────────────


@pytest.mark.service
@pytest.mark.asyncio
async def test_get_run_id_for_command(db_session) -> None:
    """get_run_id_for_command returns the run_id for a given agent_command_id."""
    org_id = uuid.uuid4()
    run_id, _, cmd_id, _ = await _seed_run(db_session, org_id=org_id)

    result = await get_run_id_for_command(cmd_id, session=db_session)
    assert result == run_id


@pytest.mark.service
@pytest.mark.asyncio
async def test_get_run_id_for_command_missing_returns_none(db_session) -> None:
    """get_run_id_for_command returns None for an unknown command_id."""
    result = await get_run_id_for_command(uuid.uuid4(), session=db_session)
    assert result is None


@pytest.mark.service
@pytest.mark.asyncio
async def test_get_run_id_for_workflow_step(db_session) -> None:
    """get_run_id_for_workflow_step returns the run_id for (wfe_id, step_id)."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()
    cmd_id = uuid.uuid4()

    run_id = await create_run(
        org_id=org_id,
        workflow_execution_id=wfe_id,
        step_id="review",
        agent_command_id=cmd_id,
        command_kind="review",
        session=db_session,
    )

    result = await get_run_id_for_workflow_step(wfe_id, "review", session=db_session)
    assert result == run_id

    # Wrong step_id returns None.
    result2 = await get_run_id_for_workflow_step(wfe_id, "other_step", session=db_session)
    assert result2 is None
