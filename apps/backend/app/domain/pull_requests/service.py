"""PR upsert + state transitions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import select, update

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
) -> PullRequest:
    async with db_session() as s:
        existing = (
            await s.execute(
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
            s.add(row)
            await s.commit()
            await s.refresh(row)
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
        await s.commit()
        await s.refresh(existing)
        row_id = existing.id

    if changed:
        await audit_for_pr(
            row_id,
            "pull_request.synced",
            _PRSyncedPayload(changed_fields=changed),
            actor=Actor.system(),
            org_id=org_id,
        )

    async with db_session() as s:
        existing = (await s.execute(select(PullRequestRow).where(PullRequestRow.id == row_id))).scalar_one()
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
        await s.commit()
    await audit_for_pr(
        pr_id,
        "pull_request.state_changed",
        _PRStateChangedPayload(from_state=prev, to_state=new_state),
        actor=Actor.system(),
        org_id=org_id,
    )


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
