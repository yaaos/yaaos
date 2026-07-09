"""Service test: live-activity normalization via the HTTP progress path.

`record_agent_event` routes progress events through the `AgentRunSink`
which calls `plugin.parse_activity_line` and publishes a normalized
`{kind, ts, message, detail}` frame to the workspace-activity SSE channel.

Two cases:
- Correlated run + renderable line → normalized frame appears on channel.
- Progress event with no correlated run → nothing published.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest

from app.core.agent_gateway import (
    AgentEvent,
    AgentEventKind,
    AuthBlock,
    ProvisionWorkspaceCommand,
    RepoRef,
    enqueue_command,
    record_agent_event,
)
from app.core.audit_log import ActorKind
from app.core.auth import org_context
from app.core.coding_agent import create_run
from app.core.sse import subscribe_workspace_activity
from app.testing.e2e_setup import seed_agent
from app.testing.e2e_setup import seed_workspace as seed_workspace_row

pytestmark = pytest.mark.service


async def _seed_workspace_with_run(db_session):
    """Seed org + agent + workspace + claimed InvokeClaudeCode command + coding_agent_runs row.

    Returns {org_id, cmd_id, run_id, pipeline_run_id}.
    """
    cmd_id = uuid7()
    pipeline_run_id = uuid4()
    org_id = uuid4()
    agent = await seed_agent(org_id=org_id)
    ws_id = await seed_workspace_row(
        org_id=org_id,
        provider_id="remote_agent",
        sha="deadbeef",
        current_command_id=cmd_id,
        agent_id=agent["id"],
    )
    # `agent_commands` row with the matching run_id.
    command = ProvisionWorkspaceCommand(
        command_id=cmd_id,
        workspace_id=UUID(ws_id),
        traceparent="00-aabbccdd-1122-01",
        repo=RepoRef(
            plugin_id="github",
            external_id="456",
            clone_url="https://github.com/me/repo.git",
            head_sha="cafebabe",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="redacted"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )
    await enqueue_command(
        org_id=org_id,
        command=command,
        session=db_session,
        run_id=pipeline_run_id,
    )
    # `coding_agent_runs` row linking agent_command → run so the sink can
    # resolve the plugin + publish the normalized frame.
    await create_run(
        org_id=org_id,
        run_id=pipeline_run_id,
        stage_execution_id=uuid4(),  # any UUID — no FK constraint
        agent_command_id=cmd_id,
        command_kind="InvokeClaudeCode",
        plugin_id="claude_code",  # stub wraps this; both resolve via get_plugin
        session=db_session,
    )
    await db_session.flush()
    return {
        "org_id": org_id,
        "cmd_id": cmd_id,
        "run_id": pipeline_run_id,
    }


@pytest.mark.asyncio
async def test_progress_with_correlated_run_publishes_normalized_frame(db_session, redis_or_skip) -> None:
    """A progress event whose command has a correlated coding_agent_runs row
    results in a normalized `{kind, ts, message, detail}` frame on the
    workspace-activity SSE channel — NOT a raw AgentEvent dump.

    The stub plugin's `parse_activity_line` maps any non-blank line to an
    `assistant_message` event, so we use a non-blank stream_line to get a
    deterministic non-None render.
    """
    seeded = await _seed_workspace_with_run(db_session)
    org_id = seeded["org_id"]
    cmd_id = seeded["cmd_id"]
    run_id = seeded["run_id"]

    sub = subscribe_workspace_activity(org_id, run_id)
    received: list[dict] = []

    async def _drain() -> None:
        async for evt in sub:
            received.append(evt)
            if len(received) >= 1:
                return

    drainer = asyncio.create_task(_drain())
    await asyncio.sleep(0)  # let subscriber register before publish fires

    stream_line = "reading the code"  # non-blank → stub renders assistant_message
    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.PROGRESS,
        outputs={"stream_line": stream_line},
        reported_at=datetime.now(UTC),
        traceparent="00-aabbccdd-1122-01",
    )
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, session=db_session)

    try:
        await asyncio.wait_for(drainer, timeout=3.0)
    except TimeoutError as exc:
        drainer.cancel()
        raise AssertionError("normalized frame never reached workspace-activity channel") from exc

    assert len(received) == 1
    frame = received[0]
    # Normalized shape — NOT a raw AgentEvent dict.
    assert "kind" in frame
    assert "ts" in frame
    assert "message" in frame
    assert "detail" in frame
    # Raw AgentEvent fields must NOT appear.
    assert "command_id" not in frame
    assert "outputs" not in frame
    # Stub renders every non-blank line as assistant_message with message=line.
    assert frame["kind"] == "assistant_message"
    assert frame["message"] == stream_line


@pytest.mark.asyncio
async def test_progress_blank_stream_line_publishes_nothing(db_session, redis_or_skip) -> None:
    """A stream_line that `parse_activity_line` returns None for (blank line →
    stub returns None) publishes nothing to the workspace-activity channel.

    Verifies the gate between render and publish is honored.
    """
    seeded = await _seed_workspace_with_run(db_session)
    org_id = seeded["org_id"]
    cmd_id = seeded["cmd_id"]
    run_id = seeded["run_id"]

    sub = subscribe_workspace_activity(org_id, run_id)
    received: list[dict] = []

    async def _drain() -> None:
        async for evt in sub:
            received.append(evt)

    drainer = asyncio.create_task(_drain())
    await asyncio.sleep(0)

    blank_line = "   "  # blank → stub returns None → no publish
    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.PROGRESS,
        outputs={"stream_line": blank_line},
        reported_at=datetime.now(UTC),
        traceparent="00-aabbccdd-1122-01",
    )
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, session=db_session)

    await asyncio.sleep(0.3)
    drainer.cancel()
    try:
        await drainer
    except asyncio.CancelledError:
        pass

    assert received == [], f"blank stream_line should not publish to channel; got: {received}"
