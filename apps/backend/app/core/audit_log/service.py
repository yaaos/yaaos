"""Write helpers + read API for the audit log."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log.actor import Actor, ActorKind
from app.core.audit_log.models import AuditEntryRow
from app.core.database import session as get_session

# How long audit rows live before the periodic cleanup task purges them.
# MCP-dispatch entries are by far the dominant volume contributor in M04+
# (one row per JSON-RPC method), so retention is sized to keep storage bounded —
# 15 days strikes the right balance for the POC.
AUDIT_LOG_RETENTION = timedelta(days=15)


class AuditEntry(BaseModel):
    id: UUID
    org_id: UUID
    entity_kind: str
    entity_id: UUID
    kind: str
    payload: dict[str, Any]
    actor: Actor
    created_at: datetime

    @classmethod
    def from_row(cls, row: AuditEntryRow) -> AuditEntry:
        return cls(
            id=row.id,
            org_id=row.org_id,
            entity_kind=row.entity_kind,
            entity_id=row.entity_id,
            kind=row.kind,
            payload=row.payload,
            actor=Actor(
                kind=ActorKind(row.actor_kind),
                login=row.actor_login,
                agent_id=row.actor_agent_id,
                user_id=row.actor_user_id,
                workspace_id=row.actor_workspace_id,
            ),
            created_at=row.created_at,
        )


class AuditEntryNotFoundError(LookupError):
    """Raised by get() when no row matches the supplied id."""


def _payload_to_jsonable(payload: BaseModel) -> dict[str, Any]:
    """`.model_dump(mode='json')` produces JSON-compatible types (UUIDs → strings, etc.)."""
    if not isinstance(payload, BaseModel):
        raise TypeError("audit payload must be a Pydantic BaseModel")
    return payload.model_dump(mode="json")


async def audit(
    entity_kind: str,
    entity_id: UUID,
    kind: str,
    payload: BaseModel,
    actor: Actor,
    *,
    org_id: UUID,
    session: AsyncSession | None = None,
) -> AuditEntry:
    """Generic escape hatch. Per-entity helpers below are preferred."""
    if not entity_kind:
        raise ValueError("entity_kind required")
    if not kind:
        raise ValueError("kind required")

    payload_json = _payload_to_jsonable(payload)
    row = AuditEntryRow(
        id=uuid4(),
        org_id=org_id,
        entity_kind=entity_kind,
        entity_id=entity_id,
        kind=kind,
        payload=payload_json,
        actor_kind=actor.kind.value,
        actor_login=actor.login,
        actor_agent_id=actor.agent_id,
        actor_user_id=actor.user_id,
        actor_workspace_id=actor.workspace_id,
    )

    if session is not None:
        session.add(row)
        await session.flush()
    else:
        async with get_session() as s:
            s.add(row)
            await s.commit()
            await s.refresh(row)
    return AuditEntry.from_row(row)


# Per-entity helpers — same signature with `entity_kind` hard-coded.


async def audit_for_ticket(
    ticket_id: UUID,
    kind: str,
    payload: BaseModel,
    *,
    actor: Actor,
    org_id: UUID,
    session: AsyncSession | None = None,
) -> AuditEntry:
    return await audit("ticket", ticket_id, kind, payload, actor, org_id=org_id, session=session)


async def audit_for_pr(
    pr_id: UUID,
    kind: str,
    payload: BaseModel,
    *,
    actor: Actor,
    org_id: UUID,
    session: AsyncSession | None = None,
) -> AuditEntry:
    return await audit("pull_request", pr_id, kind, payload, actor, org_id=org_id, session=session)


async def audit_for_lesson(
    lesson_id: UUID,
    kind: str,
    payload: BaseModel,
    *,
    actor: Actor,
    org_id: UUID,
    session: AsyncSession | None = None,
) -> AuditEntry:
    return await audit("lesson", lesson_id, kind, payload, actor, org_id=org_id, session=session)


async def audit_for_review_job(
    review_job_id: UUID,
    kind: str,
    payload: BaseModel,
    *,
    actor: Actor,
    org_id: UUID,
    session: AsyncSession | None = None,
) -> AuditEntry:
    return await audit("review_job", review_job_id, kind, payload, actor, org_id=org_id, session=session)


async def audit_for_finding(
    finding_id: UUID,
    kind: str,
    payload: BaseModel,
    *,
    actor: Actor,
    org_id: UUID,
    session: AsyncSession | None = None,
) -> AuditEntry:
    """Plan §5.3: every durable-finding state transition writes an audit row."""
    return await audit("finding", finding_id, kind, payload, actor, org_id=org_id, session=session)


async def audit_for_webhook_event(
    webhook_event_id: UUID,
    kind: str,
    payload: BaseModel,
    *,
    actor: Actor,
    org_id: UUID,
    session: AsyncSession | None = None,
) -> AuditEntry:
    return await audit(
        "webhook_event", webhook_event_id, kind, payload, actor, org_id=org_id, session=session
    )


async def audit_for_workspace(
    workspace_id: UUID,
    kind: str,
    payload: BaseModel,
    *,
    actor: Actor,
    org_id: UUID,
    session: AsyncSession | None = None,
) -> AuditEntry:
    return await audit("workspace", workspace_id, kind, payload, actor, org_id=org_id, session=session)


# Read API


async def list_for_entity(
    entity_kind: str,
    entity_id: UUID,
    *,
    org_id: UUID,
    limit: int = 50,
    before_ts: datetime | None = None,
    kinds: list[str] | None = None,
) -> list[AuditEntry]:
    """Entries for the entity, newest first."""
    async with get_session() as s:
        stmt = (
            select(AuditEntryRow)
            .where(
                AuditEntryRow.org_id == org_id,
                AuditEntryRow.entity_kind == entity_kind,
                AuditEntryRow.entity_id == entity_id,
            )
            .order_by(AuditEntryRow.created_at.desc())
            .limit(limit)
        )
        if before_ts is not None:
            stmt = stmt.where(AuditEntryRow.created_at < before_ts)
        if kinds:
            stmt = stmt.where(AuditEntryRow.kind.in_(kinds))
        rows = (await s.execute(stmt)).scalars().all()
        return [AuditEntry.from_row(r) for r in rows]


async def get(entry_id: UUID, *, org_id: UUID) -> AuditEntry:
    async with get_session() as s:
        row = (
            await s.execute(
                select(AuditEntryRow).where(AuditEntryRow.id == entry_id, AuditEntryRow.org_id == org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise AuditEntryNotFoundError(str(entry_id))
        return AuditEntry.from_row(row)


async def list_for_org(
    *,
    org_id: UUID,
    actor_kinds: list[str] | None = None,
    actions: list[str] | None = None,
    before_ts: datetime | None = None,
    after_ts: datetime | None = None,
    limit: int = 50,
) -> list[AuditEntry]:
    """Cross-entity org-scoped feed for the Audit page (Owners/Admins).

    Filters by `actor_kinds`, `actions` (matches `kind` column), and a
    `created_at` window. Newest first; capped at 500 per page.
    """
    capped_limit = max(1, min(limit, 500))
    async with get_session() as s:
        stmt = (
            select(AuditEntryRow)
            .where(AuditEntryRow.org_id == org_id)
            .order_by(AuditEntryRow.created_at.desc())
            .limit(capped_limit)
        )
        if actor_kinds:
            stmt = stmt.where(AuditEntryRow.actor_kind.in_(actor_kinds))
        if actions:
            stmt = stmt.where(AuditEntryRow.kind.in_(actions))
        if before_ts is not None:
            stmt = stmt.where(AuditEntryRow.created_at < before_ts)
        if after_ts is not None:
            stmt = stmt.where(AuditEntryRow.created_at >= after_ts)
        rows = (await s.execute(stmt)).scalars().all()
        return [AuditEntry.from_row(r) for r in rows]


async def purge_older_than(cutoff: datetime) -> int:
    """Delete `audit_entries` rows with `created_at < cutoff`. Returns
    count purged. Used by the daily retention task."""
    from sqlalchemy import delete as sql_delete  # noqa: PLC0415

    async with get_session() as s:
        result = await s.execute(
            sql_delete(AuditEntryRow).where(AuditEntryRow.created_at < cutoff).returning(AuditEntryRow.id)
        )
        n = len(result.all())
        await s.commit()
        return n
