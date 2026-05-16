"""Wrapper plugin that fakes any `CodingAgentPlugin` for offline tests.

The bootstrap (when `YAAOF_CODING_AGENT_STUB` is set) walks the
`domain/coding_agent` registry and replaces each registered plugin with a
`StubCodingAgentPlugin` wrapping it. From every consumer's perspective, nothing
changes — `coding_agent.review("claude_code", ...)` returns the same
`ReviewResult` shape; it just never touches a real CLI or vendor API.

The stub returns canned success results. It has zero knowledge of prompt
content — that's the real plugin's responsibility. `validate_config` passes
through; `health_check` reports stub mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from app.core.workspace import Workspace
from app.domain.coding_agent import (
    HealthStatus,
    InvocationStatus,
    InvocationTelemetry,
    ReplyContext,
    ReplyResult,
    ReviewContext,
    ReviewResult,
    ValidationResult,
)

log = structlog.get_logger("testing.stub_coding_agent")


_STUB_TELEMETRY = InvocationTelemetry(
    tokens_in=1000,
    tokens_out=200,
    cost_usd=Decimal("0.0050"),
    latency_ms=10,
    raw_output="",
    raw_stderr="",
)


class StubCodingAgentPlugin:
    """Wraps a real `CodingAgentPlugin`; intercepts `review` and `reply`.

    Constructor takes the wrapped plugin; mirrors its `plugin_id` so the
    registry consumer can't tell the difference. `validate_config` passes
    through (config validation is config-shape work, not LLM behavior).
    """

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped
        self.meta = wrapped.meta

    async def review(self, workspace: Workspace, context: ReviewContext) -> ReviewResult:
        del workspace
        return ReviewResult(
            status=InvocationStatus.SUCCESS,
            findings=[],
            state="APPROVED",
            summary_body=f"[stub] {context.agent_name} review",
            lesson_ids_consulted=[lesson.id for lesson in context.lessons],
            telemetry=_STUB_TELEMETRY,
        )

    async def reply(self, workspace: Workspace, context: ReplyContext) -> ReplyResult:
        del workspace
        return ReplyResult(
            status=InvocationStatus.SUCCESS,
            body=f"[stub] {context.agent_name} reply",
            telemetry=_STUB_TELEMETRY,
        )

    async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult:
        return await self._wrapped.validate_config(agent_config)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(healthy=True, message="stub mode", checked_at=datetime.now(UTC))


def wrap_all_registered_plugins() -> int:
    """Replace every entry in `domain.coding_agent._PLUGINS` with a stub wrapping it.

    Returns the count of wrapped plugins. Called from `app/main.py` when
    `YAAOF_CODING_AGENT_STUB` is set; the testing layer is the only thing
    permitted to reach into the registry like this.
    """
    from app.domain.coding_agent import _PLUGINS  # noqa: PLC0415 — registry access

    count = 0
    for plugin_id, real in list(_PLUGINS.items()):
        if isinstance(real, StubCodingAgentPlugin):
            continue  # idempotent
        _PLUGINS[plugin_id] = StubCodingAgentPlugin(wrapped=real)
        count += 1
    log.info("stub_coding_agent.wrapped_all", count=count)
    return count
