"""Service-level coverage for `core/agent_gateway` — per-agent FIFO,
heartbeat reconciliation, terminal event routing, stale-claim guard."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.agent_gateway import (
    AgentEvent,
    AgentEventKind,
    AgentRef,
    AuthBlock,
    CreateWorkspaceCommand,
    HeartbeatRequest,
    HeartbeatWorkspaceEntry,
    RepoRef,
    StaleClaimError,
    WorkspaceEvent,
    WorkspaceEventKind,
    claim_next,
    clear_queues,
    enqueue_command,
    has_any_reachable_agent,
    pick_agent_for_org,
    queue_depth,
    record_agent_event,
    record_heartbeat,
    record_workspace_event,
)
from app.core.tasks import drain_once
from app.core.workflow import (
    CommandCategory,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    WorkflowExecutionRow,
    WorkflowState,
    scoped_engine,
)
from app.core.workspace import WorkspaceRow


@pytest.fixture(autouse=True)
def _isolate_queues() -> None:
    clear_queues()
    yield
    clear_queues()


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
async def test_terminal_event_advances_workflow_to_done(db_session) -> None:
    """A terminal AgentEvent for a Workspace step causes the workflow to
    advance: record_agent_event enqueues handle_agent_event, and draining
    that task drives the workflow to DONE."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    class _NoopWs:
        kind = "NoopWs"
        category = CommandCategory.WORKSPACE
        restart_safe = True

        async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
            del inputs, ctx
            return Outcome.success()

    with scoped_engine() as eng:
        eng.register_command(_NoopWs())
        eng.register_workflow(
            Workflow(
                name="gw-terminal-test",
                version=1,
                steps=(
                    Step(
                        id="ws",
                        command_kind="NoopWs",
                        transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
                    ),
                ),
                entry_step_id="ws",
            )
        )

        exec_id = await eng.start(
            workflow_name="gw-terminal-test",
            ticket_id=str(uuid4()),
            workspace_provider="remote_agent",
            session=db_session,
        )
        await db_session.commit()

        # Drain start_step → workspace stub dispatch → AWAITING_AGENT.
        async def _dispatcher(kind: str, payload: dict) -> None:
            assert kind == "taskiq_enqueue"
            decorated = get_broker().find_task(payload["task_name"])
            assert decorated is not None
            await decorated.original_func(**payload["args"])

        for _ in range(10):
            n = await drain_once(db_session, dispatcher=_dispatcher)
            await db_session.commit()
            if n == 0:
                break

        wfx = await db_session.get(WorkflowExecutionRow, exec_id)
        assert wfx.state == WorkflowState.AWAITING_AGENT.value
        cmd_id = wfx.pending_agent_command_id
        assert cmd_id is not None

        # Seed the workspace row pointing at this execution so record_agent_event
        # can look up the workspace by command_id.
        ws = WorkspaceRow(
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
            current_command_id=cmd_id,
            current_holder_workflow_id=exec_id,
        )
        db_session.add(ws)
        await db_session.flush()

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

        # Drain handle_agent_event + route_workflow → workflow reaches DONE.
        for _ in range(10):
            n = await drain_once(db_session, dispatcher=_dispatcher)
            await db_session.commit()
            if n == 0:
                break

        wfx = await db_session.get(WorkflowExecutionRow, exec_id)
        assert wfx.state == WorkflowState.DONE.value
        assert wfx.pending_agent_command_id is None


@pytest.mark.asyncio
async def test_progress_event_does_not_advance_workflow(db_session) -> None:
    """A PROGRESS AgentEvent does not advance the workflow — the execution
    stays in AWAITING_AGENT after the event is processed."""
    from app.core.tasks import get_broker  # noqa: PLC0415

    class _NoopWs2:
        kind = "NoopWs2"
        category = CommandCategory.WORKSPACE
        restart_safe = True

        async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
            del inputs, ctx
            return Outcome.success()

    with scoped_engine() as eng:
        eng.register_command(_NoopWs2())
        eng.register_workflow(
            Workflow(
                name="gw-progress-test",
                version=1,
                steps=(
                    Step(
                        id="ws",
                        command_kind="NoopWs2",
                        transitions={"success": TerminalAction.COMPLETE_WORKFLOW},
                    ),
                ),
                entry_step_id="ws",
            )
        )

        exec_id = await eng.start(
            workflow_name="gw-progress-test",
            ticket_id=str(uuid4()),
            workspace_provider="remote_agent",
            session=db_session,
        )
        await db_session.commit()

        async def _dispatcher(kind: str, payload: dict) -> None:
            assert kind == "taskiq_enqueue"
            decorated = get_broker().find_task(payload["task_name"])
            assert decorated is not None
            await decorated.original_func(**payload["args"])

        for _ in range(10):
            n = await drain_once(db_session, dispatcher=_dispatcher)
            await db_session.commit()
            if n == 0:
                break

        wfx = await db_session.get(WorkflowExecutionRow, exec_id)
        assert wfx.state == WorkflowState.AWAITING_AGENT.value
        cmd_id = wfx.pending_agent_command_id
        assert cmd_id is not None

        ws = WorkspaceRow(
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
            current_command_id=cmd_id,
            current_holder_workflow_id=exec_id,
        )
        db_session.add(ws)
        await db_session.flush()

        # Post a PROGRESS event — workflow must stay in AWAITING_AGENT.
        event = AgentEvent(
            command_id=cmd_id,
            kind=AgentEventKind.PROGRESS,
            reported_at=datetime.now(UTC),
            traceparent="00-aabbccdd-1122-01",
        )
        await record_agent_event(event, session=db_session)
        await db_session.commit()

        # Drain anything that was enqueued (should be nothing for a progress event).
        for _ in range(5):
            n = await drain_once(db_session, dispatcher=_dispatcher)
            await db_session.commit()
            if n == 0:
                break

        wfx = await db_session.get(WorkflowExecutionRow, exec_id)
        assert wfx.state == WorkflowState.AWAITING_AGENT.value, "progress event must not advance the workflow"


@pytest.mark.asyncio
async def test_progress_event_publishes_to_sse_pubsub(db_session, redis_or_skip) -> None:
    """Slice 77: progress AgentEvents posted via HTTP get republished to
    the `activity:{workflow_execution_id}` SSE channel so the SPA's
    live-tail picks them up alongside batched WebSocket events."""
    from app.core.sse_pubsub import channel_for, subscribe  # noqa: PLC0415
    from app.core.sse_pubsub import shutdown as sse_shutdown  # noqa: PLC0415

    await sse_shutdown()

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


# ── pick_agent_for_org + has_any_reachable_agent ───────────────────────


def _make_agent_row(org_id, *, state: str = "reachable", seconds_ago: int = 10):
    """Build a WorkspaceAgentRow for seeding. Returns the unsaved row."""
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    return WorkspaceAgentRow(
        org_id=org_id,
        agent_pod_id=uuid4(),
        iam_arn="arn:aws:iam::123456789012:role/test-role",
        version="0.1.0",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=seconds_ago),
        state=state,
    )


@pytest.mark.asyncio
async def test_pick_agent_for_org_returns_none_when_no_agents(db_session) -> None:
    result = await pick_agent_for_org(uuid4(), session=db_session)
    assert result is None


@pytest.mark.asyncio
async def test_pick_agent_for_org_returns_agent_ref(db_session) -> None:
    org_id = uuid4()
    row = _make_agent_row(org_id)
    db_session.add(row)
    await db_session.flush()

    result = await pick_agent_for_org(org_id, session=db_session)

    assert result is not None
    assert isinstance(result, AgentRef)
    assert result.agent_pod_id == row.agent_pod_id
    assert result.agent_id == row.id


@pytest.mark.asyncio
async def test_pick_agent_for_org_ignores_stale_heartbeat(db_session) -> None:
    org_id = uuid4()
    stale = _make_agent_row(org_id, seconds_ago=200)  # beyond 90-s cutoff
    db_session.add(stale)
    await db_session.flush()

    result = await pick_agent_for_org(org_id, session=db_session)
    assert result is None


@pytest.mark.asyncio
async def test_pick_agent_for_org_ignores_unreachable_state(db_session) -> None:
    org_id = uuid4()
    row = _make_agent_row(org_id, state="lost")
    db_session.add(row)
    await db_session.flush()

    result = await pick_agent_for_org(org_id, session=db_session)
    assert result is None


@pytest.mark.asyncio
async def test_pick_agent_for_org_prefers_less_loaded(db_session) -> None:
    """Among two reachable agents, the one with fewer queued commands wins."""
    org_id = uuid4()
    row_a = _make_agent_row(org_id, seconds_ago=5)
    row_b = _make_agent_row(org_id, seconds_ago=10)
    db_session.add(row_a)
    db_session.add(row_b)
    await db_session.flush()

    # Enqueue two commands for row_a so row_b is less loaded.
    await enqueue_command(row_a.id, _make_create_command())
    await enqueue_command(row_a.id, _make_create_command())

    result = await pick_agent_for_org(org_id, session=db_session)
    assert result is not None
    assert result.agent_pod_id == row_b.agent_pod_id


@pytest.mark.asyncio
async def test_has_any_reachable_agent_false_when_empty(db_session) -> None:
    assert await has_any_reachable_agent(session=db_session) is False


@pytest.mark.asyncio
async def test_has_any_reachable_agent_true_when_present(db_session) -> None:
    org_id = uuid4()
    row = _make_agent_row(org_id)
    db_session.add(row)
    await db_session.flush()

    assert await has_any_reachable_agent(session=db_session) is True


@pytest.mark.asyncio
async def test_has_any_reachable_agent_false_when_stale(db_session) -> None:
    org_id = uuid4()
    row = _make_agent_row(org_id, seconds_ago=200)
    db_session.add(row)
    await db_session.flush()

    assert await has_any_reachable_agent(session=db_session) is False
