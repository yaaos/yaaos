"""Service test: CodeReview.dispatch records exception + ERROR status on span
when compile_invocation raises.

Exercises the catch block in commands/__init__.py `CodeReview.dispatch`. Registers
a minimal plugin stub whose `compile_invocation` raises, seeds the DB rows dispatch
needs, then asserts the surrounding span carries both an exception event and
StatusCode.ERROR.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from app.testing.observability import span_capture

pytestmark = pytest.mark.service


class _RaisingPlugin:
    """Minimal CodingAgentPlugin stub whose compile_invocation always raises."""

    plugin_id = "claude_code"

    def compile_invocation(self, invocation: Any) -> Any:
        raise RuntimeError("simulated build_invocation failure")

    def parse_result(self, terminal_event_payload: Any) -> Any:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_code_review_dispatch_compile_invocation_failure_sets_span_error(
    db_session,
) -> None:
    """compile_invocation failure records exception event + ERROR on active span."""
    from app.core import byok  # noqa: PLC0415
    from app.core.audit_log import Actor  # noqa: PLC0415
    from app.core.coding_agent import set_coding_agents_for_tests  # noqa: PLC0415
    from app.core.workflow import CommandContext  # noqa: PLC0415
    from app.domain.orgs import create_org  # noqa: PLC0415
    from app.domain.reviewer.commands import CodeReview, CodeReviewInputs  # noqa: PLC0415
    from app.testing.e2e_setup import seed_agent as _seed_agent  # noqa: PLC0415
    from app.testing.e2e_setup import seed_workspace as _seed_workspace  # noqa: PLC0415

    # Seed a real org so the byok key insert FK passes.
    org = await create_org(db_session, slug=f"t-{uuid4().hex[:8]}", display_name="t")
    org_id = org.id

    # Seed the DB rows dispatch needs to pass its workspace-owner guard.
    agent_row = await _seed_agent(org_id=org_id)
    ws_id = await _seed_workspace(
        org_id=org_id,
        provider_id="in_process",
        sha="deadbeef",
        agent_id=agent_row["id"],
    )
    # CodeReview.dispatch loads the Anthropic key before calling build_invocation.
    await byok.set(org_id, "anthropic", "sk-test-key", actor=Actor.system(), session=db_session)
    await db_session.commit()

    # Swap the coding-agent registry so the raising stub is under "claude_code".
    with set_coding_agents_for_tests() as fresh_registry:
        fresh_registry.replace(_RaisingPlugin())  # type: ignore[arg-type]

        cmd = CodeReview()
        ctx = CommandContext(
            ticket_id=str(uuid4()),
            workflow_execution_id=str(uuid4()),
            step_id="code_review",
            attempt=0,
        )
        inputs = CodeReviewInputs(
            workspace_id=ws_id,
            org_id=org_id,
            repo_external_id="owner/repo",
            pr_external_id="42",
            head_sha="deadbeef",
            base_sha="babecafe",
        )

        with span_capture() as exporter:
            tracer = trace.get_tracer(__name__)
            with tracer.start_as_current_span("workflow.start_step.CodeReview"):
                with pytest.raises(RuntimeError, match="simulated"):
                    await cmd.dispatch(inputs, ctx, session=db_session)

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
