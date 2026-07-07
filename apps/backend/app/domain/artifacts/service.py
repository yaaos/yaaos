"""Stub service surface for `domain/artifacts`.

Bodies raise `NotImplementedError` — only the signatures are load-bearing.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.artifacts.types import Artifact, ArtifactGroup


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
) -> UUID:
    """Insert a new non-final artifact version; version = per-(ticket, stage_name) max+1."""
    raise NotImplementedError


async def mark_final(artifact_id: UUID, *, session: AsyncSession) -> None:
    """Flip `is_final` — the module's only mutation."""
    raise NotImplementedError


async def latest_final(
    *, org_id: UUID, ticket_id: UUID, stage_name: str, session: AsyncSession
) -> Artifact | None:
    """Return the latest final artifact for (ticket, stage_name), or None."""
    raise NotImplementedError


async def list_for_ticket(org_id: UUID, ticket_id: UUID, *, session: AsyncSession) -> list[ArtifactGroup]:
    """Return artifact versions grouped by stage_name, metadata only."""
    raise NotImplementedError


async def get(artifact_id: UUID, *, session: AsyncSession) -> Artifact:
    """Return the artifact with body. Raises `ArtifactNotFoundError`."""
    raise NotImplementedError
