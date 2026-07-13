"""Unit tests for the f2a3b4c5d6e7 credential-hygiene migration.

Verifies that the migration SQL is idempotent and that it correctly removes
`config.api_keys` from ConfigUpdate rows while leaving other rows untouched.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

_MIGRATION_SQL = (
    "UPDATE agent_commands SET payload = payload #- '{config,api_keys}' WHERE command_kind = 'ConfigUpdate'"
)


# ── Tests ────────────────────────────────────────────────────────────────────


@pytest.mark.service
async def test_migration_removes_api_keys_from_config_update(db_session) -> None:
    """Running the migration SQL once removes `config.api_keys` from ConfigUpdate rows."""
    # Insert a ConfigUpdate row with api_keys embedded (simulating old behaviour).
    # The payload is passed as a named bind parameter to avoid the SQLAlchemy
    # text() scanner misinterpreting ':2' (from JSON "max_workspaces":2) as a
    # positional bind parameter.
    payload_json = json.dumps(
        {
            "kind": "ConfigUpdate",
            "traceparent": "",
            "config": {"max_workspaces": 2, "api_keys": {"anthropic": "sk-secret"}},
        }
    )
    result = await db_session.execute(
        text(
            "INSERT INTO agent_commands (org_id, command_kind, status, attempt, payload) "
            "VALUES (gen_random_uuid(), 'ConfigUpdate', 'pending', 0, CAST(:payload AS jsonb)) "
            "RETURNING id::text"
        ),
        {"payload": payload_json},
    )
    row_id = result.fetchone()[0]

    # Execute the migration SQL.
    await db_session.execute(text(_MIGRATION_SQL))

    # Verify api_keys is gone.
    row = (
        await db_session.execute(text(f"SELECT payload FROM agent_commands WHERE id = '{row_id}'::uuid"))
    ).fetchone()
    assert row is not None
    config = row[0].get("config", {})
    assert "api_keys" not in config, (
        f"api_keys must be removed by migration; config after migration: {config!r}"
    )


@pytest.mark.service
async def test_migration_idempotent_running_twice(db_session) -> None:
    """Running the migration SQL twice produces the same result as running it once."""
    payload_json = json.dumps(
        {
            "kind": "ConfigUpdate",
            "traceparent": "",
            "config": {"max_workspaces": 2, "api_keys": {"anthropic": "sk-secret"}},
        }
    )
    result = await db_session.execute(
        text(
            "INSERT INTO agent_commands (org_id, command_kind, status, attempt, payload) "
            "VALUES (gen_random_uuid(), 'ConfigUpdate', 'pending', 0, CAST(:payload AS jsonb)) "
            "RETURNING id::text"
        ),
        {"payload": payload_json},
    )
    row_id = result.fetchone()[0]

    # Run twice.
    await db_session.execute(text(_MIGRATION_SQL))
    await db_session.execute(text(_MIGRATION_SQL))

    row = (
        await db_session.execute(text(f"SELECT payload FROM agent_commands WHERE id = '{row_id}'::uuid"))
    ).fetchone()
    assert row is not None
    config = row[0].get("config", {})
    assert "api_keys" not in config, f"Migration must be idempotent; config after second run: {config!r}"


@pytest.mark.service
async def test_migration_no_op_on_row_without_api_keys(db_session) -> None:
    """The migration SQL is a no-op on ConfigUpdate rows that have no api_keys."""
    payload_json = json.dumps(
        {
            "kind": "ConfigUpdate",
            "traceparent": "",
            "config": {"max_workspaces": 2},
        }
    )
    result = await db_session.execute(
        text(
            "INSERT INTO agent_commands (org_id, command_kind, status, attempt, payload) "
            "VALUES (gen_random_uuid(), 'ConfigUpdate', 'pending', 0, CAST(:payload AS jsonb)) "
            "RETURNING id::text"
        ),
        {"payload": payload_json},
    )
    row_id = result.fetchone()[0]

    await db_session.execute(text(_MIGRATION_SQL))

    row = (
        await db_session.execute(text(f"SELECT payload FROM agent_commands WHERE id = '{row_id}'::uuid"))
    ).fetchone()
    assert row is not None
    assert row[0].get("config", {}).get("max_workspaces") == 2, (
        "Migration must not alter rows that already lack api_keys"
    )


@pytest.mark.service
async def test_migration_does_not_affect_non_config_update_rows(db_session) -> None:
    """The migration SQL only touches ConfigUpdate rows; other kinds are unchanged."""
    result = await db_session.execute(
        text(
            "INSERT INTO agent_commands (org_id, command_kind, status, attempt, payload) "
            "VALUES ("
            "  gen_random_uuid(), 'InvokeClaudeCode', 'pending', 0,"
            '  \'{"kind":"InvokeClaudeCode","api_keys":{"anthropic":"should-stay"}}\'::jsonb'
            ") RETURNING id::text"
        )
    )
    row_id = result.fetchone()[0]

    await db_session.execute(text(_MIGRATION_SQL))

    row = (
        await db_session.execute(text(f"SELECT payload FROM agent_commands WHERE id = '{row_id}'::uuid"))
    ).fetchone()
    assert row is not None
    # The non-ConfigUpdate row must be untouched.
    assert row[0].get("api_keys") == {"anthropic": "should-stay"}, (
        f"Migration must not touch non-ConfigUpdate rows; got {row[0]!r}"
    )
