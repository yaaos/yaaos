"""Service test: `CodingAgentRunSinkImpl.handle_terminal_event` calls
`plugin.parse_result(outputs)`, persists via `finalize_run`, and returns
`{"output": ..., "error_message": ...}`.

Drives the complete terminal-event path against real Postgres. Asserts:
- `coding_agent_runs` row has `tokens_in`, `tokens_out`, `duration_ms`,
  `exit_code`, and `status` populated after the sink fires.
- `coding_agent_activity` row has a JSONB `events` array.
- Sink return dict carries `output` and `error_message` keys.

Uses `register_fake_coding_agent` to get a deterministic `parse_result`
that ignores stub-mode wiring set by `YAAOS_CODING_AGENT_STUB=1` in the
test environment.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.core.coding_agent.models import CodingAgentActivityRow, CodingAgentRunRow
from app.core.coding_agent.run_service import create_run
from app.core.coding_agent.run_sink_impl import CodingAgentRunSinkImpl
from app.testing.fake_coding_agent import register_fake_coding_agent
from app.web import app as _web_app  # noqa: F401 — registers all models so FK metadata resolves

pytestmark = pytest.mark.service

# Known values returned by `FakeCodingAgentPlugin.parse_result`.
# Keep these in sync with `app/testing/fake_coding_agent/service.py`.
_FAKE_TOKENS_IN = 0
_FAKE_TOKENS_OUT = 0
_FAKE_DURATION_MS = 0


@pytest.mark.asyncio
async def test_sink_populates_run_row_and_activity_blob(db_session) -> None:
    """A completed_success terminal event via the sink writes token counts,
    duration_ms, exit_code, and status onto the run row and creates a
    coding_agent_activity blob.

    Uses `FakeCodingAgentPlugin` so the test is independent of stub-mode
    wiring in the test environment."""
    org_id = uuid.uuid4()
    cmd_id = uuid.uuid4()
    pipeline_run_id = uuid.uuid4()

    run_id = await create_run(
        org_id=org_id,
        run_id=pipeline_run_id,
        stage_execution_id=uuid.uuid4(),
        agent_command_id=cmd_id,
        command_kind="InvokeClaudeCode",
        plugin_id="claude_code",
        session=db_session,
    )

    with register_fake_coding_agent("claude_code"):
        sink = CodingAgentRunSinkImpl()
        result = await sink.handle_terminal_event(
            command_id=cmd_id,
            command_kind="InvokeClaudeCode",
            event_kind="completed_success",
            outputs={"exit_code": 0, "stdout": "stub output"},
            session=db_session,
        )

    # Run row should be finalized.
    run_row = (
        await db_session.execute(select(CodingAgentRunRow).where(CodingAgentRunRow.id == run_id))
    ).scalar_one()
    assert run_row.status == "success"
    assert run_row.exit_code == 0
    assert run_row.tokens_in == _FAKE_TOKENS_IN
    assert run_row.tokens_out == _FAKE_TOKENS_OUT
    assert run_row.duration_ms == _FAKE_DURATION_MS
    assert run_row.completed_at is not None

    # Activity blob should be persisted.
    activity_rows = (
        (
            await db_session.execute(
                select(CodingAgentActivityRow).where(CodingAgentActivityRow.run_id == run_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(activity_rows) == 1
    payload = activity_rows[0].payload
    assert isinstance(payload, dict)
    assert "events" in payload
    # FakeCodingAgentPlugin.parse_result returns ActivityLog(events=[]).
    assert isinstance(payload["events"], list)

    # Sink return dict carries output + error_message keys.
    assert result is not None
    assert result["output"] == "stub output"
    assert result["error_message"] is None


@pytest.mark.asyncio
async def test_sink_populates_failure_status(db_session) -> None:
    """A completed_failure terminal event writes status=failure."""
    org_id = uuid.uuid4()
    cmd_id = uuid.uuid4()
    pipeline_run_id = uuid.uuid4()

    await create_run(
        org_id=org_id,
        run_id=pipeline_run_id,
        stage_execution_id=uuid.uuid4(),
        agent_command_id=cmd_id,
        command_kind="InvokeClaudeCode",
        plugin_id="claude_code",
        session=db_session,
    )

    with register_fake_coding_agent("claude_code"):
        sink = CodingAgentRunSinkImpl()
        result = await sink.handle_terminal_event(
            command_id=cmd_id,
            command_kind="InvokeClaudeCode",
            event_kind="completed_failure",
            outputs={"exit_code": 1, "stdout": ""},
            session=db_session,
        )

    run_row = (
        await db_session.execute(
            select(CodingAgentRunRow).where(CodingAgentRunRow.agent_command_id == cmd_id)
        )
    ).scalar_one()
    assert run_row.status == "failure"
    assert run_row.exit_code == 1

    assert result is not None
    assert result["output"] == ""
    assert result["error_message"] is None


@pytest.mark.asyncio
async def test_sink_returns_none_for_non_invoke_kind(db_session) -> None:
    """Non-InvokeClaudeCode command kinds return None — no run row finalized."""
    sink = CodingAgentRunSinkImpl()
    result = await sink.handle_terminal_event(
        command_id=uuid.uuid4(),
        command_kind="ProvisionWorkspace",
        event_kind="completed_success",
        outputs={},
        session=db_session,
    )
    assert result is None
