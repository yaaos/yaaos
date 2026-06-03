"""Service-level coverage for `core/agent_gateway` — heartbeat reconciliation,
terminal event routing, stale-claim guard, and agent liveness helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

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
    has_any_reachable_agent,
    record_agent_event,
    record_heartbeat,
    record_workspace_event,
)
from app.core.plugin_kit import PluginMeta
from app.core.tasks import drain_once
from app.core.workflow import (
    CommandCategory,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    WorkflowState,
    get_execution_summary,
)
from app.core.workspace import WorkspaceRegistry, bind_workspace_registry, register_workspace_provider
from app.testing.seed import seed_workspace as _seed_workspace_for_tests
from app.testing.workflow_harness import scoped_engine


class _MinimalWorkspaceProvider:
    """Stub WorkspaceProvider so `list_workspace_providers()` returns exactly
    one entry when Workspace commands dispatch through the engine in tests."""

    meta = PluginMeta(id="gw_test_stub", type="workspace", display_name="gw-test-stub")

    async def provision(self, spec):  # type: ignore[no-untyped-def]
        return {}

    async def destroy(self, plugin_state):  # type: ignore[no-untyped-def]
        return None

    async def health_check(self, plugin_state):  # type: ignore[no-untyped-def]
        return None

    async def run_coding_agent_cli(self, plugin_state, argv, **kwargs):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def read_text(self, plugin_state, path):  # type: ignore[no-untyped-def]
        return None

    async def write_text(self, plugin_state, path, content):  # type: ignore[no-untyped-def]
        return None


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


async def _seed_workspace(db_session, *, claimed_by_command: bool = True) -> dict:
    cmd_id = uuid4()
    wfx_id = uuid4()
    org_id = uuid4()
    ws_id = await _seed_workspace_for_tests(
        org_id=org_id,
        provider_id="remote_agent",
        plugin_state={},
        sha="deadbeef",
        current_command_id=cmd_id if claimed_by_command else None,
        current_holder_workflow_id=wfx_id if claimed_by_command else None,
        caller_session=db_session,
    )
    return {"id": ws_id, "org_id": org_id, "command_id": cmd_id, "workflow_id": wfx_id}


# ── Heartbeat reconciliation ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_forgets_unknown_workspaces(db_session) -> None:
    known = await _seed_workspace(db_session, claimed_by_command=False)
    unknown_id = uuid4()

    request = HeartbeatRequest(
        reported_at=datetime.now(UTC),
        workspaces=(
            HeartbeatWorkspaceEntry(workspace_id=UUID(known["id"]), status="running"),
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

        bind_workspace_registry(WorkspaceRegistry())
        register_workspace_provider(_MinimalWorkspaceProvider())

        exec_id = await eng.start(
            workflow_name="gw-terminal-test",
            ticket_id=str(uuid4()),
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

        wfx = await get_execution_summary(UUID(exec_id), session=db_session)
        assert wfx is not None
        assert wfx.state == WorkflowState.AWAITING_AGENT.value
        cmd_id = wfx.pending_agent_command_id
        assert cmd_id is not None

        # Seed the workspace row pointing at this execution so record_agent_event
        # can look up the workspace by command_id.
        seeded_ws_id = await _seed_workspace_for_tests(
            org_id=uuid4(),
            provider_id="remote_agent",
            plugin_state={},
            sha="deadbeef",
            current_command_id=cmd_id,
            current_holder_workflow_id=UUID(exec_id),
            caller_session=db_session,
        )

        event = AgentEvent(
            command_id=cmd_id,
            kind=AgentEventKind.COMPLETED_SUCCESS,
            outcome_label="success",
            outputs={"workspace_id": seeded_ws_id},
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

        wfx = await get_execution_summary(UUID(exec_id), session=db_session)
        assert wfx is not None
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

        bind_workspace_registry(WorkspaceRegistry())
        register_workspace_provider(_MinimalWorkspaceProvider())

        exec_id = await eng.start(
            workflow_name="gw-progress-test",
            ticket_id=str(uuid4()),
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

        wfx = await get_execution_summary(UUID(exec_id), session=db_session)
        assert wfx is not None
        assert wfx.state == WorkflowState.AWAITING_AGENT.value
        cmd_id = wfx.pending_agent_command_id
        assert cmd_id is not None

        ws_org_id = uuid4()
        await _seed_workspace_for_tests(
            org_id=ws_org_id,
            provider_id="remote_agent",
            plugin_state={},
            sha="deadbeef",
            current_command_id=cmd_id,
            current_holder_workflow_id=UUID(exec_id),
            caller_session=db_session,
        )

        # Post a PROGRESS event — workflow must stay in AWAITING_AGENT.
        # Progress events call require_org_context(), so wrap in org_context.
        from app.core.audit_log import ActorKind  # noqa: PLC0415
        from app.core.auth import org_context  # noqa: PLC0415

        event = AgentEvent(
            command_id=cmd_id,
            kind=AgentEventKind.PROGRESS,
            reported_at=datetime.now(UTC),
            traceparent="00-aabbccdd-1122-01",
        )
        async with org_context(ws_org_id, ActorKind.WORKSPACE):
            await record_agent_event(event, session=db_session)
        await db_session.commit()

        # Drain anything that was enqueued (should be nothing for a progress event).
        for _ in range(5):
            n = await drain_once(db_session, dispatcher=_dispatcher)
            await db_session.commit()
            if n == 0:
                break

        wfx = await get_execution_summary(UUID(exec_id), session=db_session)
        assert wfx is not None
        assert wfx.state == WorkflowState.AWAITING_AGENT.value, "progress event must not advance the workflow"


@pytest.mark.asyncio
async def test_progress_event_publishes_to_sse(db_session, redis_or_skip) -> None:
    """Progress AgentEvents posted via HTTP get republished to the org-scoped
    workspace-activity channel so the SPA's live-tail picks them up."""
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415
    from app.core.redis import RedisPubsub, bind_pubsub  # noqa: PLC0415
    from app.core.redis import shutdown as redis_shutdown  # noqa: PLC0415
    from app.core.sse import subscribe_workspace_activity  # noqa: PLC0415

    await redis_shutdown()
    # redis_shutdown() clears the ContextVar binding; restore it so
    # subscribe_workspace_activity (which calls get_pubsub()) does not raise.
    bind_pubsub(RedisPubsub())

    ws = await _seed_workspace(db_session)
    cmd_id = ws["command_id"]
    wfx_id = ws["workflow_id"]
    org_id = ws["org_id"]

    # Open an SSE subscriber on the org-scoped channel BEFORE posting the event.
    sub = subscribe_workspace_activity(org_id, wfx_id)
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
    async with org_context(org_id, ActorKind.WORKSPACE):
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
    from app.core.workspace import get_workspace_info, update_workspace_status  # noqa: PLC0415

    ws = await _seed_workspace(db_session)
    cmd_id = ws["command_id"]
    ws_id = UUID(ws["id"])

    # Demote status to creating so we can observe the transition.
    await update_workspace_status(ws_id, "creating", db_session)
    await db_session.flush()

    event = WorkspaceEvent(
        workspace_id=ws_id,
        command_id=cmd_id,
        kind=WorkspaceEventKind.READY,
        reported_at=datetime.now(UTC),
    )
    await record_workspace_event(event, session=db_session)
    await db_session.flush()
    info = await get_workspace_info(ws_id)
    assert info.status.value == "active"


@pytest.mark.asyncio
async def test_workspace_event_with_stale_command_raises(db_session) -> None:
    ws = await _seed_workspace(db_session)
    event = WorkspaceEvent(
        workspace_id=UUID(ws["id"]),
        command_id=uuid4(),  # mismatched
        kind=WorkspaceEventKind.READY,
        reported_at=datetime.now(UTC),
    )
    with pytest.raises(StaleClaimError):
        await record_workspace_event(event, session=db_session)


# ── has_any_reachable_agent ────────────────────────────────────────────


def _make_agent_row(org_id, *, state: str = "reachable", seconds_ago: int = 10):
    """Build a WorkspaceAgentRow for seeding. Returns the unsaved row."""
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    return WorkspaceAgentRow(
        org_id=org_id,
        instance_id=f"test-task-{uuid4().hex[:8]}",
        iam_arn="arn:aws:iam::123456789012:role/test-role",
        version="0.1.0",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=seconds_ago),
        state=state,
    )


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
