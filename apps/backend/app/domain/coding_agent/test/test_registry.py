"""Registry + dispatch tests for `domain/coding_agent`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.plugin_kit import PluginMeta
from app.domain.coding_agent import (
    HealthStatus,
    InvocationStatus,
    InvocationTelemetry,
    OnActivity,
    PluginNotFoundError,
    ReviewContext,
    ReviewResult,
    ValidationResult,
    get_plugin,
    health_check_all,
    list_registered_plugins,
    register_plugin,
    registered_plugin_ids,
    review,
    validate_config,
)
from app.domain.coding_agent.service import clear_plugins


class _StubPlugin:
    meta = PluginMeta(id="stub", type="coding_agent", display_name="Stub")

    async def review(
        self,
        workspace: Any,
        context: ReviewContext,
        on_activity: OnActivity | None = None,
    ) -> ReviewResult:
        del workspace, context, on_activity
        return ReviewResult(
            status=InvocationStatus.SUCCESS,
            findings=[],
            state="APPROVED",
            summary_body="reviewed",
            telemetry=InvocationTelemetry(tokens_in=1, tokens_out=2, latency_ms=5),
        )

    async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult:
        return ValidationResult(valid=True, errors=[])

    async def health_check(self) -> HealthStatus:
        return HealthStatus(healthy=True, message="ok", checked_at=datetime.now(UTC))


@pytest.fixture(autouse=True)
def _reset() -> None:
    clear_plugins()
    yield
    clear_plugins()


def test_register_and_get_plugin() -> None:
    plugin = _StubPlugin()
    register_plugin(plugin)
    assert get_plugin("stub") is plugin
    assert "stub" in registered_plugin_ids()


def test_register_duplicate_raises() -> None:
    register_plugin(_StubPlugin())
    with pytest.raises(ValueError, match="already registered"):
        register_plugin(_StubPlugin())


def test_get_unknown_plugin_raises() -> None:
    with pytest.raises(PluginNotFoundError):
        get_plugin("nope")


@pytest.mark.asyncio
async def test_review_dispatch() -> None:
    from app.domain.vcs import Diff, VCSPullRequest  # noqa: PLC0415

    register_plugin(_StubPlugin())

    pr = VCSPullRequest(
        plugin_id="github",
        external_id="acme/web#1",
        repo_external_id="acme/web",
        number=1,
        title="t",
        body=None,
        author_login="alice",
        author_type="user",
        base_branch="main",
        head_branch="feat",
        base_sha="b",
        head_sha="h",
        is_draft=False,
        is_fork=False,
        state="open",
        html_url="http://x",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    ctx = ReviewContext(
        pr=pr,
        diff=Diff(raw="", files=[]),
        lessons=[],
    )
    result = await review("stub", workspace=None, context=ctx)  # type: ignore[arg-type]
    assert result.status == InvocationStatus.SUCCESS
    assert result.state == "APPROVED"


@pytest.mark.asyncio
async def test_validate_config_dispatch() -> None:
    register_plugin(_StubPlugin())
    res = await validate_config("stub", {})
    assert res.valid is True


@pytest.mark.asyncio
async def test_health_check_all_handles_plugin_exception() -> None:
    class _Broken:
        meta = PluginMeta(id="broken", type="coding_agent", display_name="Broken")

        async def review(self, *a, **kw):
            raise NotImplementedError

        async def validate_config(self, *a, **kw):
            return ValidationResult(valid=True, errors=[])

        async def health_check(self) -> HealthStatus:
            raise RuntimeError("boom")

    register_plugin(_Broken())
    out = await health_check_all()
    assert out["broken"].healthy is False
    assert "boom" in out["broken"].message


def test_register_plugin_adds_and_is_retrievable() -> None:
    plugin = _StubPlugin()
    register_plugin(plugin)
    assert get_plugin("stub") is plugin
    assert "stub" in registered_plugin_ids()


def test_list_registered_plugins_returns_insertion_order() -> None:
    class _A:
        meta = PluginMeta(id="aaa", type="coding_agent", display_name="A")

    class _B:
        meta = PluginMeta(id="bbb", type="coding_agent", display_name="B")

    register_plugin(_A())
    register_plugin(_B())
    result = list_registered_plugins()
    assert [p.meta.id for p in result] == ["aaa", "bbb"]


def test_clear_plugins_empties_registry() -> None:
    register_plugin(_StubPlugin())
    assert len(list_registered_plugins()) == 1
    clear_plugins()
    assert list_registered_plugins() == []
    assert registered_plugin_ids() == []
