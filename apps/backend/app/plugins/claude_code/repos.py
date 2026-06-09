"""Repo-row service for the claude_code plugin.

Owns the `claude_code_repos` table. Provides:
- `get_or_create_repo_row` — upsert for the per-(org, repo) identity row.
- `resolve_skill` — read `skill_name` for a given (org, repo) pair. Returns
  `None` when the row is absent or `skill_name` is null/empty.
- `set_repo_skill` — write `skill_name` for a given (org, repo) pair. Creates
  the identity row via upsert if it doesn't exist yet.
- `list_repos_with_skill` — return all `(repo_external_id, skill_name)` pairs
  for the org, ordered by `repo_external_id`. Used by the settings list route.
"""

from __future__ import annotations

from uuid import UUID

import structlog
from sqlalchemy import select

from app.plugins.claude_code.models import ClaudeCodeRepoRow

if True:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("claude_code.repos")


async def get_or_create_repo_row(
    org_id: UUID,
    repo_external_id: str,
    *,
    session: AsyncSession,
) -> ClaudeCodeRepoRow:
    """Return the existing row or create a new one. Never commits."""
    row = (
        await session.execute(
            select(ClaudeCodeRepoRow).where(
                ClaudeCodeRepoRow.org_id == org_id,
                ClaudeCodeRepoRow.repo_external_id == repo_external_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = ClaudeCodeRepoRow(
            org_id=org_id,
            repo_external_id=repo_external_id,
        )
        session.add(row)
        await session.flush()
    return row


async def resolve_skill(
    org_id: UUID,
    repo_external_id: str,
    *,
    session: AsyncSession,
) -> str | None:
    """Return the configured skill name for the repo, or `None` if absent/empty.

    `None` means unconfigured — callers should treat it as an error before
    dispatching the review.
    """
    row = (
        await session.execute(
            select(ClaudeCodeRepoRow.skill_name).where(
                ClaudeCodeRepoRow.org_id == org_id,
                ClaudeCodeRepoRow.repo_external_id == repo_external_id,
            )
        )
    ).scalar_one_or_none()
    if not row:
        return None
    return row or None


async def set_repo_skill(
    org_id: UUID,
    repo_external_id: str,
    skill_name: str | None,
    *,
    session: AsyncSession,
) -> None:
    """Write `skill_name` for the given (org, repo) pair. Creates the identity
    row if it doesn't exist. Never commits."""
    row = await get_or_create_repo_row(org_id, repo_external_id, session=session)
    row.skill_name = skill_name or None


async def list_repos_with_skill(
    org_id: UUID,
    *,
    session: AsyncSession,
) -> list[dict[str, str | None]]:
    """Return all rows for the org as `{repo_external_id, skill_name}` dicts,
    ordered by `repo_external_id`."""
    rows = (
        await session.execute(
            select(ClaudeCodeRepoRow.repo_external_id, ClaudeCodeRepoRow.skill_name)
            .where(ClaudeCodeRepoRow.org_id == org_id)
            .order_by(ClaudeCodeRepoRow.repo_external_id)
        )
    ).all()
    return [{"repo_external_id": r.repo_external_id, "skill_name": r.skill_name} for r in rows]
