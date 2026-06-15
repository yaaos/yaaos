"""Service tests for the lean workspace lifecycle + guaranteed finalizer + failure recording.

Covers:
- Workspace row created on the agent's first `created`/`ready` event with
  `owning_agent_id` from the reporting bearer and `org_id`/`spec` from the
  originating `agent_commands` row.
- `release_claim` runs before the next `try_claim` (failure-report-precedes-disposal).
- `Workflow.finalizer_step_id` runs exactly once on terminal-fail before
  the execution is marked `failed`.
- The success path does NOT trigger a double-run of the finalizer step.
- `failure_reason` label + `workflow.failed` audit row written on terminal-fail.
- `release_claim` precedes the finalizer dispatch on the failure path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid7

import pytest
from sqlalchemy import select

from app.core.agent_gateway import (
    AgentEvent,
    AgentEventKind,
    AuthBlock,
    CleanupWorkspaceCommand,
    ProvisionWorkspaceCommand,
    RepoRef,
    StaleClaimError,
    WorkspaceEvent,
    enqueue_command,
    record_agent_event,
    record_workspace_event,
)
from app.core.audit_log import ActorKind, list_for_entity
from app.core.auth import org_context
from app.core.tasks import drain_once, get_pending_task_names
from app.core.workflow import (
    CommandCategory,
    Outcome,
    Step,
    TerminalAction,
    Workflow,
    WorkflowState,
    get_execution_summary,
)
from app.core.workspace.models import WorkspaceRow
from app.core.workspace.types import WorkspaceStatus
from app.testing.seed import seed_agent
from app.testing.workflow_harness import scoped_engine

pytestmark = pytest.mark.service


# ── Helpers ─────────────────────────────────────────────────────────────


async def _drain(db_session, *, max_iters: int = 50) -> None:
    """Drain the outbox by dispatching `taskiq_enqueue` rows via the broker."""
    from app.core.tasks import get_broker  # noqa: PLC0415

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


class _DispatchingWs:
    """Workspace command that enqueues a real `agent_commands` row and
    records the returned command_id for inspection by the test."""

    kind = "LeanLifecycleDispatch"
    category = CommandCategory.WORKSPACE
    restart_safe = True

    def __init__(self, *, org_id: UUID, workspace_id: UUID) -> None:
        self._org_id = org_id
        self._workspace_id = workspace_id
        self.dispatched_command_id: UUID | None = None

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()

    async def dispatch(self, inputs, ctx, *, session):  # type: ignore[no-untyped-def]
        del inputs
        command_id = uuid7()
        cmd = CleanupWorkspaceCommand(
            command_id=command_id,
            workspace_id=self._workspace_id,
            traceparent=ctx.traceparent or "",
        )
        await enqueue_command(
            org_id=self._org_id,
            command=cmd,
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
        )
        self.dispatched_command_id = command_id
        return command_id


class _FailingWs:
    """Workspace command that always signals failure via terminal event."""

    kind = "LeanLifecycleFail"
    category = CommandCategory.WORKSPACE
    restart_safe = True

    def __init__(self, *, org_id: UUID, workspace_id: UUID) -> None:
        self._org_id = org_id
        self._workspace_id = workspace_id
        self.dispatched_command_id: UUID | None = None

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()

    async def dispatch(self, inputs, ctx, *, session):  # type: ignore[no-untyped-def]
        del inputs
        command_id = uuid7()
        cmd = CleanupWorkspaceCommand(
            command_id=command_id,
            workspace_id=self._workspace_id,
            traceparent=ctx.traceparent or "",
        )
        await enqueue_command(
            org_id=self._org_id,
            command=cmd,
            session=session,
            workflow_execution_id=UUID(ctx.workflow_execution_id),
        )
        self.dispatched_command_id = command_id
        return command_id


class _NoopLocal:
    """Local terminal step — drains the workflow to DONE."""

    kind = "LeanLifecycleTerminal"
    category = CommandCategory.LOCAL
    restart_safe = True

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        return Outcome.success()


class _FinalizerLocal:
    """Local finalizer step — records that it was called."""

    kind = "LeanLifecycleFinalizer"
    category = CommandCategory.LOCAL
    restart_safe = True

    call_count: int = 0

    async def execute(self, inputs, ctx):  # type: ignore[no-untyped-def]
        del inputs, ctx
        _FinalizerLocal.call_count += 1
        return Outcome.success()


# ── Lean row creation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lean_row_created_on_first_workspace_event(db_session) -> None:
    """The `workspaces` row is created on the first `created`/`ready` event
    with `owning_agent_id` from the bearer and `org_id`/`spec` from the
    originating `agent_commands` row — no pre-created row needed."""
    org_id = uuid4()
    agent_result = await seed_agent(org_id=org_id, session=db_session)
    agent_id = UUID(str(agent_result["id"]))
    await db_session.flush()

    workspace_id = uuid7()
    command_id = uuid7()

    # Enqueue a ProvisionWorkspace command so there's a real agent_commands row.
    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent="",
    )
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # No workspace row yet.
    assert (await db_session.get(WorkspaceRow, workspace_id)) is None

    # Fire the first workspace event (kind=`created`) with the agent's id.
    event = WorkspaceEvent(
        workspace_id=workspace_id,
        command_id=command_id,
        kind="created",
        reported_at=datetime.now(UTC),
    )
    await record_workspace_event(event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    # The lean row should now exist.
    ws = await db_session.get(WorkspaceRow, workspace_id)
    assert ws is not None, "lean workspace row should be created on first event"
    assert ws.status == WorkspaceStatus.ACTIVE.value
    assert ws.owning_agent_id == agent_id
    assert ws.org_id == org_id


@pytest.mark.asyncio
async def test_lean_row_not_created_for_unknown_kind(db_session) -> None:
    """Non-`created`/`ready` kinds when no row exists → sink returns accepted=False.
    `record_workspace_event` raises StaleClaimError in that case; no row is inserted."""
    from app.core.agent_gateway import StaleClaimError  # noqa: PLC0415

    org_id = uuid4()
    workspace_id = uuid7()
    command_id = uuid7()

    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent="",
    )
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    event = WorkspaceEvent(
        workspace_id=workspace_id,
        command_id=command_id,
        kind="destroyed",  # not in _ROW_CREATE_KINDS
        reported_at=datetime.now(UTC),
    )
    # Sending a terminal-status event to a non-existent workspace raises StaleClaimError.
    with pytest.raises(StaleClaimError):
        await record_workspace_event(event, agent_id=None, session=db_session)

    # No lean row was created.
    assert (await db_session.get(WorkspaceRow, workspace_id)) is None


@pytest.mark.asyncio
async def test_lean_row_org_id_from_command_row(db_session) -> None:
    """The lean row's `org_id` must match the `agent_commands` row's `org_id`,
    not the agent's `org_id` (which matches here but the test checks the exact join)."""
    org_id = uuid4()
    agent_result = await seed_agent(org_id=org_id, session=db_session)
    agent_id = UUID(str(agent_result["id"]))
    await db_session.flush()

    workspace_id = uuid7()
    command_id = uuid7()
    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent="",
    )
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    event = WorkspaceEvent(
        workspace_id=workspace_id,
        command_id=command_id,
        kind="ready",
        reported_at=datetime.now(UTC),
    )
    await record_workspace_event(event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    ws = await db_session.get(WorkspaceRow, workspace_id)
    assert ws is not None
    assert ws.org_id == org_id


# ── ProvisionWorkspace success → lean row materialisation ────────────────


def _make_provision_command(
    *, workspace_id: UUID, command_id: UUID, ttl_seconds: int = 900
) -> ProvisionWorkspaceCommand:
    return ProvisionWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent="",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="super-secret-installation-token"),
        ttl_seconds=ttl_seconds,
        max_idle_seconds=ttl_seconds,
    )


@pytest.mark.asyncio
async def test_provision_success_completion_token_verified(db_session) -> None:
    """A command claimed via `claim_next` mints a completion token. A terminal
    `completed_success` echoing the correct token materialises the lean row; a
    terminal event with a wrong/empty token raises StaleClaimError and creates
    no workspace row."""
    from app.core.agent_gateway import claim_next  # noqa: PLC0415

    org_id = uuid4()
    agent_result = await seed_agent(org_id=org_id, session=db_session)
    agent_id = UUID(str(agent_result["id"]))

    workspace_id = uuid7()
    command_id = uuid7()
    cmd = _make_provision_command(workspace_id=workspace_id, command_id=command_id)
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # Claim the command as the agent — this mints the completion token and
    # returns it on the command DTO.
    claimed = await claim_next(
        agent_id,
        lifecycle="configured",
        new_workspaces=1,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert claimed is not None
    assert claimed.command_id == command_id
    token = claimed.completion_token
    assert token, "claim_next must inject the raw completion token on the DTO"

    # A terminal event with a WRONG token is rejected and creates no row.
    bad_event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
        completion_token="not-the-real-token",
    )
    with pytest.raises(StaleClaimError):
        async with org_context(org_id, ActorKind.WORKSPACE):
            await record_agent_event(bad_event, agent_id=agent_id, session=db_session)
    assert (await db_session.get(WorkspaceRow, workspace_id)) is None

    # An empty token is also rejected.
    empty_event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
        completion_token=None,
    )
    with pytest.raises(StaleClaimError):
        async with org_context(org_id, ActorKind.WORKSPACE):
            await record_agent_event(empty_event, agent_id=agent_id, session=db_session)
    assert (await db_session.get(WorkspaceRow, workspace_id)) is None

    # The correct token succeeds and materialises the lean row.
    good_event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
        completion_token=token,
    )
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(good_event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    ws = await db_session.get(WorkspaceRow, workspace_id)
    assert ws is not None, "correct token should materialise the lean row"
    assert ws.status == WorkspaceStatus.ACTIVE.value
    assert ws.owning_agent_id == agent_id


@pytest.mark.asyncio
async def test_provision_success_materialises_lean_row(db_session) -> None:
    """The happy path materialises the lean row via the sink: status active,
    org/agent from the command + bearer, TTL from the payload, provider id from
    the registered provider, and a spec that carries the SHA but no token."""
    from datetime import timedelta  # noqa: PLC0415

    org_id = uuid4()
    agent_result = await seed_agent(org_id=org_id, session=db_session)
    agent_id = UUID(str(agent_result["id"]))

    workspace_id = uuid7()
    command_id = uuid7()
    ttl_seconds = 900
    cmd = _make_provision_command(workspace_id=workspace_id, command_id=command_id, ttl_seconds=ttl_seconds)
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    assert (await db_session.get(WorkspaceRow, workspace_id)) is None

    event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
    )
    before = datetime.now(UTC)
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    ws = await db_session.get(WorkspaceRow, workspace_id)
    assert ws is not None, "lean row should be materialised on ProvisionWorkspace success"
    assert ws.status == WorkspaceStatus.ACTIVE.value
    assert ws.org_id == org_id
    assert ws.owning_agent_id == agent_id
    assert ws.max_idle_seconds == ttl_seconds
    # expires_at derived from the payload TTL, not the default.
    assert ws.expires_at >= before + timedelta(seconds=ttl_seconds - 5)
    # provider id resolved via the registry (falls back to the single shipped
    # provider id when no provider is bound in this service-test context).
    assert ws.provider_id == "remote_agent"
    # spec carries the SHA only — never the installation token.
    assert ws.spec == {"sha": "deadbeef"}
    assert "auth" not in ws.spec
    assert "token" not in str(ws.spec)


@pytest.mark.asyncio
async def test_provision_success_idempotent(db_session) -> None:
    """Replaying the terminal `completed_success` does not insert a duplicate
    workspace row."""
    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    org_id = uuid4()
    agent_result = await seed_agent(org_id=org_id, session=db_session)
    agent_id = UUID(str(agent_result["id"]))

    workspace_id = uuid7()
    command_id = uuid7()
    cmd = _make_provision_command(workspace_id=workspace_id, command_id=command_id)
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
    )
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    # Replay the same terminal event. The command row is retired (status=done)
    # but still present, so the guard re-passes; the sink sees the existing
    # workspace row and skips the insert — no duplicate, no error.
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, agent_id=agent_id, session=db_session)
    await db_session.flush()

    rows = (await db_session.execute(select(WorkspaceRow.id).where(WorkspaceRow.id == workspace_id))).all()
    assert len(rows) == 1


# ── release_claim timing ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_release_claim_before_next_try_claim(db_session) -> None:
    """After a terminal agent event, `current_command_id` is cleared before
    the workflow engine is resumed — so the next `try_claim` sees NULL."""
    from app.core.workspace.dispatch import try_claim  # noqa: PLC0415

    org_id = uuid4()
    workspace_id = uuid7()
    command_id = uuid7()

    # Seed a workspace row that holds the current command claim.
    from datetime import timedelta  # noqa: PLC0415

    agent_result = await seed_agent(org_id=org_id, session=db_session)
    ws = WorkspaceRow(
        id=workspace_id,
        org_id=org_id,
        provider_id="remote_agent",
        spec={"sha": "abc"},
        status=WorkspaceStatus.ACTIVE.value,
        current_command_id=command_id,
        activated_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=600),
        max_idle_seconds=600,
        owning_agent_id=agent_result["id"],
    )
    db_session.add(ws)
    await db_session.flush()

    # Enqueue an agent_commands row so record_agent_event finds it.
    cmd = CleanupWorkspaceCommand(
        command_id=command_id,
        workspace_id=workspace_id,
        traceparent="",
    )
    await enqueue_command(org_id=org_id, command=cmd, session=db_session)
    await db_session.flush()

    # Simulate a terminal agent event — this should call release_claim before
    # routing to the workflow engine.
    event = AgentEvent(
        command_id=command_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outcome_label="success",
        outputs={},
        reported_at=datetime.now(UTC),
        traceparent="",
    )
    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, session=db_session)
    await db_session.flush()

    # After the terminal event, current_command_id must be None.
    await db_session.refresh(ws)
    assert ws.current_command_id is None, "release_claim must clear current_command_id before routing"

    # A subsequent try_claim should now succeed (claim is released).
    new_cmd_id = uuid7()
    claimed = await try_claim(
        workspace_id=workspace_id,
        command_id=new_cmd_id,
        workflow_execution_id=uuid4(),
        session=db_session,
    )
    assert claimed, "try_claim should succeed after release_claim"


# ── Finalizer: fires exactly once on terminal-fail ───────────────────────


@pytest.mark.asyncio
async def test_finalizer_fires_once_on_terminal_fail(db_session) -> None:
    """When `Workflow.finalizer_step_id` is set and a terminal-fail occurs,
    the engine routes to the finalizer exactly once before marking the
    workflow `failed`."""
    _FinalizerLocal.call_count = 0

    org_id = uuid4()
    workspace_id = uuid7()
    fail_cmd = _FailingWs(org_id=org_id, workspace_id=workspace_id)
    local_cmd = _NoopLocal()
    finalizer_cmd = _FinalizerLocal()

    workflow = Workflow(
        name="finalizer-once-test",
        version=1,
        steps=(
            Step(
                id="main",
                command_kind="LeanLifecycleFail",
                transitions={
                    "success": TerminalAction.COMPLETE_WORKFLOW,
                    "failure": TerminalAction.FAIL_WORKFLOW,
                },
            ),
            Step(
                id="cleanup",
                command_kind="LeanLifecycleFinalizer",
                transitions={"success": TerminalAction.FAIL_WORKFLOW},
            ),
        ),
        entry_step_id="main",
        finalizer_step_id="cleanup",
    )

    with scoped_engine() as eng:
        eng.register_command(fail_cmd)
        eng.register_command(local_cmd)
        eng.register_command(finalizer_cmd)
        eng.register_workflow(workflow)
        wfx_id = await eng.start(
            workflow_name="finalizer-once-test",
            ticket_id=str(uuid4()),
            session=db_session,
        )
        await db_session.commit()

        # Drain start_step → AWAITING_AGENT.
        await _drain(db_session)

        wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
        assert wfx is not None
        assert wfx.state == WorkflowState.AWAITING_AGENT.value

        # Send a failure event for the main step.
        assert fail_cmd.dispatched_command_id is not None
        fail_event = AgentEvent(
            command_id=fail_cmd.dispatched_command_id,
            kind=AgentEventKind.COMPLETED_FAILURE,
            outcome_label="failure",
            outputs={"__failure_reason__": "provision_failed"},
            reported_at=datetime.now(UTC),
            traceparent="",
        )
        async with org_context(org_id, ActorKind.WORKSPACE):
            await record_agent_event(fail_event, session=db_session)
        await db_session.commit()

        # Drain: handle_agent_event → route_workflow (fires finalizer) →
        # start_step(cleanup) → route_workflow (records failed).
        await _drain(db_session)

        wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
        assert wfx is not None
        assert wfx.state == WorkflowState.FAILED.value
        # The finalizer step ran exactly once.
        assert _FinalizerLocal.call_count == 1


@pytest.mark.asyncio
async def test_finalizer_does_not_refire_on_success(db_session) -> None:
    """On the success path, the finalizer step runs as the normal terminal step
    and does NOT re-fire — the engine only invokes it on terminal-fail."""
    _FinalizerLocal.call_count = 0

    org_id = uuid4()
    workspace_id = uuid7()
    # Use a Workspace command that succeeds.
    ws_cmd = _DispatchingWs(org_id=org_id, workspace_id=workspace_id)
    finalizer_cmd = _FinalizerLocal()

    # finalizer_step_id is "cleanup" — but the success transition goes to
    # TerminalAction.COMPLETE_WORKFLOW, bypassing the finalizer path.
    workflow = Workflow(
        name="finalizer-no-refire-test",
        version=1,
        steps=(
            Step(
                id="main",
                command_kind="LeanLifecycleDispatch",
                transitions={
                    "success": TerminalAction.COMPLETE_WORKFLOW,
                    "failure": TerminalAction.FAIL_WORKFLOW,
                },
            ),
            Step(
                id="cleanup",
                command_kind="LeanLifecycleFinalizer",
                transitions={"success": TerminalAction.FAIL_WORKFLOW},
            ),
        ),
        entry_step_id="main",
        finalizer_step_id="cleanup",
    )

    with scoped_engine() as eng:
        eng.register_command(ws_cmd)
        eng.register_command(finalizer_cmd)
        eng.register_workflow(workflow)
        wfx_id = await eng.start(
            workflow_name="finalizer-no-refire-test",
            ticket_id=str(uuid4()),
            session=db_session,
        )
        await db_session.commit()

        # Drain start_step → AWAITING_AGENT.
        await _drain(db_session)

        wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
        assert wfx is not None
        assert wfx.state == WorkflowState.AWAITING_AGENT.value

        # Success event.
        assert ws_cmd.dispatched_command_id is not None
        success_event = AgentEvent(
            command_id=ws_cmd.dispatched_command_id,
            kind=AgentEventKind.COMPLETED_SUCCESS,
            outcome_label="success",
            outputs={},
            reported_at=datetime.now(UTC),
            traceparent="",
        )
        async with org_context(org_id, ActorKind.WORKSPACE):
            await record_agent_event(success_event, session=db_session)
        await db_session.commit()

        await _drain(db_session)

        wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
        assert wfx is not None
        assert wfx.state == WorkflowState.DONE.value
        # Finalizer did NOT run — success path doesn't trigger it.
        assert _FinalizerLocal.call_count == 0


# ── failure_reason + workflow.failed audit ────────────────────────────────


@pytest.mark.asyncio
async def test_failure_reason_and_audit_written_on_terminal_fail(db_session) -> None:
    """On terminal failure, `workflow_executions.failure_reason` is set and a
    `workflow.failed` audit row is written with the correct payload."""
    org_id = uuid4()
    workspace_id = uuid7()
    fail_cmd = _FailingWs(org_id=org_id, workspace_id=workspace_id)

    workflow = Workflow(
        name="failure-record-test",
        version=1,
        steps=(
            Step(
                id="main",
                command_kind="LeanLifecycleFail",
                transitions={
                    "success": TerminalAction.COMPLETE_WORKFLOW,
                    "failure": TerminalAction.FAIL_WORKFLOW,
                },
            ),
        ),
        entry_step_id="main",
    )

    ticket_id = str(uuid4())
    with scoped_engine() as eng:
        eng.register_command(fail_cmd)
        eng.register_command(_NoopLocal())
        eng.register_workflow(workflow)
        wfx_id = await eng.start(
            workflow_name="failure-record-test",
            ticket_id=ticket_id,
            session=db_session,
        )
        await db_session.commit()

        await _drain(db_session)

        wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
        assert wfx is not None
        assert wfx.state == WorkflowState.AWAITING_AGENT.value

        assert fail_cmd.dispatched_command_id is not None
        fail_event = AgentEvent(
            command_id=fail_cmd.dispatched_command_id,
            kind=AgentEventKind.COMPLETED_FAILURE,
            outcome_label="failure",
            outputs={"__failure_reason__": "provision_failed"},
            reported_at=datetime.now(UTC),
            traceparent="",
        )
        async with org_context(org_id, ActorKind.WORKSPACE):
            await record_agent_event(fail_event, session=db_session)
        await db_session.commit()

        await _drain(db_session)

        wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
        assert wfx is not None
        assert wfx.state == WorkflowState.FAILED.value
        assert wfx.failure_reason == "provision_failed"

        # Verify the workflow.failed audit row was written.
        # `_workflow_org_id` falls back to UUID(int=0) when there is no org context
        # (no OrgContextMiddleware in test task bodies). The audit row lands with that org_id.
        nil_org = UUID(int=0)
        log_entries = await list_for_entity(
            "workflow_execution",
            UUID(wfx_id),
            org_id=nil_org,
        )
        failed_entries = [e for e in log_entries if e.kind == "workflow.failed"]
        assert len(failed_entries) == 1
        payload = failed_entries[0].payload
        assert payload["workflow_execution_id"] == wfx_id
        assert payload["failure_reason"] == "provision_failed"


@pytest.mark.asyncio
async def test_failure_reason_without_structured_key_uses_label(db_session) -> None:
    """When the agent event has no `__failure_reason__` key, `failure_reason`
    falls back to the outcome label."""
    org_id = uuid4()
    workspace_id = uuid7()
    fail_cmd = _FailingWs(org_id=org_id, workspace_id=workspace_id)

    workflow = Workflow(
        name="failure-label-fallback-test",
        version=1,
        steps=(
            Step(
                id="main",
                command_kind="LeanLifecycleFail",
                transitions={
                    "success": TerminalAction.COMPLETE_WORKFLOW,
                    "failure": TerminalAction.FAIL_WORKFLOW,
                },
            ),
        ),
        entry_step_id="main",
    )

    with scoped_engine() as eng:
        eng.register_command(fail_cmd)
        eng.register_command(_NoopLocal())
        eng.register_workflow(workflow)
        wfx_id = await eng.start(
            workflow_name="failure-label-fallback-test",
            ticket_id=str(uuid4()),
            session=db_session,
        )
        await db_session.commit()

        await _drain(db_session)

        assert fail_cmd.dispatched_command_id is not None
        # No __failure_reason__ key in outputs.
        fail_event = AgentEvent(
            command_id=fail_cmd.dispatched_command_id,
            kind=AgentEventKind.COMPLETED_FAILURE,
            outcome_label="agent_failure",
            outputs={},
            reported_at=datetime.now(UTC),
            traceparent="",
        )
        async with org_context(org_id, ActorKind.WORKSPACE):
            await record_agent_event(fail_event, session=db_session)
        await db_session.commit()

        await _drain(db_session)

        wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
        assert wfx is not None
        assert wfx.state == WorkflowState.FAILED.value
        assert wfx.failure_reason == "agent_failure"


# ── Finalizer: pending failure context survives the round-trip ────────────


@pytest.mark.asyncio
async def test_finalizer_original_failure_reason_preserved(db_session) -> None:
    """The `failure_reason` on the `workflow_executions` row reflects the
    *original* failing step, not the finalizer step. Even when the finalizer
    itself succeeds, the failure context is stored before the finalizer runs."""
    _FinalizerLocal.call_count = 0

    org_id = uuid4()
    workspace_id = uuid7()
    fail_cmd = _FailingWs(org_id=org_id, workspace_id=workspace_id)
    finalizer_cmd = _FinalizerLocal()

    workflow = Workflow(
        name="finalizer-failure-context-test",
        version=1,
        steps=(
            Step(
                id="main",
                command_kind="LeanLifecycleFail",
                transitions={
                    "success": TerminalAction.COMPLETE_WORKFLOW,
                    "failure": TerminalAction.FAIL_WORKFLOW,
                },
            ),
            Step(
                id="cleanup",
                command_kind="LeanLifecycleFinalizer",
                transitions={"success": TerminalAction.FAIL_WORKFLOW},
            ),
        ),
        entry_step_id="main",
        finalizer_step_id="cleanup",
    )

    with scoped_engine() as eng:
        eng.register_command(fail_cmd)
        eng.register_command(finalizer_cmd)
        eng.register_workflow(workflow)
        wfx_id = await eng.start(
            workflow_name="finalizer-failure-context-test",
            ticket_id=str(uuid4()),
            session=db_session,
        )
        await db_session.commit()

        await _drain(db_session)

        assert fail_cmd.dispatched_command_id is not None
        fail_event = AgentEvent(
            command_id=fail_cmd.dispatched_command_id,
            kind=AgentEventKind.COMPLETED_FAILURE,
            outcome_label="failure",
            outputs={"__failure_reason__": "provision_failed"},
            reported_at=datetime.now(UTC),
            traceparent="",
        )
        async with org_context(org_id, ActorKind.WORKSPACE):
            await record_agent_event(fail_event, session=db_session)
        await db_session.commit()

        await _drain(db_session)

        wfx = await get_execution_summary(UUID(wfx_id), session=db_session)
        assert wfx is not None
        assert wfx.state == WorkflowState.FAILED.value
        # Must reflect the original failure reason, not the finalizer's outcome.
        assert wfx.failure_reason == "provision_failed"
        assert _FinalizerLocal.call_count == 1
