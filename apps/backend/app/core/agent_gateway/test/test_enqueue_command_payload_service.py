"""Service test: `enqueue_command_payload` inserts a row and emits a dispatch span.

Two scenarios:
- `test_enqueue_command_payload_row_and_span` — happy path: the row lands with
  the supplied `command_id`, `kind`, and `payload_fields` (modulo injected
  `traceparent`); the `agent_command.dispatch.InvokeClaudeCode` span fires with
  the expected `kind`, `command_id`, and `workspace_id` attributes.
- `test_enqueue_command_payload_span_records_error` — error path: a duplicate-PK
  constraint violation causes the span to record the exception and carry
  `StatusCode.ERROR`.
"""

from __future__ import annotations

from uuid import uuid4, uuid7

import pytest
from opentelemetry.trace import StatusCode
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.agent_gateway.models import AgentCommandRow
from app.core.agent_gateway.service import enqueue_command_payload
from app.core.agent_gateway.types import AgentCommandKind, InvokeClaudeCodeFields
from app.testing.e2e_setup import seed_agent
from app.testing.observability import span_capture

pytestmark = pytest.mark.service


# ── Helpers ───────────────────────────────────────────────────────────────────


def _invoke_fields() -> InvokeClaudeCodeFields:
    return InvokeClaudeCodeFields(
        invocation={
            "exec": {
                "argv": ["claude", "--print", "hello"],
                "stdin": "",
                "env": {},
            }
        },
        mcp_servers=[],
        limits={"wallclock_seconds": 300},
        result_spec={},
        skill_path=".claude/skills/pr_review/SKILL.md",
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_command_payload_row_and_span(db_session) -> None:
    """`enqueue_command_payload` inserts an `agent_commands` row with the
    supplied `command_id` + `kind` + `payload_fields`, and emits an
    `agent_command.dispatch.InvokeClaudeCode` span."""
    # Need an org row for the FK on agent_commands.
    org_id = uuid4()
    # Seed a workspace agent row so the org_id FK from the agent row is present.
    # agent_commands has no FK to workspace_agents directly, but seed_agent inserts
    # the org-level agent row that some FK paths depend on.  The `org_id` is what
    # agent_commands actually uses — no FK to workspace_agents on that table.
    await seed_agent(org_id=org_id)

    command_id = uuid7()
    workspace_id = uuid4()
    run_id = uuid4()
    fields = _invoke_fields()

    with span_capture() as exporter:
        await enqueue_command_payload(
            org_id,
            command_id=command_id,
            kind=AgentCommandKind.INVOKE_CLAUDE_CODE,
            workspace_id=workspace_id,
            payload_fields=fields,
            session=db_session,
            run_id=run_id,
        )

    # Row was inserted with the expected PK and kind.
    row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == command_id))
    ).scalar_one_or_none()
    assert row is not None, "agent_commands row not found after enqueue_command_payload"
    assert row.command_kind == "InvokeClaudeCode"
    assert row.status == "pending"
    assert row.run_id == run_id
    # The dispatch span injects `traceparent` into the payload — all other keys
    # must be present and untouched.
    assert row.payload.get("invocation") == fields.invocation
    assert row.payload.get("limits") == fields.limits
    assert "traceparent" in row.payload, "traceparent must be injected into the persisted payload"

    # Dispatch span was emitted.
    spans = exporter.get_finished_spans()
    target = next(
        (s for s in spans if s.name == "agent_command.dispatch.InvokeClaudeCode"),
        None,
    )
    assert target is not None, (
        f"expected 'agent_command.dispatch.InvokeClaudeCode' span; got: {[s.name for s in spans]}"
    )
    attrs = dict(target.attributes or {})
    assert attrs.get("kind") == "InvokeClaudeCode", f"unexpected kind attr: {attrs}"
    assert attrs.get("command_id") == str(command_id), f"unexpected command_id attr: {attrs}"
    assert attrs.get("workspace_id") == str(workspace_id), f"unexpected workspace_id attr: {attrs}"
    assert attrs.get("run_id") == str(run_id), f"unexpected run_id attr: {attrs}"


@pytest.mark.asyncio
async def test_enqueue_command_payload_span_records_error(db_session) -> None:
    """A duplicate-PK flush error causes the span to record the exception and
    set `StatusCode.ERROR`."""
    org_id = uuid4()
    await seed_agent(org_id=org_id)

    command_id = uuid7()
    workspace_id = uuid4()

    # First enqueue succeeds.
    await enqueue_command_payload(
        org_id,
        command_id=command_id,
        kind=AgentCommandKind.INVOKE_CLAUDE_CODE,
        workspace_id=workspace_id,
        payload_fields=_invoke_fields(),
        session=db_session,
    )
    await db_session.flush()

    # Second enqueue with the same command_id violates the PK constraint.
    with span_capture() as exporter:
        with pytest.raises(IntegrityError):
            await enqueue_command_payload(
                org_id,
                command_id=command_id,
                kind=AgentCommandKind.INVOKE_CLAUDE_CODE,
                workspace_id=workspace_id,
                payload_fields=_invoke_fields(),
                session=db_session,
            )

    spans = exporter.get_finished_spans()
    target = next(
        (s for s in spans if s.name == "agent_command.dispatch.InvokeClaudeCode"),
        None,
    )
    assert target is not None, (
        f"expected 'agent_command.dispatch.InvokeClaudeCode' span; got: {[s.name for s in spans]}"
    )
    assert target.status.status_code == StatusCode.ERROR, (
        f"expected ERROR status on span, got: {target.status.status_code}"
    )
    exception_events = [e for e in target.events if e.name == "exception"]
    assert exception_events, f"expected exception event on span; events: {[e.name for e in target.events]}"
