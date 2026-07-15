"""Pydantic value objects owned by `domain/attachments`."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class AttachmentMeta(BaseModel, frozen=True):
    """Metadata projection — all fields except `body`. Used by list reads, SPA, and MCP."""

    id: UUID
    filename: str
    produced_by_skill: str | None
    skill_version: str | None
    artifact_type: str | None
    repo_commit: str | None
    note: str | None
    attached_by: UUID
    attached_at: datetime


class Attachment(BaseModel, frozen=True):
    """Full attachment including body — returned by `get_attachment`."""

    id: UUID
    org_id: UUID
    ticket_id: UUID
    filename: str
    body: str
    produced_by_skill: str | None
    skill_version: str | None
    artifact_type: str | None
    produced_at: datetime | None
    repo_commit: str | None
    produced_from: str | None
    note: str | None
    attached_by: UUID
    attached_at: datetime
