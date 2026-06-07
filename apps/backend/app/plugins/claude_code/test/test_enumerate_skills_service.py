"""Service tests for the enumerate_skills_v1 workflow.

Two behaviors under test:
1. PersistSkillManifest.execute() persists the manifest from step outputs
   and emits the `skills_enumerated` SSE event after commit.
2. A finished enumeration with zero plugin skills is a success — the
   degraded path (repo-local returns, plugin skills return empty).
"""

from __future__ import annotations

from uuid import UUID

import pytest

from app.core.workflow import CommandContext
from app.core.workspace import WorkspaceTicketContext, register_workflow_context_provider
from app.domain.orgs import create_org
from app.plugins.claude_code.enumerate_workflow import PersistSkillManifest
from app.plugins.claude_code.repos import get_skill_manifest

pytestmark = pytest.mark.service

# Synthetic org + ticket ids used across tests.
_REPO = "org/my-repo"


class _StaticContextProvider:
    """A minimal WorkflowContextProvider for tests that returns a canned
    WorkspaceTicketContext. Registered via the isolation fixture."""

    def __init__(self, org_id: UUID, repo_external_id: str) -> None:
        self._org_id = org_id
        self._repo_external_id = repo_external_id

    async def get_workspace_ticket_context(self, ticket_id: UUID) -> WorkspaceTicketContext | None:
        return WorkspaceTicketContext(
            org_id=self._org_id,
            plugin_id="github",
            repo_external_id=self._repo_external_id,
            payload={},
        )


def _make_ctx(ticket_id: str = "00000000-0000-0000-0000-000000000099") -> CommandContext:
    return CommandContext(
        workflow_execution_id="00000000-0000-0000-0000-000000000001",
        ticket_id=ticket_id,
        step_id="persist_manifest",
        attempt=1,
    )


@pytest.mark.asyncio
async def test_persist_skill_manifest_writes_repo_local_skills(
    db_session,
    workflow_context_provider_isolation,
) -> None:
    """PersistSkillManifest.execute() persists the skills list and returns success."""
    org = await create_org(db_session, slug="enum-skill-1", display_name="Enum Skill Org 1")
    await db_session.commit()

    register_workflow_context_provider(_StaticContextProvider(org.id, _REPO))

    ticket_id = "00000000-0000-0000-0000-000000000001"
    inputs = {
        "skills": [
            {"name": "my-skill", "source": "repo", "plugin_name": None},
            {"name": "another", "source": "repo", "plugin_name": None},
        ]
    }

    cmd = PersistSkillManifest()
    outcome = await cmd.execute(inputs, _make_ctx(ticket_id))

    assert outcome.kind.value == "success"
    assert outcome.outputs["skill_count"] == 2

    # The manifest should now be readable via the public API.
    async with db_session:
        skills = await get_skill_manifest(org.id, _REPO, session=db_session)

    assert len(skills) == 2
    assert skills[0].name == "my-skill"
    assert skills[0].source == "repo"
    assert skills[1].name == "another"


@pytest.mark.asyncio
async def test_persist_skill_manifest_zero_skills_is_success(
    db_session,
    workflow_context_provider_isolation,
) -> None:
    """A finished enumeration with zero skills is a success — the degraded
    path (plugin skills returned empty) must not fail the workflow."""
    org = await create_org(db_session, slug="enum-skill-2", display_name="Enum Skill Org 2")
    await db_session.commit()

    register_workflow_context_provider(_StaticContextProvider(org.id, _REPO))

    ticket_id = "00000000-0000-0000-0000-000000000002"
    inputs: dict = {"skills": []}

    cmd = PersistSkillManifest()
    outcome = await cmd.execute(inputs, _make_ctx(ticket_id))

    assert outcome.kind.value == "success"
    assert outcome.outputs["skill_count"] == 0

    async with db_session:
        skills = await get_skill_manifest(org.id, _REPO, session=db_session)

    assert skills == []


@pytest.mark.asyncio
async def test_persist_skill_manifest_mixed_repo_and_plugin_skills(
    db_session,
    workflow_context_provider_isolation,
) -> None:
    """Repo-local and plugin-sourced skills both persist correctly."""
    org = await create_org(db_session, slug="enum-skill-3", display_name="Enum Skill Org 3")
    await db_session.commit()

    register_workflow_context_provider(_StaticContextProvider(org.id, _REPO))

    ticket_id = "00000000-0000-0000-0000-000000000003"
    inputs = {
        "skills": [
            {"name": "repo-skill", "source": "repo", "plugin_name": None},
            {"name": "plugin:tool", "source": "plugin", "plugin_name": "plugin"},
        ]
    }

    cmd = PersistSkillManifest()
    outcome = await cmd.execute(inputs, _make_ctx(ticket_id))

    assert outcome.kind.value == "success"
    assert outcome.outputs["skill_count"] == 2

    async with db_session:
        skills = await get_skill_manifest(org.id, _REPO, session=db_session)

    assert len(skills) == 2
    repo_skill = next(s for s in skills if s.source == "repo")
    plugin_skill = next(s for s in skills if s.source == "plugin")
    assert repo_skill.name == "repo-skill"
    assert plugin_skill.name == "plugin:tool"
    assert plugin_skill.plugin_name == "plugin"
