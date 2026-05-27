"""Registry + dispatch for coding-agent plugins."""

from __future__ import annotations

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


_registry: dict[str, CodingAgentPlugin] = {}


def register_plugin(plugin: CodingAgentPlugin) -> None:
    """Register a coding-agent plugin. Raises ValueError if id already taken."""
    if plugin.meta.id in _registry:
        raise ValueError(f"coding agent plugin {plugin.meta.id!r} already registered")
    _registry[plugin.meta.id] = plugin


def list_registered_plugins() -> list[CodingAgentPlugin]:
    """Return registered plugins in insertion order."""
    return list(_registry.values())


def clear_plugins() -> None:
    """Remove all registered plugins (use in test teardown)."""
    _registry.clear()


def get_plugin(plugin_id: str) -> CodingAgentPlugin:
    try:
        return _registry[plugin_id]
    except KeyError as e:
        raise PluginNotFoundError(plugin_id) from e


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
    for plugin_id, plugin in _registry.items():
        try:
            out[plugin_id] = await plugin.health_check()
        except Exception as e:
            out[plugin_id] = HealthStatus(healthy=False, message=str(e), checked_at=datetime.now(UTC))
    return out


def registered_plugin_ids() -> list[str]:
    return list(_registry.keys())


def list_plugin_metas() -> list[PluginMeta]:
    """Return `PluginMeta` for every registered coding-agent plugin, sorted by id."""
    return [_registry[pid].meta for pid in sorted(_registry)]
