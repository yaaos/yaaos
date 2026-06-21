"""Service test: failure-shaped catches in core/workspace record exception events on spans.

Samples CleanupWorkspace.execute() with close_workspace raising — asserts
the workflow.command.CleanupWorkspace span carries an `exception` event.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.core.workflow import CommandContext
from app.testing.observability import span_capture

pytestmark = pytest.mark.service


@pytest.mark.asyncio
async def test_workspace_cleanup_failure_records_on_span(db_session) -> None:  # type: ignore[no-untyped-def]
    """close_workspace failure inside CleanupWorkspace.execute records exception + ERROR on span."""
    import app.core.workspace.commands.cleanup as _cleanup_mod  # noqa: PLC0415

    original_close = _cleanup_mod.close_workspace

    async def _raising_close(ws_id: UUID) -> None:
        raise RuntimeError("simulated close_workspace failure")

    _cleanup_mod.close_workspace = _raising_close  # type: ignore[attr-defined]

    try:
        from app.core.workspace.commands import CleanupWorkspace, CleanupWorkspaceInputs  # noqa: PLC0415

        cmd = CleanupWorkspace()
        ctx = CommandContext(
            ticket_id="00000000-0000-0000-0000-000000000001",
            workflow_execution_id="00000000-0000-0000-0000-000000000002",
            step_id="cleanup_workspace",
            attempt=0,
        )
        with span_capture() as exporter:
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("workflow.command.CleanupWorkspace"):
                outcome = await cmd.execute(
                    CleanupWorkspaceInputs(workspace_id=UUID("00000000-0000-0000-0000-000000000099")), ctx
                )
    finally:
        _cleanup_mod.close_workspace = original_close  # type: ignore[attr-defined]

    assert outcome.kind.name == "FAILURE", f"expected FAILURE, got {outcome.kind}"

    spans = exporter.get_finished_spans()
    target = next((s for s in spans if "CleanupWorkspace" in s.name), None)
    assert target is not None, f"no CleanupWorkspace span; got: {[s.name for s in spans]}"

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, f"expected exception event on span, got: {[e.name for e in target.events]}"

    assert target.status.status_code == StatusCode.ERROR, (
        f"expected span status ERROR, got: {target.status.status_code}"
    )
