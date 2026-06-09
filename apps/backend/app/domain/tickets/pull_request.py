"""PR mirror — row, value object, and service operations owned by `domain/tickets`.

The `pull_requests` table is unchanged; only the module boundary moved.
All five services (`upsert`, `update_state`, `get`, `get_by_external`,
`list_by_ids`) live here as they are tightly coupled to the Ticket aggregate
they back-link via FK.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.audit_log import Actor, audit_for_pr
from app.core.database import Base
from app.core.database import session as db_session
from app.core.vcs import VCSPullRequest

# ---------------------------------------------------------------------------
# Row
# ---------------------------------------------------------------------------


class PullRequestRow(Base):
    __tablename__ = "pull_requests"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default=text("uuidv7()")
    )
    org_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False, index=True)
    plugin_id: Mapped[str] = mapped_column(String, nullable=False)
    external_id: Mapped[str] = mapped_column(String, nullable=False)
    repo_external_id: Mapped[str] = mapped_column(String, nullable=False, server_default="")
    ticket_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("tickets.id"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str | None] = mapped_column(String, nullable=True)
    author_login: Mapped[str] = mapped_column(String, nullable=False)
    author_type: Mapped[str] = mapped_column(String, nullable=False, default="user")
    base_branch: Mapped[str] = mapped_column(String, nullable=False)
    head_branch: Mapped[str] = mapped_column(String, nullable=False)
    base_sha: Mapped[str] = mapped_column(String, nullable=False)
    head_sha: Mapped[str] = mapped_column(String, nullable=False)
    is_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_fork: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="open")
    html_url: Mapped[str] = mapped_column(String, nullable=False)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("plugin_id", "external_id", name="uq_pull_requests_plugin_ext"),)


# ---------------------------------------------------------------------------
# Value object + type aliases
# ---------------------------------------------------------------------------

PRState = Literal["open", "closed", "merged"]


class PullRequest(BaseModel):
    id: UUID
    org_id: UUID
    plugin_id: str
    external_id: str
    repo_external_id: str
    ticket_id: UUID
    number: int
    title: str
    body: str | None
    author_login: str
    author_type: str
    base_branch: str
    head_branch: str
    base_sha: str
    head_sha: str
    is_draft: bool
    is_fork: bool
    state: PRState
    html_url: str
    last_synced_at: datetime
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: PullRequestRow) -> PullRequest:
        return cls(
            id=row.id,
            org_id=row.org_id,
            plugin_id=row.plugin_id,
            external_id=row.external_id,
            repo_external_id=row.repo_external_id,
            ticket_id=row.ticket_id,
            number=row.number,
            title=row.title,
            body=row.body,
            author_login=row.author_login,
            author_type=row.author_type,
            base_branch=row.base_branch,
            head_branch=row.head_branch,
            base_sha=row.base_sha,
            head_sha=row.head_sha,
            is_draft=row.is_draft,
            is_fork=row.is_fork,
            state=row.state,  # type: ignore[assignment]
            html_url=row.html_url,
            last_synced_at=row.last_synced_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class PullRequestNotFoundError(LookupError):
    pass


# ---------------------------------------------------------------------------
# Audit payload models (private to this module)
# ---------------------------------------------------------------------------


class _PRSyncedPayload(BaseModel):
    changed_fields: list[str]


class _PRStateChangedPayload(BaseModel):
    from_state: str
    to_state: str


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


async def upsert(
    pr: VCSPullRequest,
    *,
    ticket_id: UUID | None = None,
    org_id: UUID,
    session: AsyncSession,
) -> PullRequest:
    """Service: insert or refresh a PR row on the caller's session.

    The caller owns the transaction. We `flush()` so server-side defaults
    (`created_at`, `last_synced_at`) are populated on the in-memory row,
    but we never commit — the orchestrator decides when the work lands.

    The session parameter is what lets intake compose ticket + PR insert
    atomically: without it, the cross-session FK on `pull_requests.ticket_id`
    fires against the uncommitted ticket and blows up the request.
    """
    existing = (
        await session.execute(
            select(PullRequestRow).where(
                PullRequestRow.plugin_id == pr.plugin_id,
                PullRequestRow.external_id == pr.external_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        if ticket_id is None:
            raise ValueError("ticket_id required on insert")
        row = PullRequestRow(
            org_id=org_id,
            plugin_id=pr.plugin_id,
            external_id=pr.external_id,
            repo_external_id=pr.repo_external_id,
            ticket_id=ticket_id,
            number=pr.number,
            title=pr.title,
            body=pr.body,
            author_login=pr.author_login,
            author_type=pr.author_type,
            base_branch=pr.base_branch,
            head_branch=pr.head_branch,
            base_sha=pr.base_sha,
            head_sha=pr.head_sha,
            is_draft=pr.is_draft,
            is_fork=pr.is_fork,
            state=pr.state,
            html_url=pr.html_url,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row)
        return PullRequest.from_row(row)

    # Explicit per-field sync — compare incoming VO to the row, apply only
    # what changed, and record the field name for the audit payload.
    changed: list[str] = []
    if existing.title != pr.title:
        existing.title = pr.title
        changed.append("title")
    if existing.body != pr.body:
        existing.body = pr.body
        changed.append("body")
    if existing.base_sha != pr.base_sha:
        existing.base_sha = pr.base_sha
        changed.append("base_sha")
    if existing.head_sha != pr.head_sha:
        existing.head_sha = pr.head_sha
        changed.append("head_sha")
    if existing.is_draft != pr.is_draft:
        existing.is_draft = pr.is_draft
        changed.append("is_draft")
    if existing.state != pr.state:
        existing.state = pr.state
        changed.append("state")
    if existing.html_url != pr.html_url:
        existing.html_url = pr.html_url
        changed.append("html_url")
    existing.last_synced_at = datetime.now(UTC)
    if changed:
        await audit_for_pr(
            existing.id,
            "pull_request.synced",
            _PRSyncedPayload(changed_fields=changed),
            actor=Actor.system(),
            org_id=org_id,
            session=session,
        )
    await session.flush()
    # Refresh so `updated_at` (server-side onupdate) is populated on the
    # in-memory row before `from_row` reads it — without this, the attribute
    # is expired and the lazy-load triggers outside the greenlet context.
    await session.refresh(existing)
    return PullRequest.from_row(existing)


async def update_state(pr_id: UUID, new_state: PRState, *, org_id: UUID) -> None:
    async with db_session() as s:
        row = (
            await s.execute(
                select(PullRequestRow).where(PullRequestRow.id == pr_id, PullRequestRow.org_id == org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise PullRequestNotFoundError(str(pr_id))
        if row.state == new_state:
            return
        prev = row.state
        await s.execute(update(PullRequestRow).where(PullRequestRow.id == pr_id).values(state=new_state))
        await audit_for_pr(
            pr_id,
            "pull_request.state_changed",
            _PRStateChangedPayload(from_state=prev, to_state=new_state),
            actor=Actor.system(),
            org_id=org_id,
            session=s,
        )
        await s.commit()


async def get(pr_id: UUID, *, org_id: UUID) -> PullRequest:
    async with db_session() as s:
        row = (
            await s.execute(
                select(PullRequestRow).where(PullRequestRow.id == pr_id, PullRequestRow.org_id == org_id)
            )
        ).scalar_one_or_none()
    if row is None:
        raise PullRequestNotFoundError(str(pr_id))
    return PullRequest.from_row(row)


async def list_by_ids(pr_ids: list[UUID]) -> list[PullRequest]:
    """Return PullRequest objects for every id in *pr_ids* that exists.

    Missing ids are silently omitted. Empty input short-circuits — no DB hit.
    No org_id scoping: callers are expected to have already validated
    org membership via the ticket they fetched.
    """
    if not pr_ids:
        return []
    async with db_session() as s:
        rows = (await s.execute(select(PullRequestRow).where(PullRequestRow.id.in_(pr_ids)))).scalars().all()
    return [PullRequest.from_row(r) for r in rows]


async def get_by_external(plugin_id: str, external_id: str, *, org_id: UUID) -> PullRequest | None:
    async with db_session() as s:
        row = (
            await s.execute(
                select(PullRequestRow).where(
                    PullRequestRow.org_id == org_id,
                    PullRequestRow.plugin_id == plugin_id,
                    PullRequestRow.external_id == external_id,
                )
            )
        ).scalar_one_or_none()
    return PullRequest.from_row(row) if row is not None else None
