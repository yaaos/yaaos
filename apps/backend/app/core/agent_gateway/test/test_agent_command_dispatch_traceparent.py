"""Service test: `enqueue_command` stamps the dispatch span's own traceparent
into `AgentCommandRow.payload["traceparent"]`, not the caller's span.

Critical assertion: the span-id segment of the stored traceparent must match
the `agent_command.dispatch.<Kind>` span's span_id — proving that the agent
will parent `supervisor.dispatch.<Kind>` under the dispatch span, not the
outer caller.
"""

from __future__ import annotations

from uuid import uuid4, uuid7

import pytest
from opentelemetry import trace

from app.core.agent_gateway.models import AgentCommandRow
from app.core.agent_gateway.service import enqueue_command
from app.core.agent_gateway.types import (
    AuthBlock,
    ProvisionWorkspaceCommand,
    RepoRef,
)
from app.testing.observability import span_capture

pytestmark = pytest.mark.service


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_provision_cmd(workspace_id=None) -> ProvisionWorkspaceCommand:
    return ProvisionWorkspaceCommand(
        command_id=uuid7(),
        workspace_id=workspace_id or uuid4(),
        traceparent="",
        repo=RepoRef(
            plugin_id="github",
            external_id="456",
            clone_url="https://github.com/me/repo.git",
            head_sha="cafebabe",
        ),
        history=1,
        auth=AuthBlock(kind="github_installation", token="tok"),
        ttl_seconds=600,
        max_idle_seconds=600,
    )


def _span_id_from_traceparent(traceparent: str) -> str:
    """Extract the span-id (16-hex) segment from a W3C traceparent string."""
    parts = traceparent.split("-")
    assert len(parts) == 4, f"invalid traceparent format: {traceparent!r}"
    return parts[2]


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agent_command_dispatch_traceparent(db_session) -> None:
    """The traceparent stored in the agent_commands JSONB payload must be the
    dispatch span's own traceparent, not the outer caller's span.

    We open an outer parent span, then call `enqueue_command` inside it.
    The stored payload traceparent's span-id segment must equal the
    `agent_command.dispatch.ProvisionWorkspace` span's span_id — not the
    outer span's.
    """
    org_id = uuid4()
    workflow_id = uuid4()
    cmd = _make_provision_cmd()

    tracer = trace.get_tracer("test.agent_gateway")
    with span_capture() as exporter:
        with tracer.start_as_current_span("test.outer_parent") as outer_span:
            outer_span_id = f"{outer_span.get_span_context().span_id:016x}"
            await enqueue_command(
                org_id,
                cmd,
                session=db_session,
                workflow_execution_id=workflow_id,
            )

    spans = exporter.get_finished_spans()

    # Find the dispatch span.
    dispatch_span = next(
        (s for s in spans if s.name == "agent_command.dispatch.ProvisionWorkspace"),
        None,
    )
    assert dispatch_span is not None, (
        f"expected 'agent_command.dispatch.ProvisionWorkspace' span; got: {[s.name for s in spans]}"
    )
    dispatch_span_id = f"{dispatch_span.context.span_id:016x}"

    # The dispatch span must be a child of the outer span.
    assert dispatch_span.parent is not None, "dispatch span has no parent"
    assert f"{dispatch_span.parent.span_id:016x}" == outer_span_id, (
        f"dispatch span parent should be outer span {outer_span_id!r}, "
        f"got {dispatch_span.parent.span_id:016x}"
    )

    # Load the persisted row payload.
    from sqlalchemy import select  # noqa: PLC0415

    row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == cmd.command_id))
    ).scalar_one()
    stored_traceparent = row.payload.get("traceparent", "")

    assert stored_traceparent, f"expected non-empty traceparent in payload; got: {row.payload!r}"

    stored_span_id = _span_id_from_traceparent(stored_traceparent)

    # The stored span-id must be the dispatch span's, not the outer caller's.
    assert stored_span_id == dispatch_span_id, (
        f"payload traceparent span-id should be dispatch span {dispatch_span_id!r}, "
        f"got {stored_span_id!r}. "
        f"(outer span-id was {outer_span_id!r} — if these match, "
        f"the override in enqueue_command is missing.)"
    )
    assert stored_span_id != outer_span_id, (
        f"payload traceparent must NOT be the outer caller's span; "
        f"outer={outer_span_id!r}, stored={stored_span_id!r}"
    )
