"""Service test: CodeReview.dispatch records exception + ERROR status on span
when build_review_invocation raises.

Exercises the catch block at commands/__init__.py ~line 133 that was missing
set_status(ERROR). Registers a minimal plugin stub whose build_review_invocation
raises, seeds the DB rows dispatch needs, then asserts the surrounding span
carries both an exception event and StatusCode.ERROR.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.testing.observability import span_capture

pytestmark = pytest.mark.service


class _RaisingPlugin:
    """Minimal CodingAgentPlugin stub whose build_review_invocation always raises."""

    plugin_id = "claude_code"

    def install_url(self, org_id: UUID) -> str | None:
        del org_id
        return None

    def validate_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return dict(settings)

    async def build_review_invocation(self, ctx: Any, *, session: Any) -> Any:
        raise RuntimeError("simulated build_review_invocation failure")

    # The remaining Protocol methods are unreachable in this test path but are
    # included so type-checkers don't complain if Protocol is @runtime_checkable.
    async def review(self, *a: Any, **kw: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def incremental_review(self, *a: Any, **kw: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def verify_fix(self, *a: Any, **kw: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def stale_check(self, *a: Any, **kw: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def answer_question(self, *a: Any, **kw: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def validate_config(self, agent_config: Any) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def health_check(self) -> Any:  # type: ignore[override]
        raise NotImplementedError

    def parse_review_output(self, stdout: str) -> list:
        raise NotImplementedError

    def parse_usage(self, stdout: str) -> Any:
        raise NotImplementedError

    def render_activity(self, stdout: str) -> Any:
        raise NotImplementedError

    async def review_preflight_steps(self, ctx: Any, *, session: Any) -> tuple:
        return ()


class _StaticTicketContextProvider:
    """WorkflowContextProvider stub that returns a fixed WorkspaceTicketContext."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx

    async def get_workspace_ticket_context(self, ticket_id: UUID) -> Any:
        del ticket_id
        return self._ctx


@pytest.mark.asyncio
async def test_code_review_dispatch_build_invocation_failure_sets_span_error(
    db_session,
) -> None:
    """build_review_invocation failure records exception event + ERROR on active span."""
    from app.core.coding_agent import (  # noqa: PLC0415
        bind_coding_agent_registry,
        current_coding_agent_registry,
    )
    from app.core.workflow import CommandContext  # noqa: PLC0415
    from app.core.workspace import (  # noqa: PLC0415
        WorkspaceTicketContext,
        register_workflow_context_provider,
    )
    from app.domain.reviewer.commands import CodeReview  # noqa: PLC0415
    from app.testing.seed import seed_agent as _seed_agent  # noqa: PLC0415
    from app.testing.seed import seed_workspace as _seed_workspace  # noqa: PLC0415

    org_id = uuid4()

    # Seed the DB rows dispatch needs to pass its workspace-owner guard.
    agent_row = await _seed_agent(org_id=org_id, session=db_session)
    ws_id = await _seed_workspace(
        org_id=org_id,
        provider_id="in_process",
        sha="deadbeef",
        agent_id=agent_row["id"],
        caller_session=db_session,
    )
    await db_session.commit()

    # Install a context provider that returns a minimal valid WorkspaceTicketContext.
    register_workflow_context_provider(
        _StaticTicketContextProvider(
            WorkspaceTicketContext(
                org_id=org_id,
                plugin_id="claude_code",
                repo_external_id="owner/repo",
                payload={"head_sha": "deadbeef", "base_sha": "babecafe"},
            )
        )
    )

    # Swap the coding-agent registry so the raising stub is under "claude_code".
    prior_registry = current_coding_agent_registry()
    fresh_registry = prior_registry.copy()
    fresh_registry.replace(_RaisingPlugin())  # type: ignore[arg-type]
    bind_coding_agent_registry(fresh_registry)

    try:
        cmd = CodeReview()
        ctx = CommandContext(
            ticket_id=str(uuid4()),
            workflow_execution_id=str(uuid4()),
            step_id="code_review",
            attempt=0,
        )
        inputs = {"workspace_id": ws_id}

        with span_capture() as exporter:
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("workflow.start_step.CodeReview"):
                with pytest.raises(RuntimeError, match="build_review_invocation failed"):
                    await cmd.dispatch(inputs, ctx, session=db_session)
    finally:
        bind_coding_agent_registry(prior_registry)

    spans = exporter.get_finished_spans()
    target = next(
        (s for s in spans if "CodeReview" in s.name),
        None,
    )
    assert target is not None, f"no CodeReview span; got: {[s.name for s in spans]}"

    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, f"expected exception event on span, got: {[e.name for e in target.events]}"
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status, got {target.status.status_code}"
    )
