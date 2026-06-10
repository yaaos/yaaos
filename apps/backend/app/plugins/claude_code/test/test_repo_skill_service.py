"""Service tests for per-repo skill name: resolve_skill + build_review_invocation."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from app.core.coding_agent import CodingAgentError, ReviewContext
from app.domain.orgs import create_org
from app.plugins.claude_code import resolve_skill, set_repo_skill
from app.plugins.claude_code.service import ClaudeCodePlugin

# ── resolve_skill round-trip ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.service
async def test_resolve_skill_returns_none_when_row_absent(db_session) -> None:
    """Repo with no row returns None."""
    org = await create_org(db_session, slug="skill-org-absent", display_name="Skill Org Absent")
    await db_session.commit()

    result = await resolve_skill(org.id, "acme/no-repo", session=db_session)
    assert result is None


@pytest.mark.asyncio
@pytest.mark.service
async def test_resolve_skill_returns_none_when_skill_name_not_set(db_session) -> None:
    """Row exists but skill_name is null → None."""
    org = await create_org(db_session, slug="skill-org-null", display_name="Skill Org Null")
    await set_repo_skill(org.id, "acme/my-repo", None, session=db_session)
    await db_session.commit()

    result = await resolve_skill(org.id, "acme/my-repo", session=db_session)
    assert result is None


@pytest.mark.asyncio
@pytest.mark.service
async def test_resolve_skill_returns_configured_value(db_session) -> None:
    """set_repo_skill + resolve_skill round-trip returns the stored name."""
    org = await create_org(db_session, slug="skill-org-set", display_name="Skill Org Set")
    await set_repo_skill(org.id, "acme/my-repo", "code-review", session=db_session)
    await db_session.commit()

    result = await resolve_skill(org.id, "acme/my-repo", session=db_session)
    assert result == "code-review"


@pytest.mark.asyncio
@pytest.mark.service
async def test_resolve_skill_updates_existing_row(db_session) -> None:
    """Second call to set_repo_skill overwrites the previous value."""
    org = await create_org(db_session, slug="skill-org-update", display_name="Skill Org Update")
    await set_repo_skill(org.id, "acme/my-repo", "old-skill", session=db_session)
    await db_session.commit()
    await set_repo_skill(org.id, "acme/my-repo", "new-skill", session=db_session)
    await db_session.commit()

    result = await resolve_skill(org.id, "acme/my-repo", session=db_session)
    assert result == "new-skill"


# ── build_review_invocation — None/empty → raises ────────────────────────────


def _make_mock_session_with_skill(skill_name: str | None):
    """Return a fake AsyncSession whose execute returns a scalar skill_name."""
    session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = skill_name
    session.execute = AsyncMock(return_value=mock_result)
    return session


@pytest.mark.asyncio
async def test_build_review_invocation_raises_when_skill_name_is_none() -> None:
    """build_review_invocation raises CodingAgentError when resolve_skill returns None."""
    plugin = ClaudeCodePlugin()
    ctx = ReviewContext(
        org_id=uuid.uuid4(),
        repo_external_id="acme/no-skill-repo",
        pr_external_id="101",
        head_sha="abc123",
        base_sha="def456",
    )
    fake_session = _make_mock_session_with_skill(None)
    with pytest.raises(CodingAgentError, match="skill_name not configured"):
        await plugin.build_review_invocation(ctx, session=fake_session)


@pytest.mark.asyncio
async def test_build_review_invocation_raises_when_skill_name_is_empty_string() -> None:
    """Empty string skill name is treated as unconfigured."""
    plugin = ClaudeCodePlugin()
    ctx = ReviewContext(
        org_id=uuid.uuid4(),
        repo_external_id="acme/empty-skill-repo",
        pr_external_id="102",
        head_sha="abc123",
        base_sha="def456",
    )
    fake_session = _make_mock_session_with_skill("")
    with pytest.raises(CodingAgentError, match="skill_name not configured"):
        await plugin.build_review_invocation(ctx, session=fake_session)


# ── build_review_invocation — resolved skill name used as kind ────────────────


class _FakePlugin(ClaudeCodePlugin):
    """Subclass that overrides `_load_settings_for_invocation` to return a fake key."""

    async def _load_settings_for_invocation(self, org_id):  # type: ignore[override]
        return SecretStr("sk-ant-fake-key"), None


@pytest.mark.asyncio
async def test_build_review_invocation_uses_resolved_skill_not_constant() -> None:
    """build_review_invocation sets Invocation.kind to the resolved skill name.

    Uses a subclass to inject a fake API key (DI, no patch). The mock session
    returns a non-empty skill name so the method doesn't raise before building.
    """
    plugin = _FakePlugin()
    ctx = ReviewContext(
        org_id=uuid.uuid4(),
        repo_external_id="acme/skill-set-repo",
        pr_external_id="103",
        head_sha="abc123",
        base_sha="def456",
    )
    fake_session = _make_mock_session_with_skill("my-custom-skill")
    invocation = await plugin.build_review_invocation(ctx, session=fake_session)

    # The Invocation.kind must be the resolved skill name, not the old constant.
    assert invocation.kind == "my-custom-skill"
    assert invocation.kind != "code-review"


# ── build_review_invocation — remote exec spec, no local subprocess ───────────


@pytest.mark.asyncio
async def test_build_review_invocation_returns_exec_spec_for_remote_agent() -> None:
    """build_review_invocation produces an Invocation with a populated ExecSpec.

    The ExecSpec is the contract the remote WorkspaceAgent executes.  This
    test confirms the review path always uses the remote-dispatch model: the
    returned object carries argv + env + stdin for the agent to run, not a
    local subprocess call.  There is no in-process `review()` method to call.
    """
    plugin = _FakePlugin()
    ctx = ReviewContext(
        org_id=uuid.uuid4(),
        repo_external_id="acme/remote-repo",
        pr_external_id="200",
        head_sha="headsha",
        base_sha="basesha",
    )
    fake_session = _make_mock_session_with_skill("code-review")
    invocation = await plugin.build_review_invocation(ctx, session=fake_session)

    # Remote-dispatch contract: exec spec must be populated.
    assert invocation.exec is not None, "exec spec must be set for remote-agent dispatch"
    assert invocation.exec.argv, "exec argv must be non-empty"
    # argv[0] is the binary path — it must not be an absolute local path
    # (which would only work if the binary existed on the backend host).
    # It is always just the bare "claude" name, resolved by the agent.
    assert "/" not in invocation.exec.argv[0], (
        "argv[0] must be a bare binary name resolved by the remote agent, not a local path"
    )
    assert invocation.exec.env.get("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY must be set in remote exec env"
    assert invocation.exec.stdin, "stdin (the prompt) must be set for the remote agent"
    # No attribute named `review` exists on the plugin class.
    assert not hasattr(plugin, "review"), "in-process review() method must not exist"
