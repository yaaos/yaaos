"""PR upsert + state transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_log import Actor, audit_for_pr
from app.core.database import session as db_session
from app.domain.pull_requests.models import PullRequestRow
from app.domain.vcs import VCSPullRequest

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


class _PRSyncedPayload(BaseModel):
    changed_fields: list[str]


class _PRStateChangedPayload(BaseModel):
    from_state: str
    to_state: str


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
            id=uuid4(),
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

    changed: list[str] = []
    for field in (
        "title",
        "body",
        "base_sha",
        "head_sha",
        "is_draft",
        "state",
        "html_url",
    ):
        new = getattr(pr, field)
        if getattr(existing, field) != new:
            setattr(existing, field, new)
            changed.append(field)
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
