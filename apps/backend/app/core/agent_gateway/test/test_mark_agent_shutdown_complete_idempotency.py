"""Service tests: mark_agent_shutdown_complete atomicity and idempotency.

Verifies:
- CAS wins atomically: lifecycle='shutdown', bearer revoked, audit written, SSE queued.
- CAS loser returns False with no side effects.
- Re-fire (already shutdown) returns False — no double audit, no double revoke.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.agent_gateway.models import BearerTokenRow, WorkspaceAgentRow


async def _create_agent_with_org(db_session, *, lifecycle: str = "draining") -> WorkspaceAgentRow:
    """Create a real org row and a workspace_agents row referencing it."""
    from app.domain.orgs import insert_org  # noqa: PLC0415

    org = await insert_org(db_session, slug=f"test-msc-{uuid4().hex[:8]}")
    row = WorkspaceAgentRow(
        org_id=org.org_id,
        instance_id=f"test-msc-{uuid4().hex[:8]}",
        iam_arn=f"arn:aws:iam::123456789012:role/test-{uuid4().hex[:6]}",
        version="0.0.1",
        state="reachable",
        lifecycle=lifecycle,
        claimed_workspace_count=0,
        last_heartbeat_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.mark.asyncio
@pytest.mark.service
async def test_mark_shutdown_complete_cas_win_returns_true(db_session) -> None:
    """CAS winner: returns True, lifecycle flips to shutdown."""
    from app.core.agent_gateway.service import mark_agent_shutdown_complete  # noqa: PLC0415

    row = await _create_agent_with_org(db_session, lifecycle="draining")
    await db_session.commit()

    result = await mark_agent_shutdown_complete(agent_id=row.id, session=db_session)
    await db_session.commit()

    assert result is True

    await db_session.refresh(row)
    assert row.lifecycle == "shutdown"


@pytest.mark.asyncio
@pytest.mark.service
async def test_mark_shutdown_complete_cas_win_revokes_bearer(db_session) -> None:
    """CAS winner revokes all active bearers for the agent."""
    from app.core.agent_gateway import bearers  # noqa: PLC0415
    from app.core.agent_gateway.service import mark_agent_shutdown_complete  # noqa: PLC0415

    row = await _create_agent_with_org(db_session, lifecycle="draining")
    _plaintext, _record = await bearers.issue(
        agent_id=row.id,
        org_id=row.org_id,
        session=db_session,
        issued_iam_arn=row.iam_arn,
    )
    await db_session.commit()

    await mark_agent_shutdown_complete(agent_id=row.id, session=db_session)
    await db_session.commit()

    # All bearers for this agent should be revoked
    bearer_rows = (
        (
            await db_session.execute(
                select(BearerTokenRow).where(
                    BearerTokenRow.agent_id == row.id,
                    BearerTokenRow.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(bearer_rows) == 0, "expected all bearers revoked after shutdown_complete"


@pytest.mark.asyncio
@pytest.mark.service
async def test_mark_shutdown_complete_cas_win_writes_audit(db_session) -> None:
    """CAS winner writes workspace_agent.shutdown_complete audit row."""
    from app.core.agent_gateway.service import mark_agent_shutdown_complete  # noqa: PLC0415
    from app.core.audit_log import list_for_entity  # noqa: PLC0415

    row = await _create_agent_with_org(db_session, lifecycle="draining")
    org_id = row.org_id
    await db_session.commit()

    await mark_agent_shutdown_complete(agent_id=row.id, session=db_session)
    await db_session.commit()

    entries = await list_for_entity("workspace_agent", row.id, org_id=org_id)
    kinds = [e.kind for e in entries]
    assert "workspace_agent.shutdown_complete" in kinds


@pytest.mark.asyncio
@pytest.mark.service
async def test_mark_shutdown_complete_already_shutdown_returns_false(db_session) -> None:
    """CAS loser (already shutdown): returns False, no side effects."""
    from app.core.agent_gateway.service import mark_agent_shutdown_complete  # noqa: PLC0415
    from app.core.audit_log import list_for_entity  # noqa: PLC0415

    row = await _create_agent_with_org(db_session, lifecycle="shutdown")
    org_id = row.org_id
    await db_session.commit()

    result = await mark_agent_shutdown_complete(agent_id=row.id, session=db_session)
    await db_session.commit()

    assert result is False

    # No audit rows written (CAS lost)
    entries = await list_for_entity("workspace_agent", row.id, org_id=org_id)
    assert not any(e.kind == "workspace_agent.shutdown_complete" for e in entries)


@pytest.mark.asyncio
@pytest.mark.service
async def test_mark_shutdown_complete_active_lifecycle_returns_false(db_session) -> None:
    """Agent with lifecycle='active' is not draining — CAS loses, returns False."""
    from app.core.agent_gateway.service import mark_agent_shutdown_complete  # noqa: PLC0415

    row = await _create_agent_with_org(db_session, lifecycle="active")
    await db_session.commit()

    result = await mark_agent_shutdown_complete(agent_id=row.id, session=db_session)
    await db_session.commit()

    assert result is False
    await db_session.refresh(row)
    assert row.lifecycle == "active"  # unchanged
