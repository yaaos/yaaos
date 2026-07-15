"""Service surface for `domain/artifacts`.

One entity, one table (`artifacts`). No descriptor/lineage entity — the
lineage ("the ticket's requirements document") is the `(ticket_id,
stage_name)` group, a composite key, not a row. `store` is append-only;
`mark_final` is the module's only mutation (never touches `body`).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.artifacts.models import ArtifactRow
from app.domain.artifacts.types import Artifact, ArtifactGroup, ArtifactMeta


class ArtifactNotFoundError(LookupError):
    """No artifact row for the given id."""


async def store(
    *,
    org_id: UUID,
    ticket_id: UUID,
    run_id: UUID,
    stage_execution_id: UUID,
    stage_name: str,
    body: str,
    iteration: int,
    session: AsyncSession,
    adopted_from_attachment_id: UUID | None = None,
) -> UUID:
    """Insert a new non-final artifact version; version = per-(ticket,
    stage_name) max+1. One-run-per-ticket serializes writers, so there's no
    concurrent-insert race to guard against here.

    `adopted_from_attachment_id` is set when the artifact body was synthesised
    directly from a ticket attachment (adoption path) rather than produced by a
    live coding-agent invocation.
    """
    current_max = (
        await session.execute(
            select(func.max(ArtifactRow.version)).where(
                ArtifactRow.ticket_id == ticket_id, ArtifactRow.stage_name == stage_name
            )
        )
    ).scalar_one()
    row = ArtifactRow(
        org_id=org_id,
        ticket_id=ticket_id,
        stage_name=stage_name,
        run_id=run_id,
        stage_execution_id=stage_execution_id,
        version=(current_max or 0) + 1,
        iteration=iteration,
        body=body,
        adopted_from_attachment_id=adopted_from_attachment_id,
    )
    session.add(row)
    await session.flush()
    return row.id


async def mark_final(artifact_id: UUID, *, session: AsyncSession) -> None:
    """Flip `is_final` — the module's only mutation; never touches `body`.
    No org check: callers (the pipelines engine) address a row they just
    created in the same run, before any HTTP org-scoping context exists."""
    await session.execute(update(ArtifactRow).where(ArtifactRow.id == artifact_id).values(is_final=True))


async def latest_final(
    *, org_id: UUID, ticket_id: UUID, stage_name: str, session: AsyncSession
) -> Artifact | None:
    """Return the latest final artifact for (ticket, stage_name), or None.
    Never sees a half-reviewed loop intermediate — only `is_final` rows."""
    row = (
        await session.execute(
            select(ArtifactRow)
            .where(
                ArtifactRow.org_id == org_id,
                ArtifactRow.ticket_id == ticket_id,
                ArtifactRow.stage_name == stage_name,
                ArtifactRow.is_final.is_(True),
            )
            .order_by(ArtifactRow.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return Artifact.from_row(row) if row is not None else None


async def list_for_ticket(org_id: UUID, ticket_id: UUID, *, session: AsyncSession) -> list[ArtifactGroup]:
    """Return artifact versions grouped by stage_name, metadata only (no bodies)."""
    rows = (
        (
            await session.execute(
                select(ArtifactRow)
                .where(ArtifactRow.org_id == org_id, ArtifactRow.ticket_id == ticket_id)
                .order_by(ArtifactRow.stage_name, ArtifactRow.version)
            )
        )
        .scalars()
        .all()
    )
    groups: dict[str, list[ArtifactMeta]] = {}
    for row in rows:
        groups.setdefault(row.stage_name, []).append(
            ArtifactMeta(
                id=row.id,
                version=row.version,
                run_id=row.run_id,
                iteration=row.iteration,
                is_final=row.is_final,
                adopted_from_attachment_id=row.adopted_from_attachment_id,
                created_at=row.created_at,
            )
        )
    return [ArtifactGroup(stage_name=name, versions=tuple(versions)) for name, versions in groups.items()]


async def adopted_attachment_ids_for_run(run_id: UUID, *, session: AsyncSession) -> set[UUID]:
    """Return the set of `adopted_from_attachment_id` values for all adopted
    artifacts in this run. Used by `_build_attachment_refs` to flip the role
    of matched attachment refs from ``"context"`` to ``"adopted"``."""
    rows = (
        (
            await session.execute(
                select(ArtifactRow.adopted_from_attachment_id).where(
                    ArtifactRow.run_id == run_id,
                    ArtifactRow.adopted_from_attachment_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return {r for r in rows if r is not None}


async def get(artifact_id: UUID, *, org_id: UUID, session: AsyncSession) -> Artifact:
    """Return the artifact with body, scoped to `org_id`. Raises
    `ArtifactNotFoundError` when the row is absent OR belongs to a
    different org — the caller can't distinguish the two, which is the
    point (no cross-org existence leak)."""
    row = (
        await session.execute(
            select(ArtifactRow).where(ArtifactRow.id == artifact_id, ArtifactRow.org_id == org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ArtifactNotFoundError(str(artifact_id))
    return Artifact.from_row(row)
