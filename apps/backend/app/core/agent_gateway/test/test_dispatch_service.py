"""Service test: `enqueue_command` emits `agent_command.dispatch.{kind}` spans.

Two scenarios:
- `test_enqueue_command_span_attributes` — happy path: the span is emitted with
  the expected name and attributes (`kind`, `command_id`, `workspace_id`,
  `workflow_id`).
- `test_enqueue_command_span_records_error` — error path: a duplicate-PK
  constraint violation causes the span to record the exception and carry
  `StatusCode.ERROR`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from opentelemetry.trace import StatusCode
from sqlalchemy.exc import IntegrityError

from app.core.agent_gateway.service import enqueue_command
from app.core.agent_gateway.types import (
    AuthBlock,
    ProvisionWorkspaceCommand,
    RepoRef,
)
from app.testing.observability import span_capture

pytestmark = pytest.mark.service


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_provision_cmd(workspace_id: UUID | None = None) -> ProvisionWorkspaceCommand:
    return ProvisionWorkspaceCommand(
        command_id=uuid4(),
        workspace_id=workspace_id or uuid4(),
        traceparent="00-aabbccdd-1122-01",
        repo=RepoRef(
            plugin_id="github",
            external_id="123",
            clone_url="https://github.com/me/repo.git",
            head_sha="deadbeef",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="tok"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_command_span_attributes(db_session) -> None:
    """`enqueue_command` emits exactly one `agent_command.dispatch.ProvisionWorkspace`
    span with `kind`, `command_id`, `workspace_id`, and `workflow_id` attributes."""
    org_id = uuid4()
    workflow_id = uuid4()
    cmd = _make_provision_cmd()

    with span_capture() as exporter:
        await enqueue_command(
            org_id,
            cmd,
            session=db_session,
            workflow_execution_id=workflow_id,
        )

    spans = exporter.get_finished_spans()
    target = next(
        (s for s in spans if s.name == "agent_command.dispatch.ProvisionWorkspace"),
        None,
    )
    assert target is not None, (
        f"expected 'agent_command.dispatch.ProvisionWorkspace' span; got: {[s.name for s in spans]}"
    )

    attrs = dict(target.attributes or {})
    assert attrs.get("kind") == "ProvisionWorkspace", f"unexpected kind attr: {attrs}"
    assert attrs.get("command_id") == str(cmd.command_id), f"unexpected command_id attr: {attrs}"
    assert attrs.get("workspace_id") == str(cmd.workspace_id), f"unexpected workspace_id attr: {attrs}"
    assert attrs.get("workflow_id") == str(workflow_id), f"unexpected workflow_id attr: {attrs}"


@pytest.mark.asyncio
async def test_enqueue_command_span_no_workflow_id(db_session) -> None:
    """`enqueue_command` with no `workflow_execution_id` sets `workflow_id` to empty string."""
    org_id = uuid4()
    cmd = _make_provision_cmd()

    with span_capture() as exporter:
        await enqueue_command(org_id, cmd, session=db_session)

    spans = exporter.get_finished_spans()
    target = next(
        (s for s in spans if s.name == "agent_command.dispatch.ProvisionWorkspace"),
        None,
    )
    assert target is not None
    attrs = dict(target.attributes or {})
    assert attrs.get("workflow_id") == "", f"expected empty workflow_id; got: {attrs}"


@pytest.mark.asyncio
async def test_enqueue_command_span_records_error(db_session) -> None:
    """A duplicate-PK flush error causes the span to record the exception and
    set `StatusCode.ERROR`."""
    org_id = uuid4()
    cmd = _make_provision_cmd()

    # First enqueue succeeds.
    await enqueue_command(org_id, cmd, session=db_session)
    await db_session.flush()

    # Second enqueue with the same command_id violates the PK constraint.
    with span_capture() as exporter:
        with pytest.raises(IntegrityError):
            await enqueue_command(org_id, cmd, session=db_session)

    spans = exporter.get_finished_spans()
    target = next(
        (s for s in spans if s.name == "agent_command.dispatch.ProvisionWorkspace"),
        None,
    )
    assert target is not None, (
        f"expected 'agent_command.dispatch.ProvisionWorkspace' span; got: {[s.name for s in spans]}"
    )

    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status on span, got: {target.status.status_code}"
    )
    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, f"expected exception event on span; events: {[e.name for e in target.events]}"
