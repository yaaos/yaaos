"""Service-level coverage for `core/agent_gateway` — per-agent FIFO,
heartbeat reconciliation, terminal event routing, stale-claim guard."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.agent_gateway import (
    AgentEvent,
    AgentEventKind,
    AuthBlock,
    CreateWorkspaceCommand,
    HeartbeatRequest,
    HeartbeatWorkspaceEntry,
    RepoRef,
    StaleClaimError,
    WorkspaceEvent,
    WorkspaceEventKind,
    _reset_queues_for_tests,
    claim_next,
    enqueue_command,
    queue_depth,
    record_agent_event,
    record_heartbeat,
    record_workspace_event,
)
from app.core.outbox.models import OutboxEntryRow
from app.core.workspace.models import WorkspaceRow


@pytest.fixture(autouse=True)
def _isolate_queues() -> None:
    _reset_queues_for_tests()
    yield
    _reset_queues_for_tests()


def _make_create_command() -> CreateWorkspaceCommand:
    return CreateWorkspaceCommand(
        command_id=uuid4(),
        workspace_id=uuid4(),
        traceparent="00-aabbccdd-1122-01",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="redacted"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )


async def _seed_workspace(db_session, *, claimed_by_command: bool = True) -> WorkspaceRow:
    cmd_id = uuid4()
    wfx_id = uuid4()
    row = WorkspaceRow(
        id=uuid4(),
        org_id=uuid4(),
        provider_id="in_memory",
        provider="remote_agent",
        spec={"sha": "deadbeef"},
        plugin_state={},
        status="active",
        activated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
        max_idle_seconds=600,
        current_command_id=cmd_id if claimed_by_command else None,
        current_holder_workflow_id=wfx_id if claimed_by_command else None,
    )
    db_session.add(row)
    await db_session.flush()
    row.__dict__["_test_seeded_command_id"] = cmd_id
    row.__dict__["_test_seeded_workflow_id"] = wfx_id
    return row


# ── In-memory FIFO ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claim_returns_none_immediately_when_empty() -> None:
    agent = uuid4()
    assert await claim_next(agent, wait_seconds=0) is None


@pytest.mark.asyncio
async def test_enqueue_then_claim() -> None:
    agent = uuid4()
    cmd = _make_create_command()
    await enqueue_command(agent, cmd)
    assert queue_depth(agent) == 1
    claimed = await claim_next(agent, wait_seconds=0)
    assert claimed is cmd
    assert queue_depth(agent) == 0


@pytest.mark.asyncio
async def test_per_agent_queues_are_independent() -> None:
    a, b = uuid4(), uuid4()
    cmd_a = _make_create_command()
    cmd_b = _make_create_command()
    await enqueue_command(a, cmd_a)
    await enqueue_command(b, cmd_b)
    assert (await claim_next(a, wait_seconds=0)) is cmd_a
    assert (await claim_next(b, wait_seconds=0)) is cmd_b


@pytest.mark.asyncio
async def test_long_poll_wakes_on_enqueue() -> None:
    """A blocked claim_next returns the command as soon as enqueue_command
    runs on the same agent — verifies the per-agent condition wires up
    correctly."""
    agent = uuid4()
    cmd = _make_create_command()

    async def _enqueue_after_delay() -> None:
        await asyncio.sleep(0.05)
        await enqueue_command(agent, cmd)

    enqueue_task = asyncio.create_task(_enqueue_after_delay())
    claimed = await claim_next(agent, wait_seconds=2)
    await enqueue_task
    assert claimed is cmd


@pytest.mark.asyncio
async def test_long_poll_times_out_returning_none() -> None:
    agent = uuid4()
    claimed = await claim_next(agent, wait_seconds=1)
    assert claimed is None


# ── Heartbeat reconciliation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_forgets_unknown_workspaces(db_session) -> None:
    known = await _seed_workspace(db_session, claimed_by_command=False)
    unknown_id = uuid4()

    request = HeartbeatRequest(
        reported_at=datetime.now(UTC),
        workspaces=(
            HeartbeatWorkspaceEntry(workspace_id=known.id, status="running"),
            HeartbeatWorkspaceEntry(workspace_id=unknown_id, status="running"),
        ),
    )
    response = await record_heartbeat(uuid4(), request, session=db_session)

    assert set(response.forgotten_workspaces) == {unknown_id}


@pytest.mark.asyncio
async def test_heartbeat_with_no_workspaces_returns_empty_forget_list(db_session) -> None:
    request = HeartbeatRequest(reported_at=datetime.now(UTC), workspaces=())
    response = await record_heartbeat(uuid4(), request, session=db_session)
    assert response.forgotten_workspaces == ()


# ── Event routing + stale-claim guard ──────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_event_enqueues_workflow_handle_agent_event(db_session) -> None:
    ws = await _seed_workspace(db_session)
    cmd_id = ws.__dict__["_test_seeded_command_id"]
    wfx_id = ws.__dict__["_test_seeded_workflow_id"]

    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={"workspace_id": str(ws.id)},
        reported_at=datetime.now(UTC),
        traceparent="00-aabbccdd-1122-01",
    )
    await record_agent_event(event, session=db_session)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(OutboxEntryRow).where(OutboxEntryRow.kind == "taskiq_enqueue")))
        .scalars()
        .all()
    )
    matching = [
        r
        for r in rows
        if r.payload.get("task_name") == "workflow.handle_agent_event"
        and r.payload.get("args", {}).get("workflow_execution_id") == str(wfx_id)
        and r.payload.get("args", {}).get("agent_command_id") == str(cmd_id)
    ]
    assert matching, "expected one workflow.handle_agent_event outbox row"


@pytest.mark.asyncio
async def test_progress_event_does_not_enqueue_workflow(db_session) -> None:
    ws = await _seed_workspace(db_session)
    cmd_id = ws.__dict__["_test_seeded_command_id"]

    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.PROGRESS,
        reported_at=datetime.now(UTC),
        traceparent="00-aabbccdd-1122-01",
    )
    await record_agent_event(event, session=db_session)
    await db_session.commit()

    rows = (
        (await db_session.execute(select(OutboxEntryRow).where(OutboxEntryRow.kind == "taskiq_enqueue")))
        .scalars()
        .all()
    )
    assert not any(r.payload.get("task_name") == "workflow.handle_agent_event" for r in rows)


@pytest.mark.asyncio
async def test_progress_event_publishes_to_sse_pubsub(db_session) -> None:
    """Slice 77: progress AgentEvents posted via HTTP get republished to
    the `activity:{workflow_execution_id}` SSE channel so the SPA's
    live-tail picks them up alongside batched WebSocket events."""
    from app.core.sse_pubsub import _reset_for_tests, channel_for, subscribe  # noqa: PLC0415

    _reset_for_tests()

    ws = await _seed_workspace(db_session)
    cmd_id = ws.__dict__["_test_seeded_command_id"]
    wfx_id = ws.__dict__["_test_seeded_workflow_id"]

    # Open an SSE subscriber BEFORE posting the event so the in-memory
    # pubsub buffers the publish on the channel.
    channel = channel_for(str(wfx_id))
    sub = subscribe(channel)
    received: list[dict] = []

    async def _drain() -> None:
        async for evt in sub:
            received.append(evt)
            if len(received) >= 1:
                return

    drainer = asyncio.create_task(_drain())
    # Yield once so the subscriber registers before the publish fires.
    await asyncio.sleep(0)

    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.PROGRESS,
        outputs={"stream_line": '{"type":"tool_use"}'},
        reported_at=datetime.now(UTC),
        traceparent="00-aabbccdd-1122-01",
    )
    await record_agent_event(event, session=db_session)
    await db_session.commit()
    try:
        await asyncio.wait_for(drainer, timeout=2.0)
    except TimeoutError as exc:
        drainer.cancel()
        raise AssertionError("progress event never reached the SSE channel") from exc

    assert len(received) == 1, f"expected one event on the channel, got {received}"
    got = received[0]
    assert got["kind"] == "progress"
    assert got["command_id"] == str(cmd_id)
    assert got["outputs"]["stream_line"] == '{"type":"tool_use"}'


@pytest.mark.asyncio
async def test_stale_command_id_raises(db_session) -> None:
    """An event whose command_id doesn't match any workspace's current
    claim raises StaleClaimError so the endpoint can map to 410."""
    event = AgentEvent(
        command_id=uuid4(),  # nothing holds this
        kind=AgentEventKind.COMPLETED_SUCCESS,
        reported_at=datetime.now(UTC),
        traceparent="00-aabbccdd-1122-01",
    )
    with pytest.raises(StaleClaimError):
        await record_agent_event(event, session=db_session)


@pytest.mark.asyncio
async def test_workspace_event_ready_transitions_to_active(db_session) -> None:
    ws = await _seed_workspace(db_session)
    cmd_id = ws.__dict__["_test_seeded_command_id"]

    # Demote status to creating so we can observe the transition.
    ws.status = "creating"
    await db_session.flush()

    event = WorkspaceEvent(
        workspace_id=ws.id,
        command_id=cmd_id,
        kind=WorkspaceEventKind.READY,
        reported_at=datetime.now(UTC),
    )
    await record_workspace_event(event, session=db_session)
    await db_session.flush()
    await db_session.refresh(ws)
    assert ws.status == "active"


@pytest.mark.asyncio
async def test_workspace_event_with_stale_command_raises(db_session) -> None:
    ws = await _seed_workspace(db_session)
    event = WorkspaceEvent(
        workspace_id=ws.id,
        command_id=uuid4(),  # mismatched
        kind=WorkspaceEventKind.READY,
        reported_at=datetime.now(UTC),
    )
    with pytest.raises(StaleClaimError):
        await record_workspace_event(event, session=db_session)
