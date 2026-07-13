"""`ClaudeCodePlugin.build_command` — constructs the wire `InvokeClaudeCodeCommand`
from a `CommandBuildContext` + the compiled exec block.

No credential gating for claude — the Anthropic API key travels via
`ConfigUpdate.api_keys`, not a dispatch-time check.
"""

from __future__ import annotations

from uuid import uuid7

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agent_gateway import InvokeClaudeCodeCommand
from app.core.coding_agent import CommandBuildContext, Invocation, InvokeCodingAgent
from app.plugins.claude_code.service import ClaudeCodePlugin

pytestmark = [pytest.mark.asyncio]


def _plugin() -> ClaudeCodePlugin:
    return ClaudeCodePlugin()


def _invocation() -> Invocation:
    return Invocation(
        workspace_id=uuid7(),
        skill="requirements",
        model="opus",
        effort="medium",
        context={},
        wallclock_seconds=300,
    )


def _compiled(*, wallclock_seconds: int = 300) -> InvokeCodingAgent:
    return InvokeCodingAgent(
        argv=["claude", "--print"],
        env={},
        stdin="prompt body",
        wallclock_seconds=wallclock_seconds,
    )


def _build_context(*, invocation_body: dict | None = None) -> CommandBuildContext:
    return CommandBuildContext(
        command_id=uuid7(),
        workspace_id=uuid7(),
        traceparent="00-trace-01",
        org_id=uuid7(),
        user_id=uuid7(),
        skill_path=".claude/skills/requirements/SKILL.md",
        invocation_body=invocation_body if invocation_body is not None else {"exec": {"argv": ["claude"]}},
    )


async def test_returns_invoke_claude_code_command(db_session: AsyncSession) -> None:
    build = _build_context()
    result = await _plugin().build_command(
        compiled=_compiled(),
        invocation=_invocation(),
        build=build,
        session=db_session,
    )
    assert isinstance(result, InvokeClaudeCodeCommand)


async def test_envelope_fields_come_from_build_context(db_session: AsyncSession) -> None:
    build = _build_context(invocation_body={"exec": {"argv": ["claude", "--print"], "stdin": "", "env": {}}})
    result = await _plugin().build_command(
        compiled=_compiled(),
        invocation=_invocation(),
        build=build,
        session=db_session,
    )
    assert result.command_id == build.command_id
    assert result.workspace_id == build.workspace_id
    assert result.traceparent == build.traceparent
    assert result.skill_path == build.skill_path
    assert result.invocation == build.invocation_body


async def test_limits_wallclock_seconds_from_compiled(db_session: AsyncSession) -> None:
    build = _build_context()
    result = await _plugin().build_command(
        compiled=_compiled(wallclock_seconds=777),
        invocation=_invocation(),
        build=build,
        session=db_session,
    )
    assert result.limits.wallclock_seconds == 777


async def test_mcp_servers_empty(db_session: AsyncSession) -> None:
    build = _build_context()
    result = await _plugin().build_command(
        compiled=_compiled(),
        invocation=_invocation(),
        build=build,
        session=db_session,
    )
    assert result.mcp_servers == ()


async def test_result_spec_empty(db_session: AsyncSession) -> None:
    build = _build_context()
    result = await _plugin().build_command(
        compiled=_compiled(),
        invocation=_invocation(),
        build=build,
        session=db_session,
    )
    assert result.result_spec == {}
