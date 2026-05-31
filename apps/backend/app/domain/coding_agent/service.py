"""Registry + dispatch for coding-agent plugins."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.plugin_kit import PluginMeta
from app.core.workspace import Workspace
from app.domain.coding_agent.types import (
    AnswerQuestionContext,
    AnswerQuestionResult,
    CodingAgentPlugin,
    HealthStatus,
    IncrementalReviewContext,
    IncrementalReviewResult,
    OnActivity,
    PluginNotFoundError,
    ReviewContext,
    ReviewResult,
    StaleCheckContext,
    StaleCheckResult,
    ValidationResult,
    VerifyFixContext,
    VerifyFixResult,
)

log = structlog.get_logger("coding_agent")


class CodingAgentRegistry:
    """Coding-agent plugin map. ContextVar-bound so each test context gets a
    fresh, isolated instance; production rides the import-time default for the
    process lifetime — it never calls bind_coding_agent_registry(). The
    ContextVar exists solely for per-test isolation (see app/testing/isolation.py)."""

    def __init__(self) -> None:
        self._plugins: dict[str, CodingAgentPlugin] = {}

    def register(self, plugin: CodingAgentPlugin) -> None:
        if plugin.meta.id in self._plugins:
            raise ValueError(f"coding agent plugin {plugin.meta.id!r} already registered")
        self._plugins[plugin.meta.id] = plugin

    def replace(self, plugin: CodingAgentPlugin) -> None:
        """Overwrite-or-insert; used by stub/fake helpers."""
        self._plugins[plugin.meta.id] = plugin

    def get(self, plugin_id: str) -> CodingAgentPlugin:
        try:
            return self._plugins[plugin_id]
        except KeyError as e:
            raise PluginNotFoundError(plugin_id) from e

    def list(self) -> list[CodingAgentPlugin]:
        return list(self._plugins.values())

    def ids(self) -> list[str]:
        return list(self._plugins.keys())

    def copy(self) -> CodingAgentRegistry:
        clone = CodingAgentRegistry()
        clone._plugins = dict(self._plugins)
        return clone


_registry_var: ContextVar[CodingAgentRegistry | None] = ContextVar("_coding_agent_registry_var", default=None)
# Import-time default: plugins that call register_plugin() at module-import
# time (bootstrap()) land here when no per-test binding is active. Production
# never calls bind_coding_agent_registry(); the ContextVar exists solely for
# per-test isolation.
_default_registry = CodingAgentRegistry()


def bind_coding_agent_registry(instance: CodingAgentRegistry) -> None:
    _registry_var.set(instance)


def current_coding_agent_registry() -> CodingAgentRegistry:
    return _registry_var.get() or _default_registry


def register_plugin(plugin: CodingAgentPlugin) -> None:
    """Register a coding-agent plugin. Raises ValueError if id already taken."""
    current_coding_agent_registry().register(plugin)


# Alias with the naming convention used by helpers.
register_coding_agent_plugin = register_plugin


def list_registered_plugins() -> list[CodingAgentPlugin]:
    """Return registered plugins in insertion order."""
    return current_coding_agent_registry().list()


def get_plugin(plugin_id: str) -> CodingAgentPlugin:
    return current_coding_agent_registry().get(plugin_id)


async def review(
    plugin_id: str,
    workspace: Workspace,
    context: ReviewContext,
    on_activity: OnActivity | None = None,
) -> ReviewResult:
    plugin = get_plugin(plugin_id)
    result = await plugin.review(workspace, context, on_activity=on_activity)
    log.info(
        "agent.reviewed",
        plugin_id=plugin_id,
        status=result.status,
        findings=len(result.findings),
        tokens_in=result.telemetry.tokens_in,
        tokens_out=result.telemetry.tokens_out,
        latency_ms=result.telemetry.latency_ms,
    )
    return result


async def incremental_review(
    plugin_id: str,
    workspace: Workspace,
    context: IncrementalReviewContext,
    on_activity: OnActivity | None = None,
) -> IncrementalReviewResult:
    plugin = get_plugin(plugin_id)
    result = await plugin.incremental_review(workspace, context, on_activity=on_activity)
    log.info(
        "agent.incremental_reviewed",
        plugin_id=plugin_id,
        status=result.status,
        findings=len(result.findings),
        prev_sha=context.prev_sha,
        head_sha=context.head_sha,
        latency_ms=result.telemetry.latency_ms,
    )
    return result


async def verify_fix(
    plugin_id: str,
    workspace: Workspace,
    context: VerifyFixContext,
    on_activity: OnActivity | None = None,
) -> VerifyFixResult:
    plugin = get_plugin(plugin_id)
    result = await plugin.verify_fix(workspace, context, on_activity=on_activity)
    log.info(
        "agent.verified_fix",
        plugin_id=plugin_id,
        status=result.status,
        still_present=result.still_present,
        confidence=result.confidence,
        rule_id=context.original_rule_id,
    )
    return result


async def stale_check(
    plugin_id: str,
    workspace: Workspace,
    context: StaleCheckContext,
    on_activity: OnActivity | None = None,
) -> StaleCheckResult:
    plugin = get_plugin(plugin_id)
    result = await plugin.stale_check(workspace, context, on_activity=on_activity)
    log.info(
        "agent.stale_checked",
        plugin_id=plugin_id,
        status=result.status,
        still_applies=result.still_applies,
        confidence=result.confidence,
        rule_id=context.original_rule_id,
    )
    return result


async def answer_question(
    plugin_id: str,
    workspace: Workspace,
    context: AnswerQuestionContext,
    on_activity: OnActivity | None = None,
) -> AnswerQuestionResult:
    plugin = get_plugin(plugin_id)
    result = await plugin.answer_question(workspace, context, on_activity=on_activity)
    log.info(
        "agent.answered_question",
        plugin_id=plugin_id,
        status=result.status,
        rule_id=context.original_rule_id,
        latency_ms=result.telemetry.latency_ms,
    )
    return result


async def validate_config(plugin_id: str, agent_config: dict[str, Any]) -> ValidationResult:
    return await get_plugin(plugin_id).validate_config(agent_config)


async def health_check_all() -> dict[str, HealthStatus]:
    out: dict[str, HealthStatus] = {}
    for plugin_id, plugin in current_coding_agent_registry()._plugins.items():
        try:
            out[plugin_id] = await plugin.health_check()
        except Exception as e:
            out[plugin_id] = HealthStatus(healthy=False, message=str(e), checked_at=datetime.now(UTC))
    return out


def registered_plugin_ids() -> list[str]:
    return current_coding_agent_registry().ids()


def list_plugin_metas() -> list[PluginMeta]:
    """Return `PluginMeta` for every registered coding-agent plugin, sorted by id."""
    reg = current_coding_agent_registry()
    return [reg._plugins[pid].meta for pid in sorted(reg._plugins)]
