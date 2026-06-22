"""Service tests for the e2e_setup seed-agent and delete-user-artifacts endpoints.

Tests drive the underlying service functions and assert on durable DB state via
public module APIs only (intra-layer reach to testing/* is allowed; cross-module
imports stay within `__all__`-gated surfaces).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.agent_gateway import get_agent_info
from app.core.identity import (
    add_email,
    find_user_by_email,
    insert_user,
    mint_session,
)
from app.testing.e2e_setup.service import delete_user, seed_agent

# ---------------------------------------------------------------------------
# seed_agent writes a durable workspace_agents row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.service
async def test_seed_agent_endpoint_writes_durable_row(db_session) -> None:  # type: ignore[no-untyped-def]
    """seed_agent() inserts a workspace_agents row visible via get_agent_info."""
    org_id = uuid4()
    result = await seed_agent(org_id=org_id)
    assert "id" in result
    assert "instance_id" in result

    agent_id = result["id"]
    info = await get_agent_info(agent_id, session=db_session)
    assert info is not None, f"workspace_agents row {agent_id} not found after seed_agent()"
    assert info["org_id"] == org_id


# ---------------------------------------------------------------------------
# delete_user cascades to child rows (emails, sessions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.service
async def test_delete_user_artifacts_endpoint_cascades(db_session) -> None:  # type: ignore[no-untyped-def]
    """delete_user() removes the user row and cascades to identity-owned children."""
    # Insert a user with a primary email and a session so cascade is observable.
    user = await insert_user(db_session, display_name="Delete Me")
    email = "del-cascade@example.com"
    await add_email(db_session, user_id=user.id, email=email, is_primary=True, verified=True)
    await mint_session(db_session, user_id=user.id, workspace_id=None)
    await db_session.flush()

    user_id = user.id

    await delete_user(user_id)

    # User row must be gone — verify via email lookup (identity public API).
    found = await find_user_by_email(db_session, email)
    assert found is None, f"user row {user_id} still present after delete_user()"
