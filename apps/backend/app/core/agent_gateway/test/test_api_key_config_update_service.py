"""Service tests — API key distribution to agents via ConfigUpdate.

Covers:
- `_build_config_update_dto` populates `api_keys` when an API key provider is registered.
- Wire JSON (model_dump(mode='json')) unwraps api_keys values to plaintext.
- Python model_dump stays redacted (SecretStr).
- `api_keys.set` triggers `enqueue_config_update_for_all_org_agents` for every
  configured agent in the org.
- `api_keys.clear` triggers a ConfigUpdate refresh with an empty api_keys dict.
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


# ── AgentConfig.api_keys wire shape ─────────────────────────────────────────


@pytest.mark.service
def test_agent_config_api_keys_redacted_in_python_mode() -> None:
    """`api_keys` values must appear as SecretStr (redacted) in Python model_dump."""
    config = AgentConfig(
        max_workspaces=2,
        api_keys={"anthropic": SecretStr("sk-real-key")},
    )
    py_dump = config.model_dump()
    val = py_dump["api_keys"]["anthropic"]
    # SecretStr renders as '**********' — must NOT be the raw key.
    assert str(val) != "sk-real-key", f"api_keys must be redacted in model_dump(); got {val}"


@pytest.mark.service
def test_agent_config_api_keys_plaintext_in_json_mode() -> None:
    """`api_keys` values must be plaintext strings in model_dump(mode='json')."""
    config = AgentConfig(
        max_workspaces=2,
        api_keys={"anthropic": SecretStr("sk-real-key")},
    )
    json_dump = config.model_dump(mode="json")
    assert json_dump["api_keys"]["anthropic"] == "sk-real-key", (
        f"api_keys must be plaintext in JSON mode; got {json_dump['api_keys']}"
    )


@pytest.mark.service
def test_agent_config_api_keys_empty_by_default() -> None:
    """`api_keys` defaults to an empty dict when not supplied."""
    config = AgentConfig(max_workspaces=1)
    assert config.api_keys == {}


# ── build_config_update_dto populates api_keys ──────────────────────────────


@pytest.mark.service
async def test_build_config_update_includes_api_keys(db_session) -> None:
    """`_build_config_update_dto` includes api_keys from the registered provider."""
    import app.core.agent_gateway.service as svc  # noqa: PLC0415
    from app.core.agent_gateway import (  # noqa: PLC0415
        clear_api_key_secrets_provider,
        register_api_key_secrets_provider,
    )

    org_id = await seed_org()

    async def fake_provider(oid, *, session):
        if oid == org_id:
            return {"anthropic": SecretStr("sk-api-key-test")}
        return {}

    # Clear the production provider (registered by coding_agent bootstrap)
    # and install the fake for this test.
    clear_api_key_secrets_provider()
    register_api_key_secrets_provider(fake_provider)
    try:
        cmd = await svc._build_config_update_dto(org_id, session=db_session)
        wire = cmd.config.model_dump(mode="json")
        assert wire["api_keys"].get("anthropic") == "sk-api-key-test", (
            f"Expected anthropic key in api_keys; got {wire['api_keys']}"
        )
    finally:
        clear_api_key_secrets_provider()


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


@pytest.mark.service
async def test_fan_out_resolves_per_org_inputs_once(db_session) -> None:
    """The fan-out path resolves the per-org `AgentConfig` (org row + API key
    secrets) exactly once and reuses it across every agent — not once per
    agent. Regression-locks the O(N)→O(1) collapse in
    `enqueue_config_update_for_all_org_agents`."""
    from app.core.agent_gateway import (  # noqa: PLC0415
        clear_api_key_secrets_provider,
        enqueue_config_update_for_all_org_agents,
        register_api_key_secrets_provider,
    )

    org_id = await seed_org()
    # Three agents so a per-agent call would inflate the counter to 3.
    await _make_agent(org_id=org_id)
    await _make_agent(org_id=org_id)
    await _make_agent(org_id=org_id)

    calls = 0

    async def counting_provider(oid, *, session):
        nonlocal calls
        calls += 1
        return {}

    clear_api_key_secrets_provider()
    register_api_key_secrets_provider(counting_provider)
    try:
        await enqueue_config_update_for_all_org_agents(org_id, session=db_session)
        await db_session.flush()
    finally:
        clear_api_key_secrets_provider()

    assert calls == 1, f"API key provider must be called exactly once per fan-out; got {calls}"


# ── api_keys.set triggers ConfigUpdate fan-out ──────────────────────────────


@pytest.mark.service
async def test_api_key_set_triggers_config_update_for_org_agents(db_session) -> None:
    """`api_keys.set` must enqueue a ConfigUpdate for every configured agent in the org."""
    import app.core.api_keys as api_keys  # noqa: PLC0415

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

    await api_keys.set(org_id, "anthropic", "sk-test", actor=Actor.system(), session=db_session)
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
        f"api_keys.set must enqueue 1 ConfigUpdate; before={before_count} after={after_count}"
    )


@pytest.mark.service
async def test_api_key_clear_triggers_config_update_for_org_agents(db_session) -> None:
    """`api_keys.clear` must enqueue a ConfigUpdate (key removed) for every configured agent."""
    import app.core.api_keys as api_keys  # noqa: PLC0415

    org_id = await _make_org(db_session)
    agent_id = await _make_agent(org_id=org_id)
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    # Set a key first.
    await api_keys.set(org_id, "anthropic", "sk-test", actor=Actor.system(), session=db_session)
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

    cleared = await api_keys.clear(org_id, "anthropic", actor=Actor.system(), session=db_session)
    await db_session.flush()

    assert cleared, "api_keys.clear must return True when a row was removed"
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
        f"api_keys.clear must enqueue 1 ConfigUpdate; before={before_count} after={after_count}"
    )


# ── ClaudeCodePlugin.compile_invocation no longer emits ANTHROPIC_API_KEY ─────


@pytest.mark.service
def test_compile_invocation_does_not_emit_anthropic_api_key() -> None:
    """`ClaudeCodePlugin.compile_invocation` must NOT put ANTHROPIC_API_KEY in env.

    Key delivery is exclusively via ConfigUpdate api_keys, never via the
    InvokeCodingAgent exec env.
    """
    from app.core.coding_agent import Invocation  # noqa: PLC0415
    from app.plugins.claude_code import ClaudeCodePlugin  # noqa: PLC0415

    inv = Invocation(
        workspace_id="00000000-0000-0000-0000-000000000099",
        skill="code-review",
        model="opus",
        effort="medium",
        context={
            "stage_name": "code-review",
            "input": "review the diff",
            "artifact_path": "/tmp/artifact.md",
        },
        wallclock_seconds=300,
    )
    result = ClaudeCodePlugin().compile_invocation(inv)
    assert "ANTHROPIC_API_KEY" not in result.env, (
        f"compile_invocation must NOT set ANTHROPIC_API_KEY in env; got env keys: {list(result.env.keys())}"
    )
