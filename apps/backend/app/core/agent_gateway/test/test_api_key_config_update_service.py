"""Service tests — API key distribution to agents via ConfigUpdate.

Covers:
- `AgentConfig.api_keys` wire shape: SecretStr at Python boundary, plaintext in
  JSON mode (unchanged by the hydration rework).
- Persisted ConfigUpdate rows have `api_keys == {}` — credentials never stored at rest.
- `claim_next` delivers a ConfigUpdate DTO with real api_keys injected by the
  registered ConfigUpdate hydrator (claim-time credential hydration).
- `enqueue_config_update_for_all_org_agents` inserts a row per agent.
- The fan-out path does NOT call the api_key provider — credentials are resolved
  at claim time, not enqueue time (provider call count stays 0 during fan-out).
- `api_keys.set` triggers `enqueue_config_update_for_all_org_agents`.
- `api_keys.clear` triggers a ConfigUpdate refresh.
- `ClaudeCodePlugin.compile_invocation` does not emit `ANTHROPIC_API_KEY` in env.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.agent_gateway.models import AgentCommandRow
from app.core.agent_gateway.service import claim_next, enqueue_config_update_for_agent
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


# ── Credentials absent at rest, present in the claimed DTO ──────────────────


@pytest.mark.service
async def test_config_update_persisted_payload_has_no_api_keys(db_session) -> None:
    """The persisted ConfigUpdate row must have `config.api_keys == {}`.

    Credentials are never stored in `agent_commands.payload` — they are
    injected by the ConfigUpdate hydrator at claim time.
    """
    import app.core.api_keys as api_keys  # noqa: PLC0415

    org_id = await _make_org(db_session)
    agent_id = await _make_agent(org_id=org_id)

    # Set a real API key so there would be something to embed if the old path
    # were still active.
    await api_keys.set(org_id, "anthropic", "sk-secret-test", actor=Actor.system(), session=db_session)
    await db_session.flush()

    # Enqueue a ConfigUpdate — after the credential-hygiene rework this must
    # persist api_keys={} regardless of what keys are stored for the org.
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    rows = (
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
    assert rows, "Expected at least one ConfigUpdate row"
    for row in rows:
        persisted_api_keys = (row.payload or {}).get("config", {}).get("api_keys", {})
        assert persisted_api_keys == {}, (
            f"Persisted payload must have api_keys={{}}; got {persisted_api_keys!r}"
        )


@pytest.mark.service
async def test_claimed_config_update_dto_carries_api_keys(db_session) -> None:
    """The DTO returned by `claim_next` must carry the real api_keys in
    `config.api_keys` — injected at claim time by the ConfigUpdate hydrator,
    NOT stored in the persisted row.

    Both halves of the invariant are asserted in the same test body:
    - Persisted row: `api_keys == {}`.
    - Claimed DTO: `api_keys == {"anthropic": "sk-secret-test"}` (plaintext in
      JSON mode via `@field_serializer(when_used="json")`).
    """
    import app.core.api_keys as api_keys  # noqa: PLC0415

    org_id = await _make_org(db_session)
    agent_id = await _make_agent(org_id=org_id)

    await api_keys.set(org_id, "anthropic", "sk-secret-test", actor=Actor.system(), session=db_session)
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    # Half 1: persisted payload has no api_keys.
    row = (
        (
            await db_session.execute(
                select(AgentCommandRow)
                .where(
                    AgentCommandRow.org_id == org_id,
                    AgentCommandRow.command_kind == AgentCommandKind.CONFIG_UPDATE,
                    AgentCommandRow.status == "pending",
                )
                .order_by(AgentCommandRow.id)
                .limit(1)
            )
        )
        .scalars()
        .one_or_none()
    )
    assert row is not None
    assert (row.payload or {}).get("config", {}).get("api_keys", {}) == {}, (
        "Persisted payload must have api_keys={}"
    )

    # Half 2: claim returns a DTO with the real api_keys hydrated in.
    cmd = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert cmd is not None, "Expected a claimed ConfigUpdate command"
    from app.core.agent_gateway.types import ConfigUpdateCommand  # noqa: PLC0415

    assert isinstance(cmd, ConfigUpdateCommand)
    wire = cmd.config.model_dump(mode="json")
    assert wire["api_keys"].get("anthropic") == "sk-secret-test", (
        f"Claimed DTO must carry hydrated api_keys; got {wire['api_keys']}"
    )


@pytest.mark.service
async def test_claimed_config_update_dto_has_empty_api_keys_when_key_cleared(db_session) -> None:
    """When the API key has been cleared, `claim_next` delivers `api_keys == {}`."""
    import app.core.api_keys as api_keys  # noqa: PLC0415

    org_id = await _make_org(db_session)
    agent_id = await _make_agent(org_id=org_id)

    # Set then immediately clear so no key is stored at claim time.
    await api_keys.set(org_id, "anthropic", "sk-secret-temp", actor=Actor.system(), session=db_session)
    await api_keys.clear(org_id, "anthropic", actor=Actor.system(), session=db_session)
    await enqueue_config_update_for_agent(agent_id, org_id=org_id, session=db_session)
    await db_session.flush()

    cmd = await claim_next(
        agent_id,
        lifecycle="unconfigured",
        new_workspaces=0,
        workspace_ids=[],
        wait_seconds=0,
        session=db_session,
    )
    assert cmd is not None
    from app.core.agent_gateway.types import ConfigUpdateCommand  # noqa: PLC0415

    assert isinstance(cmd, ConfigUpdateCommand)
    wire = cmd.config.model_dump(mode="json")
    assert wire["api_keys"] == {}, f"Cleared key must not appear in claimed DTO; got {wire['api_keys']}"


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
async def test_fan_out_does_not_call_api_key_provider(db_session) -> None:
    """The fan-out path must NOT call the api_key provider.

    Credentials are now resolved at claim time by the ConfigUpdate hydrator,
    not at enqueue / fan-out time.  This ensures the persisted rows are
    credential-free; the provider is O(1) per claim, not O(N) per fan-out.
    """
    from app.core.agent_gateway import (  # noqa: PLC0415
        clear_api_key_secrets_provider,
        enqueue_config_update_for_all_org_agents,
        get_api_key_secrets_provider,
        register_api_key_secrets_provider,
    )

    org_id = await seed_org()
    await _make_agent(org_id=org_id)
    await _make_agent(org_id=org_id)
    await _make_agent(org_id=org_id)

    calls = 0

    async def counting_provider(oid, *, session):
        nonlocal calls
        calls += 1
        return {}

    original_provider = get_api_key_secrets_provider()
    clear_api_key_secrets_provider()
    register_api_key_secrets_provider(counting_provider)
    try:
        await enqueue_config_update_for_all_org_agents(org_id, session=db_session)
        await db_session.flush()
    finally:
        # Restore the original provider so subsequent tests see it.
        clear_api_key_secrets_provider()
        if original_provider is not None:
            register_api_key_secrets_provider(original_provider)

    assert calls == 0, (
        f"API key provider must NOT be called during fan-out (claim-time hydration only); got {calls} call(s)"
    )


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
