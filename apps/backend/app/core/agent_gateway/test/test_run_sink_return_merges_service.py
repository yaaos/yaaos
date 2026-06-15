"""Service test: run-sink return dict is merged into HANDLE_AGENT_EVENT outputs.

A stub AgentRunSink that returns `{"foo": "bar"}` is registered.  After
driving a terminal AgentEvent through `record_agent_event`, the enqueued
HANDLE_AGENT_EVENT task args must carry `outputs["foo"] == "bar"`.

When the sink returns a key that also exists in `event.outputs`, the sink's
value overrides — also covered here.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
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
    register_report_sink,
    register_run_sink,
)
from app.core.agent_gateway.report_sink import clear_report_sink
from app.core.agent_gateway.run_sink import clear_run_sink
from app.core.tasks import get_pending_outbox_payloads

# ── Stub sinks ─────────────────────────────────────────────────────────


class _StubRunSink:
    """AgentRunSink stub that returns a fixed extras dict on every call."""

    def __init__(self, extras: Mapping[str, Any] | None) -> None:
        self._extras = extras
        self.called: bool = False

    async def handle_terminal_event(
        self,
        command_id: UUID,
        command_kind: str,
        event_kind: str,
        outputs: dict,  # type: ignore[type-arg]
        session: object,
    ) -> Mapping[str, Any] | None:
        self.called = True
        return self._extras


class _NoopReportSink:
    """Minimal WorkspaceAgentReportSink stub — all methods are no-ops."""

    async def reconcile_heartbeat(self, reported_ids: set[UUID], session: object) -> set[UUID]:
        return set()

    async def apply_workspace_event(self, report: object, session: object) -> object:
        from app.core.agent_gateway.report_sink import WorkspaceEventOutcome  # noqa: PLC0415

        return WorkspaceEventOutcome(resolved_status=None, accepted=True)

    async def materialise_provision_success(
        self,
        *,
        command_id: UUID,
        agent_id: UUID,
        session: object,
    ) -> None:
        return None

    async def resolve_claim(self, command_id: UUID, session: object) -> UUID | None:
        return None

    async def owning_agent_for_workspace(self, workspace_id: UUID, session: object) -> UUID | None:
        return None

    async def owning_agent_for_command(self, command_id: UUID, session: object) -> UUID | None:
        return None

    async def handle_agent_loss(self, agent_ids: set[UUID], session: object) -> None:
        return None

    async def release_command_claim(self, command_id: UUID, session: object) -> None:
        return None


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_run_sink():
    """Swap in a clean run-sink slot; restore the prior one on teardown."""
    prior = None
    try:
        from app.core.agent_gateway.run_sink import get_run_sink as _get  # noqa: PLC0415

        prior = _get()
    except Exception:
        pass

    clear_run_sink()
    yield
    clear_run_sink()
    if prior is not None:
        register_run_sink(prior)


@pytest.fixture(autouse=True)
def _reset_report_sink():
    """Swap in the no-op report sink for this module's tests."""
    try:
        from app.core.agent_gateway import get_report_sink as _get  # noqa: PLC0415

        prior = _get()
    except RuntimeError:
        prior = None

    clear_report_sink()
    register_report_sink(_NoopReportSink())
    yield
    clear_report_sink()
    if prior is not None:
        register_report_sink(prior)


# ── Helpers ────────────────────────────────────────────────────────────


async def _seed_command(
    org_id: UUID,
    *,
    workflow_execution_id: UUID,
    session: object,
) -> UUID:
    """Enqueue a ProvisionWorkspaceCommand and return its command_id."""
    cmd_id = uuid7()
    command = ProvisionWorkspaceCommand(
        command_id=cmd_id,
        workspace_id=uuid4(),
        traceparent="",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/example/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="tok"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )
    from sqlalchemy.ext.asyncio import AsyncSession  # noqa: PLC0415

    assert isinstance(session, AsyncSession)
    await enqueue_command(
        org_id=org_id,
        command=command,
        session=session,
        workflow_execution_id=workflow_execution_id,
    )
    return cmd_id


# ── Tests ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_sink_return_dict_merged_into_task_args(db_session) -> None:
    """A sink returning {"foo": "bar"} causes HANDLE_AGENT_EVENT outputs to
    carry foo="bar" alongside the native event outputs."""
    org_id = uuid4()
    wfx_id = uuid4()

    run_sink = _StubRunSink(extras={"foo": "bar"})
    register_run_sink(run_sink)

    cmd_id = await _seed_command(org_id, workflow_execution_id=wfx_id, session=db_session)
    await db_session.commit()

    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outputs={"exit_code": 0},
        reported_at=datetime.now(UTC),
        traceparent="",
    )

    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, session=db_session)

    assert run_sink.called

    payloads = await get_pending_outbox_payloads(db_session)
    # The HANDLE_AGENT_EVENT enqueue and the seeding enqueue are both in
    # outbox — filter to HANDLE_AGENT_EVENT by task_name.
    handle_payloads = [p for p in payloads if p.get("task_name", "").endswith("handle_agent_event")]
    assert len(handle_payloads) == 1, f"expected exactly one handle_agent_event; got {handle_payloads}"

    task_outputs = handle_payloads[0]["args"]["outputs"]
    assert task_outputs["foo"] == "bar"
    assert task_outputs["exit_code"] == 0


@pytest.mark.asyncio
@pytest.mark.service
async def test_sink_return_overrides_same_key_in_event_outputs(db_session) -> None:
    """When the sink returns a key already present in event.outputs, the sink's
    value wins (sink keys override native values)."""
    org_id = uuid4()
    wfx_id = uuid4()

    run_sink = _StubRunSink(extras={"exit_code": 99})
    register_run_sink(run_sink)

    cmd_id = await _seed_command(org_id, workflow_execution_id=wfx_id, session=db_session)
    await db_session.commit()

    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outputs={"exit_code": 0},
        reported_at=datetime.now(UTC),
        traceparent="",
    )

    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, session=db_session)

    payloads = await get_pending_outbox_payloads(db_session)
    handle_payloads = [p for p in payloads if p.get("task_name", "").endswith("handle_agent_event")]
    assert len(handle_payloads) == 1

    task_outputs = handle_payloads[0]["args"]["outputs"]
    assert task_outputs["exit_code"] == 99


@pytest.mark.asyncio
@pytest.mark.service
async def test_sink_returning_none_leaves_outputs_unchanged(db_session) -> None:
    """A sink returning None leaves the HANDLE_AGENT_EVENT outputs equal to
    event.outputs — no merge, no extra keys."""
    org_id = uuid4()
    wfx_id = uuid4()

    run_sink = _StubRunSink(extras=None)
    register_run_sink(run_sink)

    cmd_id = await _seed_command(org_id, workflow_execution_id=wfx_id, session=db_session)
    await db_session.commit()

    event = AgentEvent(
        command_id=cmd_id,
        kind=AgentEventKind.COMPLETED_SUCCESS,
        outputs={"exit_code": 0, "stdout": "hello"},
        reported_at=datetime.now(UTC),
        traceparent="",
    )

    from app.core.audit_log import ActorKind  # noqa: PLC0415
    from app.core.auth import org_context  # noqa: PLC0415

    async with org_context(org_id, ActorKind.WORKSPACE):
        await record_agent_event(event, session=db_session)

    payloads = await get_pending_outbox_payloads(db_session)
    handle_payloads = [p for p in payloads if p.get("task_name", "").endswith("handle_agent_event")]
    assert len(handle_payloads) == 1

    task_outputs = handle_payloads[0]["args"]["outputs"]
    assert task_outputs == {"exit_code": 0, "stdout": "hello"}
