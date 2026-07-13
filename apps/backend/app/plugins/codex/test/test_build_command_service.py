"""Service tests for `CodexPlugin.build_command`.

Covers the org-level OpenAI-key gate (via `_require_org_openai_key`) and the
`output_schema_json` normalization (str passthrough / dict → json.dumps /
absent → None). `build_command` reads the schema from `invocation.context` —
the vendor-neutral `InvokeCodingAgent` carries no schema field at all.

No unittest.mock.patch — behaviour is driven by DB state and injected callables.
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4, uuid7

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import InvokeCodexCommand
from app.core.audit_log import Actor
from app.core.coding_agent import (
    CommandBuildContext,
    CredentialUnavailableError,
    Invocation,
    InvokeCodingAgent,
    install_coding_agent,
)
from app.core.tenancy import create_org
from app.plugins.codex.service import CodexPlugin

pytestmark = [pytest.mark.service, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def org_id(db_session: AsyncSession) -> UUID:
    """Seed a fresh org with codex installed but no OpenAI key."""
    org = await create_org(db_session, slug=f"codex-build-{uuid4().hex[:8]}", display_name="Codex Build Org")
    import app.plugins.codex  # noqa: PLC0415 — triggers bootstrap

    _ = app.plugins.codex  # ensure import side-effect runs
    await install_coding_agent(
        db_session,
        org_id=org.org_id,
        plugin_id="codex",
        settings={},
        actor=Actor.system(),
    )
    await db_session.flush()
    return org.org_id


def _plugin() -> CodexPlugin:
    return CodexPlugin()


def _invocation(*, output_schema: object | None = None) -> Invocation:
    context: dict[str, object] = {}
    if output_schema is not None:
        context["output_schema"] = output_schema
    return Invocation(
        workspace_id=uuid7(),
        skill="requirements",
        model="gpt-5-codex",
        effort="medium",
        context=context,
        wallclock_seconds=300,
    )


def _compiled(*, wallclock_seconds: int = 300) -> InvokeCodingAgent:
    return InvokeCodingAgent(
        argv=["codex", "exec"],
        env={},
        stdin="prompt body",
        wallclock_seconds=wallclock_seconds,
    )


def _build_context(*, org_id: UUID) -> CommandBuildContext:
    return CommandBuildContext(
        command_id=uuid7(),
        workspace_id=uuid7(),
        traceparent="00-trace-01",
        org_id=org_id,
        user_id=None,
        skill_path=".codex/skills/requirements/SKILL.md",
        invocation_body={"exec": {"argv": ["codex", "exec"], "stdin": "prompt body", "env": {}}},
    )


async def test_no_key_raises_credential_unavailable(db_session: AsyncSession, org_id: UUID) -> None:
    with pytest.raises(CredentialUnavailableError, match="No OpenAI API key"):
        await _plugin().build_command(
            compiled=_compiled(),
            invocation=_invocation(),
            build=_build_context(org_id=org_id),
            session=db_session,
        )


async def test_key_present_returns_invoke_codex_command(db_session: AsyncSession, org_id: UUID) -> None:
    import app.core.api_keys as api_keys  # noqa: PLC0415

    await api_keys.set(org_id, "openai", "sk-test-openai-key", actor=Actor.system(), session=db_session)
    await db_session.flush()

    build = _build_context(org_id=org_id)
    result = await _plugin().build_command(
        compiled=_compiled(wallclock_seconds=555),
        invocation=_invocation(),
        build=build,
        session=db_session,
    )

    assert isinstance(result, InvokeCodexCommand)
    assert result.command_id == build.command_id
    assert result.workspace_id == build.workspace_id
    assert result.traceparent == build.traceparent
    assert result.invocation == build.invocation_body
    assert result.skill_path == build.skill_path
    assert result.limits.wallclock_seconds == 555
    assert result.result_spec == {}


async def test_output_schema_str_passthrough(db_session: AsyncSession, org_id: UUID) -> None:
    import app.core.api_keys as api_keys  # noqa: PLC0415

    await api_keys.set(org_id, "openai", "sk-test-openai-key", actor=Actor.system(), session=db_session)
    await db_session.flush()

    result = await _plugin().build_command(
        compiled=_compiled(),
        invocation=_invocation(output_schema='{"type": "object"}'),
        build=_build_context(org_id=org_id),
        session=db_session,
    )

    assert result.output_schema_json == '{"type": "object"}'


async def test_output_schema_dict_json_dumps(db_session: AsyncSession, org_id: UUID) -> None:
    import app.core.api_keys as api_keys  # noqa: PLC0415

    await api_keys.set(org_id, "openai", "sk-test-openai-key", actor=Actor.system(), session=db_session)
    await db_session.flush()

    schema = {"type": "object", "properties": {"outcome": {"type": "string"}}}
    result = await _plugin().build_command(
        compiled=_compiled(),
        invocation=_invocation(output_schema=schema),
        build=_build_context(org_id=org_id),
        session=db_session,
    )

    assert result.output_schema_json == json.dumps(schema)


async def test_output_schema_absent_is_none(db_session: AsyncSession, org_id: UUID) -> None:
    import app.core.api_keys as api_keys  # noqa: PLC0415

    await api_keys.set(org_id, "openai", "sk-test-openai-key", actor=Actor.system(), session=db_session)
    await db_session.flush()

    result = await _plugin().build_command(
        compiled=_compiled(),
        invocation=_invocation(),
        build=_build_context(org_id=org_id),
        session=db_session,
    )

    assert result.output_schema_json is None
