"""Seed, reset, and read helpers for tests — all via production service APIs.

Functions that insert canonical test rows (orgs, users, memberships, etc.),
reset registries, or read produced state, so each service test starts from a
known, minimal state without coupling to DB models.

Pytest-free by design: unlike `app/testing/isolation` (which imports
`pytest_asyncio` for its fixtures), this module pulls in no test framework, so
production-reachable testing code — the stub/fake coding-agent wrappers and
`e2e_setup`, all imported at app startup in stub/test mode — can import these
helpers without dragging pytest into the production image.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import ensure_agent_row

__all__ = [
    "delete_org",
    "delete_user_artifacts",
    "read_email_inbox",
    "seed_agent",
    "seed_workspace",
    "set_session_last_seen",
]


def read_email_inbox() -> list:
    """Return the list of `SentEmail` items captured in the current test's inbox.

    The list is mutable — tests may call `.clear()` on it if they need to
    discard prior messages within a single test body.
    """
    from app.domain.orgs.email import get_email_inbox  # noqa: PLC0415

    return get_email_inbox().messages


async def seed_agent(
    *,
    org_id: UUID,
    session: AsyncSession,
    iam_arn: str = "arn:aws:iam::123456789012:role/yaaos-agent",
    version: str = "0.0.1",
    heartbeat_age_seconds: int = 0,
    instance_id: str | None = None,
) -> dict:
    """Insert a reachable workspace-agent row for testing.

    Returns a dict with `id` (row PK), `instance_id`, and `org_id`.
    Backdates `last_heartbeat_at` when `heartbeat_age_seconds > 0`.
    """
    from app.core.agent_gateway.models import WorkspaceAgentRow  # noqa: PLC0415

    _instance_id = instance_id or f"test-instance-{uuid4().hex[:8]}"
    agent_id = await ensure_agent_row(
        org_id=org_id,
        instance_id=_instance_id,
        iam_arn=iam_arn,
        version=version,
        session=session,
    )
    if heartbeat_age_seconds > 0:
        row = await session.get(WorkspaceAgentRow, agent_id)
        if row is not None:
            row.last_heartbeat_at = datetime.now(UTC) - timedelta(seconds=heartbeat_age_seconds)
            await session.flush()
    return {"id": agent_id, "instance_id": _instance_id, "org_id": org_id}


async def seed_workspace(
    *,
    org_id: UUID,
    provider_id: str,
    plugin_state: dict,
    sha: str,
    current_command_id: UUID | None = None,
    current_holder_workflow_id: UUID | None = None,
    agent_id: UUID | None = None,
    status: str | None = None,
    caller_session: AsyncSession | None = None,
) -> str:
    """Insert a workspace row in `active` state with caller-supplied plugin_state.

    For cross-module tests that need a workspace in the DB without going through
    the full provision flow. Returns the workspace id string.

    When `caller_session` is supplied the row is added to the caller's transaction
    (no commit — the caller commits). When omitted a new session is opened and
    committed immediately.

    `current_command_id` and `current_holder_workflow_id` are optional — set
    them when the test needs to simulate a claimed workspace (agent_gateway tests).
    `agent_id` sets the owning agent (`WorkspaceRow.agent_id`) — set it when the
    test exercises per-agent ownership authz.
    """
    from app.core.database import session as get_session  # noqa: PLC0415
    from app.core.workspace.models import WorkspaceRow  # noqa: PLC0415
    from app.core.workspace.types import WorkspaceStatus  # noqa: PLC0415

    def _utcnow() -> datetime:
        return datetime.now(UTC)

    def _build_row() -> WorkspaceRow:
        return WorkspaceRow(
            org_id=org_id,
            provider_id=provider_id,
            spec={"sha": sha},
            status=status or WorkspaceStatus.ACTIVE.value,
            expires_at=_utcnow() + timedelta(hours=1),
            plugin_state=plugin_state,
            current_command_id=current_command_id,
            current_holder_workflow_id=current_holder_workflow_id,
            owning_agent_id=agent_id,
        )

    if caller_session is not None:
        row = _build_row()
        caller_session.add(row)
        await caller_session.flush()
        return str(row.id)

    async with get_session() as s:
        row = _build_row()
        s.add(row)
        await s.flush()
        ws_id = row.id
        await s.commit()
    return str(ws_id)


async def delete_org(session: AsyncSession, org_id: UUID) -> None:
    """Hard-delete an org row. Cascades to memberships, invitations, etc.

    Test-only teardown helper — there is no production org-deletion flow.
    Routes through the owning module's repository to keep raw SQL in one place.
    """
    from app.core.tenancy.repository import delete_org as _delete_org_row  # noqa: PLC0415

    await _delete_org_row(session, org_id)


async def delete_user_artifacts(db: AsyncSession, *, user_id: UUID) -> None:
    """Delete all identity-owned rows for `user_id` (user, emails, OAuth
    identities, sessions). DB-level CASCADE handles child rows when deleting
    the user row — callers that need cross-module cleanup (e.g. memberships)
    must handle those separately.
    """
    from sqlalchemy import delete  # noqa: PLC0415

    from app.core.identity.models import UserRow  # noqa: PLC0415

    await db.execute(delete(UserRow).where(UserRow.id == user_id))


async def set_session_last_seen(
    db: AsyncSession,
    *,
    token_hash: str,
    last_seen_at: datetime,
) -> None:
    """Write `last_seen_at` for a session row identified by `token_hash`.
    Helper to simulate idle sessions in tests without importing SessionRow.
    """
    from app.core.identity import repository as repo  # noqa: PLC0415

    row = await repo.get_session_by_hash(db, token_hash)
    assert row is not None, f"session not found for hash: {token_hash[:8]}..."
    row.last_seen_at = last_seen_at
    await db.flush()
