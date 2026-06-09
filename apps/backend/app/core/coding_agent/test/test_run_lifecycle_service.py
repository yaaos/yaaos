"""Service tests for coding-agent run lifecycle.

Covers:
- `create_run` at dispatch writes a row with status=running.
- `finalize_run` at terminal event writes status/exit_code/duration_ms.
- The run-sink fires only for InvokeClaudeCode terminal events; all other
  command kinds are no-ops.
- The run-sink resolves the plugin from the run row's `plugin_id` and skips
  (logs + returns, no raise, run stays unfinalised) when that plugin is
  unregistered.
- `reviews.run_id` is populated when `PostFindings` runs after a CodeReview.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text

import app.web  # noqa: F401 — registers all models so FK metadata resolves correctly
from app.core.coding_agent.models import CodingAgentActivityRow, CodingAgentRunRow
from app.core.coding_agent.run_service import (
    create_run,
    finalize_run,
    get_run_id_for_command,
    get_run_id_for_workflow_step,
    get_step_activity,
)
from app.core.coding_agent.run_sink_impl import CodingAgentRunSinkImpl
from app.core.coding_agent.types import ActivityEvent, ActivityLog, Usage

# ── helpers ────────────────────────────────────────────────────────────────────


def datetime_now() -> datetime:
    return datetime.now(UTC)


async def _seed_run(
    db_session,
    *,
    org_id: uuid.UUID,
    command_kind: str = "review",
    plugin_id: str = "claude_code",
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
        plugin_id=plugin_id,
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
        plugin_id="claude_code",
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
    assert row.plugin_id == "claude_code"
    assert row.tokens_in is None
    assert row.tokens_out is None
    assert row.exit_code is None
    assert row.duration_ms is None
    assert row.completed_at is None


# ── finalize_run ───────────────────────────────────────────────────────────────


@pytest.mark.service
@pytest.mark.asyncio
async def test_finalize_run_writes_status_exit_code_duration(db_session) -> None:
    """finalize_run writes status, exit_code, a non-negative duration_ms, and tokens."""
    org_id = uuid.uuid4()
    run_id, *_ = await _seed_run(db_session, org_id=org_id)

    await finalize_run(
        run_id,
        usage=Usage(tokens_in=10, tokens_out=20, duration_ms=500),
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
    # `usage.duration_ms` takes precedence over wallclock when present.
    assert row.duration_ms == 500
    assert row.completed_at is not None
    # Token usage written from the supplied `Usage`.
    assert row.tokens_in == 10
    assert row.tokens_out == 20


@pytest.mark.service
@pytest.mark.asyncio
async def test_finalize_run_persists_activity_blob(db_session) -> None:
    """finalize_run inserts one coding_agent_activity row when `activity` is supplied."""
    org_id = uuid.uuid4()
    run_id, *_ = await _seed_run(db_session, org_id=org_id)

    activity = ActivityLog(
        events=(
            ActivityEvent(
                seq=0,
                ts=datetime_now(),
                kind="session_start",
                message="Session started · model opus",
                detail={"model": "opus"},
            ),
            ActivityEvent(
                seq=1,
                ts=datetime_now(),
                kind="result",
                message="Review complete",
                detail={"num_turns": 1},
            ),
        )
    )

    await finalize_run(
        run_id,
        usage=Usage(tokens_in=100, tokens_out=50, duration_ms=1200),
        activity=activity,
        exit_code=0,
        status="success",
        session=db_session,
    )

    rows = (
        (
            await db_session.execute(
                select(CodingAgentActivityRow).where(CodingAgentActivityRow.run_id == run_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.org_id == org_id
    assert isinstance(row.payload, dict)
    assert "events" in row.payload
    assert len(row.payload["events"]) == 2
    assert row.payload["events"][0]["seq"] == 0
    assert row.payload["events"][0]["kind"] == "session_start"
    assert row.payload["events"][1]["seq"] == 1
    assert row.payload["events"][1]["kind"] == "result"


@pytest.mark.service
@pytest.mark.asyncio
async def test_finalize_run_failure_status(db_session) -> None:
    """finalize_run writes status=failure + non-zero exit_code."""
    org_id = uuid.uuid4()
    run_id, *_ = await _seed_run(db_session, org_id=org_id)

    await finalize_run(
        run_id,
        usage=Usage(),
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


@pytest.mark.service
@pytest.mark.asyncio
async def test_run_sink_resolves_plugin_from_run_row(db_session) -> None:
    """The sink resolves the plugin from the run row's `plugin_id`, not a
    constant. A run seeded with the registered plugin_id finalizes."""
    org_id = uuid.uuid4()
    run_id, _, cmd_id, _ = await _seed_run(
        db_session, org_id=org_id, command_kind="review", plugin_id="claude_code"
    )

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
    assert row.plugin_id == "claude_code"
    assert row.status == "success"


@pytest.mark.service
@pytest.mark.asyncio
async def test_run_sink_skips_when_plugin_unregistered(db_session) -> None:
    """If the run's plugin_id is not in the registry, the sink logs + returns
    without raising and without finalizing the run row (defensive: the sink
    may be loaded without the issuing plugin in a misconfigured env)."""
    org_id = uuid.uuid4()
    run_id, _, cmd_id, _ = await _seed_run(
        db_session, org_id=org_id, command_kind="review", plugin_id="unregistered_plugin"
    )

    sink = CodingAgentRunSinkImpl()
    # Must not raise even though the plugin is absent from the registry.
    await sink.handle_terminal_event(
        command_id=cmd_id,
        command_kind="InvokeClaudeCode",
        event_kind="completed_success",
        outputs={"exit_code": 0, "stdout": ""},
        session=db_session,
    )

    # Run row stays unfinalised (still "running").
    row = (
        await db_session.execute(select(CodingAgentRunRow).where(CodingAgentRunRow.id == run_id))
    ).scalar_one()
    assert row.status == "running"
    assert row.completed_at is None


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
        plugin_id="claude_code",
        session=db_session,
    )

    result = await get_run_id_for_workflow_step(wfe_id, "review", session=db_session)
    assert result == run_id

    # Wrong step_id returns None.
    result2 = await get_run_id_for_workflow_step(wfe_id, "other_step", session=db_session)
    assert result2 is None


# ── get_step_activity ──────────────────────────────────────────────────────────


@pytest.mark.service
@pytest.mark.asyncio
async def test_get_step_activity_returns_activity_log_when_present(db_session) -> None:
    """get_step_activity returns the ActivityLog for (wfx_id, step_id) when
    a finalize_run with an activity blob has landed."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()
    cmd_id = uuid.uuid4()

    run_id = await create_run(
        org_id=org_id,
        workflow_execution_id=wfe_id,
        step_id="review",
        agent_command_id=cmd_id,
        command_kind="review",
        plugin_id="claude_code",
        session=db_session,
    )

    activity = ActivityLog(
        events=(
            ActivityEvent(
                seq=0,
                ts=datetime_now(),
                kind="session_start",
                message="Session started",
                detail={"model": "opus"},
            ),
        )
    )
    await finalize_run(
        run_id,
        usage=Usage(tokens_in=1, tokens_out=2, duration_ms=10),
        activity=activity,
        exit_code=0,
        status="success",
        session=db_session,
    )

    result = await get_step_activity(wfe_id, "review", session=db_session)
    assert result is not None
    assert isinstance(result, ActivityLog)
    assert len(result.events) == 1
    assert result.events[0].kind == "session_start"


@pytest.mark.service
@pytest.mark.asyncio
async def test_get_step_activity_returns_none_when_no_run(db_session) -> None:
    """get_step_activity returns None when no run exists for the workflow step
    (e.g. a non-InvokeClaudeCode step, or a step that hasn't dispatched)."""
    result = await get_step_activity(uuid.uuid4(), "review", session=db_session)
    assert result is None


@pytest.mark.service
@pytest.mark.asyncio
async def test_get_step_activity_returns_none_when_partition_aged_out(db_session) -> None:
    """get_step_activity returns None when the run row exists but no activity
    blob was persisted (simulating a dropped weekly partition past the 4-week TTL).
    The SPA renders this as 'activity expired'."""
    org_id = uuid.uuid4()
    wfe_id = uuid.uuid4()
    cmd_id = uuid.uuid4()

    run_id = await create_run(
        org_id=org_id,
        workflow_execution_id=wfe_id,
        step_id="review",
        agent_command_id=cmd_id,
        command_kind="review",
        plugin_id="claude_code",
        session=db_session,
    )
    # Finalize with `activity=None` — no row is inserted in coding_agent_activity.
    await finalize_run(
        run_id,
        usage=Usage(),
        activity=None,
        exit_code=0,
        status="success",
        session=db_session,
    )

    result = await get_step_activity(wfe_id, "review", session=db_session)
    assert result is None
