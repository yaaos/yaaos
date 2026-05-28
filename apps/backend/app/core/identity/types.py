"""Shared types for `core/identity` — Pydantic value objects + exceptions."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.core.identity.models import (
    OAuthIdentityRow,
    SessionRow,
    UserEmailRow,
    UserRow,
)


class User(BaseModel):
    id: UUID
    display_name: str
    github_username: str | None = None
    deactivated_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: UserRow) -> User:
        return cls(
            id=row.id,
            display_name=row.display_name,
            github_username=row.github_username,
            deactivated_at=row.deactivated_at,
            created_at=row.created_at,
        )


class UserEmail(BaseModel):
    id: UUID
    user_id: UUID
    email: str
    is_primary: bool
    verified_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: UserEmailRow) -> UserEmail:
        return cls(
            id=row.id,
            user_id=row.user_id,
            email=row.email,
            is_primary=row.is_primary,
            verified_at=row.verified_at,
            created_at=row.created_at,
        )


class OAuthIdentity(BaseModel):
    id: UUID
    user_id: UUID
    provider: str
    external_subject: str
    verified_at: datetime | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: OAuthIdentityRow) -> OAuthIdentity:
        return cls(
            id=row.id,
            user_id=row.user_id,
            provider=row.provider,
            external_subject=row.external_subject,
            verified_at=row.verified_at,
            created_at=row.created_at,
        )


class Session(BaseModel):
    token_hash: str
    user_id: UUID | None
    workspace_id: UUID | None
    sso_satisfied_for_org_id: UUID | None
    sso_satisfied_at: datetime | None
    csrf_token: str
    ip: str | None
    user_agent: str | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime

    @classmethod
    def from_row(cls, row: SessionRow) -> Session:
        return cls(
            token_hash=row.token_hash,
            user_id=row.user_id,
            workspace_id=row.workspace_id,
            sso_satisfied_for_org_id=row.sso_satisfied_for_org_id,
            sso_satisfied_at=row.sso_satisfied_at,
            csrf_token=row.csrf_token,
            ip=str(row.ip) if row.ip is not None else None,
            user_agent=row.user_agent,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
        )


class UserNotFoundError(LookupError):
    """No user matches the supplied id, email, or oauth identity."""


class EmailAlreadyLinkedError(ValueError):
    """An attempt to attach an email already linked to a different user."""


class SessionNotFoundError(LookupError):
    """No session with the supplied token hash (or session expired)."""


class TotpError(ValueError):
    """TOTP code rejected or secret missing."""
