"""Value objects for `domain/findings`."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.core.audit_log import Actor
from app.domain.findings.models import FindingRow

Severity = Literal["blocker", "should_fix", "nit"]
FindingStatus = Literal["open", "resolved", "dismissed"]
StatusEventMethod = Literal["review_verdict", "user_overrode"]


class FindingStatusEvent(BaseModel):
    """Appended per transition AND per re-assertion (re-flag/re-sighting)."""

    status: FindingStatus
    method: StatusEventMethod
    actor: Actor
    run_id: UUID | None = None
    stage_execution_id: UUID | None = None
    comment_external_id: str | None = None
    at: datetime


class FindingSpec(BaseModel):
    """Write input for `record_findings` — findings-owned so
    `pipelines → findings` stays one-way."""

    id: UUID
    severity: Severity
    body: str
    code_file: str | None = None
    code_line: int | None = None
    artifact_section: str | None = None
    defect_in_artifact: str | None = None
    display_prefix: str


class AutoApproveConditions(BaseModel):
    """The four Repos-page auto-approve checkboxes."""

    no_blocker: bool = False
    no_should_fix: bool = False
    no_nit: bool = False
    all_confirmed_fixed: bool = False


class Finding(BaseModel):
    """Domain value object for one durable finding."""

    id: UUID
    org_id: UUID
    ticket_id: UUID
    source_run_id: UUID
    source_stage_name: str
    source_stage_execution_id: UUID
    first_seen_iteration: int
    display_prefix: str
    display_id: int
    severity: Severity
    body: str
    code_file: str | None
    code_line: int | None
    artifact_section: str | None
    defect_in_artifact: str | None
    status: FindingStatus
    status_events: tuple[FindingStatusEvent, ...]
    defended_at: datetime | None
    external_comment_id: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def handle(self) -> str:
        return f"{self.display_prefix}-{self.display_id:03d}"

    @classmethod
    def from_row(cls, row: FindingRow) -> Finding:
        return cls(
            id=row.id,
            org_id=row.org_id,
            ticket_id=row.ticket_id,
            source_run_id=row.source_run_id,
            source_stage_name=row.source_stage_name,
            source_stage_execution_id=row.source_stage_execution_id,
            first_seen_iteration=row.first_seen_iteration,
            display_prefix=row.display_prefix,
            display_id=row.display_id,
            severity=row.severity,  # type: ignore[arg-type]
            body=row.body,
            code_file=row.code_file,
            code_line=row.code_line,
            artifact_section=row.artifact_section,
            defect_in_artifact=row.defect_in_artifact,
            status=row.status,  # type: ignore[arg-type]
            status_events=tuple(FindingStatusEvent.model_validate(e) for e in row.status_events),
            defended_at=row.defended_at,
            external_comment_id=row.external_comment_id,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class InvalidFindingTransition(ValueError):
    """An illegal status jump was attempted (e.g. resurrecting a dismissed finding)."""
