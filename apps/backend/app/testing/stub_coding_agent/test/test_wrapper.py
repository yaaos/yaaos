"""Stub wrapper tests — no DB, no subprocess, no env."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.core.plugin_kit import PluginMeta
from app.domain.coding_agent import (
    CodingAgentRegistry,
    HealthStatus,
    InvocationStatus,
    ReviewContext,
    ReviewResult,
    ValidationResult,
    bind_coding_agent_registry,
    list_registered_plugins,
    register_plugin,
)
from app.testing.stub_coding_agent import (
    StubCodingAgentPlugin,
    wrap_all_registered_plugins,
)


class _DummyPlugin:
    meta = PluginMeta(id="dummy", type="coding_agent", display_name="Dummy")

    async def review(self, *args, **kwargs) -> ReviewResult:
        raise AssertionError("real review must not be called when wrapped")

    async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult:
        return ValidationResult(valid=True, errors=[])

    async def health_check(self) -> HealthStatus:
        return HealthStatus(healthy=True, message="real ok", checked_at=datetime.now(UTC))


class _FakeWorkspace:
    id = "fake"

    async def info(self):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def run_coding_agent_cli(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("workspace must not be reached in stub mode")


@pytest.mark.asyncio
async def test_review_returns_canned_success() -> None:
    stub = StubCodingAgentPlugin(wrapped=_DummyPlugin())
    ctx = ReviewContext(
        org_id=UUID(int=1),
        repo_external_id="acme/web",
        pr_external_id="acme/web#1",
        head_sha="h",
        base_sha="b",
    )
    result = await stub.review(_FakeWorkspace(), ctx)
    assert result.status == InvocationStatus.SUCCESS
    assert result.state == "COMMENT"
    # One synthetic finding lets UI specs exercise the finding-expansion
    # and Teach-yaaos flow without needing a real LLM. See service.review.
    assert len(result.findings) == 1
    # ReviewResult.findings carries canonical ReportedFinding shape.
    assert result.findings[0].file == "src/example.ts"
    assert result.findings[0].rule_violated == "stub/sample-suggestion"
    assert result.telemetry.tokens_in == 1000


@pytest.mark.asyncio
async def test_validate_config_passes_through() -> None:
    stub = StubCodingAgentPlugin(wrapped=_DummyPlugin())
    res = await stub.validate_config({})
    assert res.valid is True


@pytest.mark.asyncio
async def test_health_check_always_healthy_in_stub_mode() -> None:
    stub = StubCodingAgentPlugin(wrapped=_DummyPlugin())
    h = await stub.health_check()
    assert h.healthy is True
    assert "stub" in h.message.lower()


def test_meta_mirrors_wrapped() -> None:
    stub = StubCodingAgentPlugin(wrapped=_DummyPlugin())
    assert stub.meta.id == "dummy"
    assert stub.meta.display_name == "Dummy"


def test_wrap_all_is_idempotent() -> None:
    # Start from an empty registry so the test is independent of suite state.
    bind_coding_agent_registry(CodingAgentRegistry())
    dummy = _DummyPlugin()
    register_plugin(dummy)
    assert wrap_all_registered_plugins() == 1
    plugins = list_registered_plugins()
    assert len(plugins) == 1
    assert isinstance(plugins[0], StubCodingAgentPlugin)
    # second call is a no-op — already wrapped
    assert wrap_all_registered_plugins() == 0
