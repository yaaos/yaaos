"""Service tests: agent_gateway delegates workspace-state work to the sink.

Registers a stub sink so workspace is never imported here — the stub is
swapped in via `register_report_sink` / `clear_report_sink` from
`app.core.agent_gateway.report_sink`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.core.agent_gateway import (
    HeartbeatRequest,
    HeartbeatWorkspaceEntry,
    StaleClaimError,
    WorkspaceEvent,
    WorkspaceEventKind,
    WorkspaceEventOutcome,
    WorkspaceEventReport,
    record_heartbeat,
    record_workspace_event,
    register_report_sink,
)
from app.core.agent_gateway.report_sink import clear_report_sink

# ── Stub sink ──────────────────────────────────────────────────────────


class _StubSink:
    """Minimal in-memory stub satisfying WorkspaceAgentReportSink."""

    def __init__(
        self,
        *,
        # id → status map for reconcile_heartbeat
        statuses: dict[UUID, str] | None = None,
        # command_id → holder_workflow_id for resolve_claim
        claims: dict[UUID, UUID | None] | None = None,
        # workspace_id → (current_command_id, accepted) for apply_workspace_event
        ws_commands: dict[UUID, UUID | None] | None = None,
    ) -> None:
        self.statuses: dict[UUID, str] = statuses or {}
        self.claims: dict[UUID, UUID | None] = claims or {}
        self.ws_commands: dict[UUID, UUID | None] = ws_commands or {}
        self.applied_events: list[WorkspaceEventReport] = []

    async def reconcile_heartbeat(self, reported_ids: set[UUID], session: object) -> set[UUID]:
        forgotten: set[UUID] = set()
        for ws_id in reported_ids:
            status = self.statuses.get(ws_id)
            if status is None or status == "destroyed":
                forgotten.add(ws_id)
        return forgotten

    _MISSING = object()

    async def apply_workspace_event(
        self, report: WorkspaceEventReport, session: object
    ) -> WorkspaceEventOutcome:
        self.applied_events.append(report)
        # Workspace not registered in stub → unknown workspace, reject.
        sentinel = self._MISSING
        current_cmd = self.ws_commands.get(report.workspace_id, sentinel)
        if current_cmd is sentinel:
            return WorkspaceEventOutcome(resolved_status=None, accepted=False)
        # Stale-claim guard: non-None current_cmd must match event's command_id.
        if current_cmd is not None and current_cmd != report.command_id:
            return WorkspaceEventOutcome(resolved_status=None, accepted=False)
        kind_map = {"ready": "active", "destroyed": "destroyed", "failed": "destroy_failed"}
        return WorkspaceEventOutcome(
            resolved_status=kind_map.get(report.kind),
            accepted=True,
        )

    async def resolve_claim(self, command_id: UUID, session: object) -> UUID | None:
        return self.claims.get(command_id)


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_sink():
    from app.core.agent_gateway import get_report_sink  # noqa: PLC0415

    # Save the previously-registered sink (may raise if none registered) so
    # other test modules that registered the real workspace sink aren't broken
    # by these tests clearing the slot.
    try:
        prior = get_report_sink()
    except RuntimeError:
        prior = None

    clear_report_sink()
    yield
    clear_report_sink()
    if prior is not None:
        register_report_sink(prior)


# ── Tests: record_heartbeat delegates reconciliation to the sink ────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_reconcile_via_sink(db_session) -> None:
    """record_heartbeat forwards reported ids to the sink; ids the sink marks
    forgotten surface in HeartbeatResponse.forgotten_workspaces."""
    known_id = uuid4()
    unknown_id = uuid4()
    destroyed_id = uuid4()

    stub = _StubSink(statuses={known_id: "active", destroyed_id: "destroyed"})
    register_report_sink(stub)

    request = HeartbeatRequest(
        reported_at=datetime.now(UTC),
        workspaces=(
            HeartbeatWorkspaceEntry(workspace_id=known_id, status="running"),
            HeartbeatWorkspaceEntry(workspace_id=unknown_id, status="running"),
            HeartbeatWorkspaceEntry(workspace_id=destroyed_id, status="running"),
        ),
    )
    response = await record_heartbeat(uuid4(), request, session=db_session)
    assert set(response.forgotten_workspaces) == {unknown_id, destroyed_id}


@pytest.mark.asyncio
@pytest.mark.service
async def test_heartbeat_empty_returns_no_forgotten(db_session) -> None:
    register_report_sink(_StubSink())
    request = HeartbeatRequest(reported_at=datetime.now(UTC), workspaces=())
    response = await record_heartbeat(uuid4(), request, session=db_session)
    assert response.forgotten_workspaces == ()


# ── Tests: record_workspace_event delegates to the sink ───────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_record_workspace_event_delegates_to_sink(db_session) -> None:
    """record_workspace_event calls apply_workspace_event on the sink; no
    direct workspace DB access happens in this code path."""
    ws_id = uuid4()
    cmd_id = uuid4()

    stub = _StubSink(ws_commands={ws_id: cmd_id})
    register_report_sink(stub)

    event = WorkspaceEvent(
        workspace_id=ws_id,
        command_id=cmd_id,
        kind=WorkspaceEventKind.READY,
        reported_at=datetime.now(UTC),
    )
    await record_workspace_event(event, session=db_session)

    assert len(stub.applied_events) == 1
    applied = stub.applied_events[0]
    assert applied.workspace_id == ws_id
    assert applied.command_id == cmd_id
    assert applied.kind == "ready"


@pytest.mark.asyncio
@pytest.mark.service
async def test_record_workspace_event_raises_stale_when_sink_rejects(db_session) -> None:
    """When the sink returns accepted=False, record_workspace_event raises
    StaleClaimError (the caller maps this to 410 Gone)."""
    ws_id = uuid4()
    cmd_id = uuid4()
    other_cmd = uuid4()

    # ws_commands maps ws_id → other_cmd so our event's cmd_id mismatches.
    stub = _StubSink(ws_commands={ws_id: other_cmd})
    register_report_sink(stub)

    event = WorkspaceEvent(
        workspace_id=ws_id,
        command_id=cmd_id,
        kind=WorkspaceEventKind.READY,
        reported_at=datetime.now(UTC),
    )
    with pytest.raises(StaleClaimError):
        await record_workspace_event(event, session=db_session)


@pytest.mark.asyncio
@pytest.mark.service
async def test_record_workspace_event_unknown_workspace_raises(db_session) -> None:
    """An event for a workspace the sink doesn't know about is rejected."""
    ws_id = uuid4()
    stub = _StubSink()  # empty — ws_id not present
    register_report_sink(stub)

    # apply_workspace_event returns accepted=False when workspace not found
    event = WorkspaceEvent(
        workspace_id=ws_id,
        command_id=uuid4(),
        kind=WorkspaceEventKind.READY,
        reported_at=datetime.now(UTC),
    )
    with pytest.raises(StaleClaimError):
        await record_workspace_event(event, session=db_session)
