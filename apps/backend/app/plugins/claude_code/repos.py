"""Skill manifest cache service for the claude_code plugin.

Owns the `claude_code_repos` table. Provides:
- `SkillManifestEntry` — one discovered skill (`name`, `source`, `plugin_name`).
- `get_or_create_repo_row` — upsert the per-(org, repo) row.
- `persist_skill_manifest` — write the enumeration result into the row.
- `get_skill_manifest` — read the cached manifest.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.plugins.claude_code.models import ClaudeCodeRepoRow

if True:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger("claude_code.repos")


class SkillManifestEntry(BaseModel):
    """One element of an enumerated skills list.

    `name` is the invocation handle: the directory name for repo-local skills,
    or `<plugin>:<skill>` for plugin-sourced skills.
    `plugin_name` is None for `source=="repo"`.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    source: Literal["repo", "plugin"]
    plugin_name: str | None = None


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
            skills=[],
        )
        session.add(row)
        await session.flush()
    return row


async def persist_skill_manifest(
    org_id: UUID,
    repo_external_id: str,
    skills: list[SkillManifestEntry],
    *,
    session: AsyncSession,
) -> None:
    """Overwrite the cached skill manifest and stamp `enumerated_at`. Never commits."""
    row = await get_or_create_repo_row(org_id, repo_external_id, session=session)
    row.skills = [s.model_dump() for s in skills]
    row.enumerated_at = datetime.now(UTC)
    await session.flush()
    log.info(
        "claude_code.repos.skill_manifest_persisted",
        org_id=str(org_id),
        repo_external_id=repo_external_id,
        skill_count=len(skills),
    )


async def get_skill_manifest(
    org_id: UUID,
    repo_external_id: str,
    *,
    session: AsyncSession,
) -> list[SkillManifestEntry]:
    """Return the cached skill manifest. Empty list if not yet enumerated."""
    row = (
        await session.execute(
            select(ClaudeCodeRepoRow).where(
                ClaudeCodeRepoRow.org_id == org_id,
                ClaudeCodeRepoRow.repo_external_id == repo_external_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return []
    try:
        return [SkillManifestEntry.model_validate(entry) for entry in (row.skills or [])]
    except Exception:
        log.warning(
            "claude_code.repos.skill_manifest_parse_error",
            org_id=str(org_id),
            repo_external_id=repo_external_id,
        )
        return []
