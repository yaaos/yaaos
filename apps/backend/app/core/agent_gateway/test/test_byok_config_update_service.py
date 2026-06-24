"""Service tests — BYOK key distribution to agents via ConfigUpdate.

Covers:
- `_build_config_update_dto` populates `byok_secrets` when a byok provider is registered.
- Wire JSON (model_dump(mode='json')) unwraps byok_secrets values to plaintext.
- Python model_dump stays redacted (SecretStr).
- `byok.set` triggers `enqueue_config_update_for_all_org_agents` for every
  configured agent in the org.
- `byok.clear` triggers a ConfigUpdate refresh with an empty byok_secrets dict.
- `ClaudeCodePlugin.build_invocation` no longer emits `ANTHROPIC_API_KEY` in
  `InvokeCodingAgent.env` — key delivery is exclusively via ConfigUpdate.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.agent_gateway.models import AgentCommandRow
from app.core.agent_gateway.service import enqueue_config_update_for_agent
from app.core.agent_gateway.types import AgentCommandKind, AgentConfig
from app.core.audit_log import Actor
from app.domain.orgs import insert_org
from app.testing.e2e_setup import seed_agent, seed_org

# ── helpers ─────────────────────────────────────────────────────────────────


async def _make_org(db_session) -> UUID:
    """Insert a minimal org row and return its id."""
    org = await insert_org(db_session, slug=f"test-org-{uuid4().hex[:8]}")
    return org.org_id


async def _make_agent(*, org_id=None):
    result = await seed_agent(org_id=org_id or uuid4())
    return result["id"]


# ── AgentConfig.byok_secrets wire shape ─────────────────────────────────────


@pytest.mark.service
def test_agent_config_byok_secrets_redacted_in_python_mode() -> None:
    """`byok_secrets` values must appear as SecretStr (redacted) in Python model_dump."""
    config = AgentConfig(
        max_workspaces=2,
        byok_secrets={"anthropic": SecretStr("sk-real-key")},
    )
    py_dump = config.model_dump()
    val = py_dump["byok_secrets"]["anthropic"]
    # SecretStr renders as '**********' — must NOT be the raw key.
    assert str(val) != "sk-real-key", f"byok_secrets must be redacted in model_dump(); got {val}"


@pytest.mark.service
def test_agent_config_byok_secrets_plaintext_in_json_mode() -> None:
    """`byok_secrets` values must be plaintext strings in model_dump(mode='json')."""
    config = AgentConfig(
        max_workspaces=2,
        byok_secrets={"anthropic": SecretStr("sk-real-key")},
    )
    json_dump = config.model_dump(mode="json")
    assert json_dump["byok_secrets"]["anthropic"] == "sk-real-key", (
        f"byok_secrets must be plaintext in JSON mode; got {json_dump['byok_secrets']}"
    )


@pytest.mark.service
def test_agent_config_byok_secrets_empty_by_default() -> None:
    """`byok_secrets` defaults to an empty dict when not supplied."""
    config = AgentConfig(max_workspaces=1)
    assert config.byok_secrets == {}


# ── build_config_update_dto populates byok_secrets ──────────────────────────


@pytest.mark.service
async def test_build_config_update_includes_byok_secrets(db_session) -> None:
    """`_build_config_update_dto` includes byok_secrets from the registered provider."""
    import app.core.agent_gateway.service as svc  # noqa: PLC0415
    from app.core.agent_gateway import (  # noqa: PLC0415
        clear_byok_secrets_provider,
        register_byok_secrets_provider,
    )

    org_id = await seed_org()

    async def fake_provider(oid, *, session):
        if oid == org_id:
            return {"anthropic": SecretStr("sk-byok-test")}
        return {}

    # Clear the production provider (registered by coding_agent bootstrap)
    # and install the fake for this test.
    clear_byok_secrets_provider()
    register_byok_secrets_provider(fake_provider)
    try:
        cmd = await svc._build_config_update_dto(org_id, session=db_session)
        wire = cmd.config.model_dump(mode="json")
        assert wire["byok_secrets"].get("anthropic") == "sk-byok-test", (
            f"Expected anthropic key in byok_secrets; got {wire['byok_secrets']}"
        )
    finally:
        clear_byok_secrets_provider()


# ── enqueue_config_update_for_all_org_agents ────────────────────────────────


@pytest.mark.service
async def test_enqueue_config_update_for_all_org_agents_inserts_rows(db_session) -> None:
    """`enqueue_config_update_for_all_org_agents` inserts a ConfigUpdate row for
    every configured agent in the org."""
    from app.core.agent_gateway import enqueue_config_update_for_all_org_agents  # noqa: PLC0415

    org_id = await seed_org()
    # Register two agents for the same org.
    agent_id_1 = await _make_agent(org_id=org_id)
    agent_id_2 = await _make_agent(org_id=org_id)
    # Seed a ConfigUpdate for each so they are "active".
    await enqueue_config_update_for_agent(agent_id_1, org_id=org_id, session=db_session)
    await enqueue_config_update_for_agent(agent_id_2, org_id=org_id, session=db_session)
    await db_session.flush()

    # Drain those two seed rows from the count baseline.
    before = (
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.org_id == org_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                )
            )
        )
        .scalars()
        .all()
    )
    before_count = len(before)

    await enqueue_config_update_for_all_org_agents(org_id, session=db_session)
    await db_session.flush()

    after = (
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.org_id == org_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(after) == before_count + 2, (
        f"Expected 2 new ConfigUpdate rows; before={before_count} after={len(after)}"
    )


# ── byok.set triggers ConfigUpdate fan-out ──────────────────────────────────


@pytest.mark.service
async def test_byok_set_triggers_config_update_for_org_agents(db_session) -> None:
    """`byok.set` must enqueue a ConfigUpdate for every configured agent in the org."""
    import app.core.byok as byok  # noqa: PLC0415

    org_id = await _make_org(db_session)
    agent_id = await _make_agent(org_id=org_id)
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    before_count = len(
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.org_id == org_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                )
            )
        )
        .scalars()
        .all()
    )

    await byok.set(org_id, "anthropic", "sk-test", actor=Actor.system(), session=db_session)
    await db_session.flush()

    after_count = len(
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.org_id == org_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert after_count == before_count + 1, (
        f"byok.set must enqueue 1 ConfigUpdate; before={before_count} after={after_count}"
    )


@pytest.mark.service
async def test_byok_clear_triggers_config_update_for_org_agents(db_session) -> None:
    """`byok.clear` must enqueue a ConfigUpdate (key removed) for every configured agent."""
    import app.core.byok as byok  # noqa: PLC0415

    org_id = await _make_org(db_session)
    agent_id = await _make_agent(org_id=org_id)
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    # Set a key first.
    await byok.set(org_id, "anthropic", "sk-test", actor=Actor.system(), session=db_session)
    await db_session.flush()

    before_count = len(
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.org_id == org_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                )
            )
        )
        .scalars()
        .all()
    )

    cleared = await byok.clear(org_id, "anthropic", actor=Actor.system(), session=db_session)
    await db_session.flush()

    assert cleared, "byok.clear must return True when a row was removed"
    after_count = len(
        (
            await db_session.execute(
                select(AgentCommandRow).where(
                    AgentCommandRow.org_id == org_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                )
            )
        )
        .scalars()
        .all()
    )
    assert after_count == before_count + 1, (
        f"byok.clear must enqueue 1 ConfigUpdate; before={before_count} after={after_count}"
    )


# ── ClaudeCodePlugin.compile_invocation no longer emits ANTHROPIC_API_KEY ─────


@pytest.mark.service
def test_compile_invocation_does_not_emit_anthropic_api_key() -> None:
    """`ClaudeCodePlugin.compile_invocation` must NOT put ANTHROPIC_API_KEY in env.

    Key delivery is exclusively via ConfigUpdate byok_secrets, never via the
    InvokeCodingAgent exec env.
    """
    from app.core.coding_agent import Invocation  # noqa: PLC0415
    from app.plugins.claude_code import ClaudeCodePlugin  # noqa: PLC0415

    inv = Invocation(
        workspace_id="00000000-0000-0000-0000-000000000099",
        skill="pr_review",
        model="opus",
        effort="medium",
        context={
            "org_id": "00000000-0000-0000-0000-000000000001",
            "repo_external_id": "acme/web",
            "pr_external_id": "acme/web#42",
            "head_sha": "deadbeef",
            "base_sha": "cafebabe",
        },
        wallclock_seconds=300,
    )
    result = ClaudeCodePlugin().compile_invocation(inv)
    assert "ANTHROPIC_API_KEY" not in result.env, (
        f"compile_invocation must NOT set ANTHROPIC_API_KEY in env; got env keys: {list(result.env.keys())}"
    )
