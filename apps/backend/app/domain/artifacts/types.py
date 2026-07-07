"""Value objects for `domain/artifacts`."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.domain.artifacts.models import ArtifactRow


class Artifact(BaseModel):
    """Domain value object for one artifact version, body included."""

    id: UUID
    org_id: UUID
    ticket_id: UUID
    stage_name: str
    run_id: UUID
    stage_execution_id: UUID
    version: int
    iteration: int
    is_final: bool
    body: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: ArtifactRow) -> Artifact:
        return cls(
            id=row.id,
            org_id=row.org_id,
            ticket_id=row.ticket_id,
            stage_name=row.stage_name,
            run_id=row.run_id,
            stage_execution_id=row.stage_execution_id,
            version=row.version,
            iteration=row.iteration,
            is_final=row.is_final,
            body=row.body,
            created_at=row.created_at,
        )


class ArtifactMeta(BaseModel):
    """Metadata-only view of one artifact version — no body."""

    id: UUID
    version: int
    run_id: UUID
    iteration: int
    is_final: bool
    created_at: datetime


class ArtifactGroup(BaseModel):
    """Versions grouped by `stage_name` — the version-dropdown shape."""

    stage_name: str
    versions: tuple[ArtifactMeta, ...]
