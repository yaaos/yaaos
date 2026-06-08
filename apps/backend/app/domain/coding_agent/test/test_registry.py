"""Registry + dispatch tests for `domain/coding_agent`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.domain.coding_agent import (
    CodingAgentRegistry,
    HealthStatus,
    InvocationStatus,
    InvocationTelemetry,
    OnActivity,
    PluginNotFoundError,
    ReviewContext,
    ReviewResult,
    ValidationResult,
    bind_coding_agent_registry,
    get_plugin,
    health_check_all,
    list_registered_plugins,
    register_plugin,
    registered_plugin_ids,
    review,
    validate_config,
)


class _StubPlugin:
    plugin_id = "stub"

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
def _fresh_registry():
    """Bind a clean CodingAgentRegistry before each test so registrations don't
    bleed across tests."""
    bind_coding_agent_registry(CodingAgentRegistry())
    yield


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
    from uuid import UUID as _UUID  # noqa: PLC0415

    register_plugin(_StubPlugin())

    ctx = ReviewContext(
        org_id=_UUID(int=1),
        repo_external_id="acme/web",
        pr_external_id="acme/web#1",
        head_sha="h",
        base_sha="b",
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
        plugin_id = "broken"

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
        plugin_id = "aaa"

    class _B:
        plugin_id = "bbb"

    register_plugin(_A())
    register_plugin(_B())
    result = list_registered_plugins()
    assert [p.plugin_id for p in result] == ["aaa", "bbb"]


def test_registry_items_returns_tuple_of_pairs() -> None:
    """items() returns a tuple of (plugin_id, plugin) pairs matching registered entries."""
    from app.domain.coding_agent.service import current_coding_agent_registry  # noqa: PLC0415

    plugin = _StubPlugin()
    register_plugin(plugin)
    result = current_coding_agent_registry().items()
    assert isinstance(result, tuple)
    assert len(result) == 1
    pid, p = result[0]
    assert pid == "stub"
    assert p is plugin


def test_registry_items_is_immutable_snapshot() -> None:
    """Mutating the tuple returned by items() does not affect the registry."""
    from app.domain.coding_agent.service import current_coding_agent_registry  # noqa: PLC0415

    register_plugin(_StubPlugin())
    reg = current_coding_agent_registry()
    snapshot = reg.items()
    # Replace the entry in a local copy — registry must be unchanged.
    modified = list(snapshot)
    modified[0] = ("stub", None)  # type: ignore[assignment]
    assert reg.items()[0][1] is not None  # original plugin still there
