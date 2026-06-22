"""Service tests: typed payload_fields for enqueue_command_payload + envelope-wins integrity.

Three scenarios:
- `test_invoke_payload_fields_round_trip` — `InvokeClaudeCodeFields` serialises to
  exactly the flat keys expected by `_row_to_command`, and round-tripping through
  `enqueue_command_payload` → fetch row → `_row_to_command` reproduces an
  `InvokeClaudeCodeCommand` equivalent.
- `test_enqueue_command_payload_typed_fields_key_set` — the JSONB key set stored
  in `agent_commands.payload` is byte-identical to the legacy dict-based call.
- `test_enqueue_command_payload_envelope_wins` — passing an `InvokeClaudeCodeFields`
  instance that has no envelope keys confirms that the envelope fields
  (`kind`, `command_id`, `traceparent`, `completion_token`, `workflow_execution_id`,
  `workspace_id`) are present in the persisted payload even though the fields
  model doesn't carry them.
"""

from __future__ import annotations

from uuid import uuid4, uuid7

import pytest
from sqlalchemy import select

from app.core.agent_gateway.models import AgentCommandRow
from app.core.agent_gateway.service import _row_to_command, enqueue_command_payload
from app.core.agent_gateway.types import (
    AgentCommandKind,
    InvokeClaudeCodeCommand,
    InvokeClaudeCodeFields,
)
from app.testing.e2e_setup import seed_agent

pytestmark = pytest.mark.service

# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_invoke_fields() -> InvokeClaudeCodeFields:
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
    )


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_payload_fields_round_trip(db_session) -> None:
    """Enqueue via `InvokeClaudeCodeFields`, then fetch the row and deserialise
    via `_row_to_command`; confirm it produces an `InvokeClaudeCodeCommand`."""
    org_id = uuid4()
    await seed_agent(org_id=org_id)

    command_id = uuid7()
    workspace_id = uuid4()
    workflow_id = uuid4()
    fields = _make_invoke_fields()

    await enqueue_command_payload(
        org_id,
        command_id=command_id,
        kind=AgentCommandKind.INVOKE_CLAUDE_CODE,
        workspace_id=workspace_id,
        payload_fields=fields,
        session=db_session,
        workflow_execution_id=workflow_id,
    )
    await db_session.flush()

    row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == command_id))
    ).scalar_one()

    cmd = _row_to_command(row)
    assert isinstance(cmd, InvokeClaudeCodeCommand), f"expected InvokeClaudeCodeCommand, got {type(cmd)}"
    assert cmd.command_id == command_id
    assert cmd.workspace_id == workspace_id
    assert cmd.kind == AgentCommandKind.INVOKE_CLAUDE_CODE
    assert cmd.invocation == fields.invocation
    assert cmd.limits.wallclock_seconds == 300


@pytest.mark.asyncio
async def test_enqueue_command_payload_typed_fields_key_set(db_session) -> None:
    """The JSONB key set stored via `InvokeClaudeCodeFields` must equal the
    legacy flat-dict key set — no new or missing keys, preserving wire shape."""
    org_id = uuid4()
    await seed_agent(org_id=org_id)

    workspace_id = uuid4()
    workflow_id = uuid4()

    # Enqueue via new typed path.
    typed_id = uuid7()
    fields = _make_invoke_fields()
    await enqueue_command_payload(
        org_id,
        command_id=typed_id,
        kind=AgentCommandKind.INVOKE_CLAUDE_CODE,
        workspace_id=workspace_id,
        payload_fields=fields,
        session=db_session,
        workflow_execution_id=workflow_id,
    )
    await db_session.flush()
    typed_row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == typed_id))
    ).scalar_one()
    typed_keys = set(typed_row.payload.keys())

    # Expected keys: the 4 kind-specific fields + 6 envelope fields.
    expected_kind_keys = {"invocation", "mcp_servers", "limits", "result_spec"}
    expected_envelope_keys = {
        "kind",
        "command_id",
        "traceparent",
        "completion_token",
        "workflow_execution_id",
        "workspace_id",
    }
    expected_keys = expected_kind_keys | expected_envelope_keys
    assert typed_keys == expected_keys, (
        f"JSONB key set mismatch.\n  got:      {sorted(typed_keys)}\n  expected: {sorted(expected_keys)}"
    )


@pytest.mark.asyncio
async def test_enqueue_command_payload_envelope_wins(db_session) -> None:
    """Envelope identity fields must be present in the persisted payload and
    must reflect the named parameters, not any value from payload_fields."""
    org_id = uuid4()
    await seed_agent(org_id=org_id)

    command_id = uuid7()
    workspace_id = uuid4()
    workflow_id = uuid4()
    fields = _make_invoke_fields()

    await enqueue_command_payload(
        org_id,
        command_id=command_id,
        kind=AgentCommandKind.INVOKE_CLAUDE_CODE,
        workspace_id=workspace_id,
        payload_fields=fields,
        session=db_session,
        workflow_execution_id=workflow_id,
    )
    await db_session.flush()

    row = (
        await db_session.execute(select(AgentCommandRow).where(AgentCommandRow.id == command_id))
    ).scalar_one()

    p = row.payload
    # Envelope fields must be set from named params.
    assert p["kind"] == "InvokeClaudeCode", f"kind mismatch: {p['kind']}"
    assert p["command_id"] == str(command_id), f"command_id mismatch: {p['command_id']}"
    assert p["workspace_id"] == str(workspace_id), f"workspace_id mismatch: {p['workspace_id']}"
    assert p["workflow_execution_id"] == str(workflow_id), f"wfx_id mismatch: {p['workflow_execution_id']}"
    assert p["completion_token"] is None, (
        f"completion_token should be None at enqueue: {p['completion_token']}"
    )
    # traceparent is set by the dispatch span — just confirm it's present.
    assert "traceparent" in p
