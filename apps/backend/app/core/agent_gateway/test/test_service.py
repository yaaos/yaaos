"""Service-level coverage for `core/agent_gateway` — heartbeat reconciliation,
terminal event routing, stale-claim guard, and agent liveness helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid7

import pytest
from sqlalchemy import select

from app.core.agent_gateway import (
    AgentEvent,
    AgentEventKind,
    AuthBlock,
    HeartbeatRequest,
    HeartbeatWorkspaceEntry,
    ProvisionWorkspaceCommand,
    RepoRef,
    StaleClaimError,
    WorkspaceEvent,
    WorkspaceEventKind,
    has_any_reachable_agent,
    record_agent_event,
    record_heartbeat,
    record_workspace_event,
)
from app.core.audit_log import Actor
from app.core.tasks import drain_once
from app.testing.e2e_setup import seed_workspace as _seed_workspace_for_tests


def _make_provision_command() -> ProvisionWorkspaceCommand:
    return ProvisionWorkspaceCommand(
        command_id=uuid7(),
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
    from app.core.agent_gateway import enqueue_command  # noqa: PLC0415
    from app.testing.e2e_setup import seed_agent  # noqa: PLC0415

    cmd_id = uuid7()
    run_id = uuid4()
    org_id = uuid4()
    agent = await seed_agent(org_id=org_id)
    ws_id = await _seed_workspace_for_tests(
        org_id=org_id,
        provider_id="remote_agent",
        sha="deadbeef",
        current_command_id=cmd_id if claimed_by_command else None,
        agent_id=agent["id"],
    )
    if claimed_by_command:
        # Persist the matching `agent_commands` row pre-stamped with the
        # run_id. `record_agent_event` resolves correlation
        # purely from this row — no workspace-row lookup is involved.
        provision = ProvisionWorkspaceCommand(
            command_id=cmd_id,
            workspace_id=UUID(ws_id),
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
        await enqueue_command(
            org_id=org_id,
            command=provision,
            session=db_session,
            run_id=run_id,
        )
    return {"id": ws_id, "org_id": org_id, "command_id": cmd_id, "run_id": run_id}


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


async def _pending_command_for_org(db_session, org_id: UUID) -> UUID:
    """Read the single `pending` AgentCommand row for `org_id`.

    Intra-module read of `core/agent_gateway`'s own `agent_commands` table —
    the test asserts on this module's own durable state, never on
    `domain/pipelines`' internal run/stage rows."""
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415

    return (
        await db_session.execute(
            select(AgentCommandRow.id).where(
                AgentCommandRow.org_id == org_id, AgentCommandRow.status == "pending"
            )
        )
    ).scalar_one()


async def _start_parked_pipeline_run(db_session) -> tuple[UUID, UUID]:
    """Start a real one-skill-stage pipeline run and drain it to the point
    where it's parked on its `provision-workspace` system stage — returns
    `(org_id, pending_agent_command_id)`. Shared setup for the two
    `record_agent_event` consumer-registry tests below."""
    from app.core.auth import Role  # noqa: PLC0415
    from app.core.identity import create_user  # noqa: PLC0415
    from app.core.tenancy import create_membership, create_org  # noqa: PLC0415
    from app.core.workspace import (  # noqa: PLC0415
        is_workspace_provider_registered,
        register_workspace_providers,
    )
    from app.domain.pipelines import (  # noqa: PLC0415
        BoundaryControl,
        Kickoff,
        PipelineDefinition,
        SkillStage,
        create_pipeline,
        start_run,
    )
    from app.domain.tickets import create_from_pr  # noqa: PLC0415
    from app.testing.stub_vcs import register_stub_vcs  # noqa: PLC0415

    if not is_workspace_provider_registered("remote_agent"):
        register_workspace_providers()

    org = await create_org(db_session, slug=f"gw-test-{uuid4().hex[:8]}", display_name="GW Test Org")
    user = await create_user(db_session, display_name="GW Test User")
    await create_membership(
        db_session, user_id=user.id, org_id=org.org_id, role=Role.BUILDER, handle="gwtest"
    )
    ticket_id, _ = await create_from_pr(
        org_id=org.org_id,
        source_external_id=f"ext-{uuid4().hex[:8]}",
        title="gw test ticket",
        description=None,
        repo_external_id="acme/repo",
        plugin_id="github",
        idempotency_key=f"key-{uuid4().hex}",
        payload={},
        session=db_session,
    )
    await db_session.flush()

    with register_stub_vcs(plugin_id="github"):
        pipeline_id = await create_pipeline(
            org_id=org.org_id,
            definition=PipelineDefinition(
                name=f"gw-pipe-{uuid4().hex[:8]}",
                stages=(
                    SkillStage(
                        name="write-spec",
                        skill_name="write-spec",
                        coding_agent_plugin_id="claude_code",
                        model="sonnet",
                        effort="medium",
                        boundary=BoundaryControl(),
                    ),
                ),
            ),
            actor=Actor.system(),
            session=db_session,
        )
        await db_session.flush()

        kickoff = Kickoff(intake_point_id="test", actor=Actor.user(user_id=user.id), input_text="go")
        await start_run(
            org_id=org.org_id,
            ticket_id=ticket_id,
            pipeline_id=pipeline_id,
            kickoff=kickoff,
            session=db_session,
        )
        await db_session.commit()
        await _drain_pipeline_outbox(db_session)

    command_id = await _pending_command_for_org(db_session, org.org_id)
    return org.org_id, command_id


async def _drain_pipeline_outbox(db_session, *, max_iters: int = 20) -> None:
    from app.core.tasks import get_broker, get_pending_task_names  # noqa: PLC0415

    async def _dispatcher(kind: str, payload: dict) -> None:
        assert kind == "taskiq_enqueue"
        decorated = get_broker().find_task(payload["task_name"])
        assert decorated is not None
        await decorated.original_func(**payload["args"])

    for _ in range(max_iters):
        pending = await get_pending_task_names(db_session)
        if not pending:
            return
        delivered = await drain_once(db_session, dispatcher=_dispatcher)
        await db_session.commit()
        if delivered == 0:
            return


@pytest.mark.asyncio
@pytest.mark.usefixtures("redis_or_skip")
async def test_terminal_event_advances_pipeline_run(db_session) -> None:
    """A terminal AgentEvent for the parked `provision-workspace` command
    causes the registered `domain/pipelines` consumer to resume the run:
    `record_agent_event` enqueues `handle_agent_event`, and draining that
    task advances the run past the provision phase — observable here as
    the original command retiring to `done` (the run's own next-stage
    dispatch is `domain/pipelines`' concern, exercised in its own tests)."""
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    org_id, command_id = await _start_parked_pipeline_run(db_session)

    event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="00-aabbccdd-1122-01",
    )
    async with org_context(org_id, ActorKind.WORKSPACE, actor_id=None):
        await record_agent_event(event, session=db_session)
    await db_session.commit()
    await _drain_pipeline_outbox(db_session)

    original = await db_session.get(AgentCommandRow, command_id)
    assert original is not None
    assert original.status == "done", "the provision command must retire once the run resumes"


@pytest.mark.asyncio
@pytest.mark.usefixtures("redis_or_skip")
async def test_progress_event_does_not_advance_pipeline_run(db_session) -> None:
    """A PROGRESS AgentEvent does not advance the run — it stays parked on
    the same pending command after the event is processed."""
    from app.core.agent_gateway.models import AgentCommandRow  # noqa: PLC0415
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    org_id, command_id = await _start_parked_pipeline_run(db_session)

    event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.PROGRESS,
        reported_at=datetime.now(UTC),
        traceparent="00-aabbccdd-1122-01",
    )
    async with org_context(org_id, ActorKind.WORKSPACE, actor_id=None):
        await record_agent_event(event, session=db_session)
    await db_session.commit()
    await _drain_pipeline_outbox(db_session)

    original = await db_session.get(AgentCommandRow, command_id)
    assert original is not None
    assert original.status == "pending", "progress event must not retire the command"

    still_pending_id = await _pending_command_for_org(db_session, org_id)
    assert still_pending_id == command_id, "progress event must not advance the run"


@pytest.mark.asyncio
async def test_progress_event_does_not_publish_without_correlated_run(db_session, redis_or_skip) -> None:
    """Progress AgentEvents with no correlated coding_agent_runs row do NOT
    publish anything to the workspace-activity SSE channel.

    The live-tail is gated on a correlated run: the run sink looks up
    `coding_agent_runs.command_id` to resolve plugin + run_id before delegating
    to `parse_activity_line`. With no row, publish is skipped entirely — there
    is no run_id to scope the channel publish.
    """
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415
    from app.core.sse import subscribe_workspace_activity  # noqa: PLC0415

    ws = await _seed_workspace(db_session)
    cmd_id = ws["command_id"]
    run_id = ws["run_id"]
    org_id = ws["org_id"]

    # Subscribe to the channel. A publish would arrive here.
    sub = subscribe_workspace_activity(org_id, run_id)
    received: list[dict] = []

    async def _drain() -> None:
        async for evt in sub:
            received.append(evt)

    drainer = asyncio.create_task(_drain())
    await asyncio.sleep(0)  # let subscriber register

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

    # Wait a moment — if anything was published it would arrive quickly.
    await asyncio.sleep(0.3)
    drainer.cancel()
    try:
        await drainer
    except asyncio.CancelledError:
        pass

    assert received == [], (
        f"progress event for a command with no correlated run should not publish to SSE; got: {received}"
    )


@pytest.mark.asyncio
async def test_stale_command_id_raises(db_session) -> None:
    """An event whose command_id doesn't match any workspace's current
    claim raises StaleClaimError so the endpoint can map to 410."""
    event = AgentEvent(
        command_id=uuid7(),  # nothing holds this
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

    # Demote status so we can observe the ready→active transition.
    await update_workspace_status(ws_id, "expired", db_session)
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
        command_id=uuid7(),  # mismatched
        kind=WorkspaceEventKind.READY,
        reported_at=datetime.now(UTC),
    )
    with pytest.raises(StaleClaimError):
        await record_workspace_event(event, session=db_session)


# ── has_any_reachable_agent ────────────────────────────────────────────


def _make_agent_row(
    org_id,
    *,
    state: str = "reachable",
    seconds_ago: int = 10,
    lifecycle: str = "active",
):
    """Build a WorkspaceAgentRow for seeding. Returns the unsaved row."""
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    return WorkspaceAgentRow(
        org_id=org_id,
        instance_id=f"test-task-{uuid4().hex[:8]}",
        iam_arn="arn:aws:iam::123456789012:role/test-role",
        version="0.1.0",
        last_heartbeat_at=datetime.now(UTC) - timedelta(seconds=seconds_ago),
        state=state,
        lifecycle=lifecycle,
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


@pytest.mark.asyncio
async def test_has_any_reachable_agent_false_when_lifecycle_shutdown(db_session) -> None:
    # `mark_agent_shutdown_complete` pins state='offline' on graceful drain, so
    # production rows never reach this combo.  We seed it directly to verify
    # the belt-and-suspenders lifecycle filter excludes the row even if state
    # disagreed mid-write.
    org_id = uuid4()
    shutdown_row = _make_agent_row(org_id, state="reachable", seconds_ago=5, lifecycle="shutdown")
    db_session.add(shutdown_row)
    await db_session.flush()

    assert await has_any_reachable_agent(session=db_session) is False

    # A normal reachable+active agent still counts.
    active_row = _make_agent_row(org_id, state="reachable", seconds_ago=5, lifecycle="active")
    db_session.add(active_row)
    await db_session.flush()

    assert await has_any_reachable_agent(session=db_session) is True
