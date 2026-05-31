"""Standalone fake `CodingAgentPlugin` for tests that don't have a real
plugin registered (the existing `stub_coding_agent` wraps a real plugin;
this one stands alone).

Each method returns a deterministic, schema-valid result so command-body
tests can drive the workflow end-to-end without real plugin auth.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.plugin_kit import PluginMeta
from app.core.workspace import Workspace
from app.domain.coding_agent import (
    AnswerQuestionContext,
    AnswerQuestionResult,
    FindingDraft,
    HealthStatus,
    IncrementalReviewContext,
    IncrementalReviewResult,
    InvocationStatus,
    InvocationTelemetry,
    OnActivity,
    ReviewContext,
    ReviewResult,
    StaleCheckContext,
    StaleCheckResult,
    ValidationResult,
    VerifyFixContext,
    VerifyFixResult,
)

_TELEMETRY = InvocationTelemetry(tokens_in=0, tokens_out=0, latency_ms=0)


class FakeCodingAgentPlugin:
    """Minimal `CodingAgentPlugin` impl. Tests can override the canned
    returns by mutating the public attributes (`review_findings`,
    `verify_fix_still_present`, etc.) on the registered instance."""

    def __init__(self, plugin_id: str = "claude_code") -> None:
        self.meta = PluginMeta(id=plugin_id, type="coding_agent", display_name=f"fake-{plugin_id}")
        # Overridable per-instance return values.
        self.review_findings: list[FindingDraft] = []
        self.incremental_findings: list[FindingDraft] = []
        self.verify_fix_still_present: bool = False
        self.verify_fix_confidence: float = 0.95
        self.stale_still_applies: bool = True
        self.stale_confidence: float = 0.95
        self.answer_text: str = "fake answer"
        # ActivityEvents to emit on each invocation. Tests that want to
        # exercise the activity-stream fan-out path set this attribute,
        # and every coding-agent method invokes `on_activity` for each
        # event in turn before returning the result.
        self.activity_events: list = []
        # Captures of last calls for assertions.
        self.last_review_context: ReviewContext | None = None
        self.last_verify_fix_context: VerifyFixContext | None = None
        self.last_stale_context: StaleCheckContext | None = None
        self.last_answer_context: AnswerQuestionContext | None = None

    async def _emit_activity(self, on_activity):  # type: ignore[no-untyped-def]
        if on_activity is None:
            return
        for event in self.activity_events:
            await on_activity(event)

    def install_url(self, org_id: UUID) -> str | None:
        del org_id
        return None

    def validate_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return dict(settings)

    async def review(
        self,
        workspace: Workspace,
        context: ReviewContext,
        on_activity: OnActivity | None = None,
    ) -> ReviewResult:
        del workspace
        self.last_review_context = context
        await self._emit_activity(on_activity)
        return ReviewResult(
            status=InvocationStatus.SUCCESS,
            findings=list(self.review_findings),
            state="COMMENT",
            summary_body="fake review",
            telemetry=_TELEMETRY,
        )

    async def incremental_review(
        self,
        workspace: Workspace,
        context: IncrementalReviewContext,
        on_activity: OnActivity | None = None,
    ) -> IncrementalReviewResult:
        del workspace, context
        await self._emit_activity(on_activity)
        return IncrementalReviewResult(
            status=InvocationStatus.SUCCESS,
            findings=list(self.incremental_findings),
            telemetry=_TELEMETRY,
        )

    async def verify_fix(
        self,
        workspace: Workspace,
        context: VerifyFixContext,
        on_activity: OnActivity | None = None,
    ) -> VerifyFixResult:
        del workspace
        self.last_verify_fix_context = context
        await self._emit_activity(on_activity)
        return VerifyFixResult(
            status=InvocationStatus.SUCCESS,
            still_present=self.verify_fix_still_present,
            confidence=self.verify_fix_confidence,
            reasoning="fake verdict",
            telemetry=_TELEMETRY,
        )

    async def stale_check(
        self,
        workspace: Workspace,
        context: StaleCheckContext,
        on_activity: OnActivity | None = None,
    ) -> StaleCheckResult:
        del workspace
        self.last_stale_context = context
        await self._emit_activity(on_activity)
        return StaleCheckResult(
            status=InvocationStatus.SUCCESS,
            still_applies=self.stale_still_applies,
            confidence=self.stale_confidence,
            reasoning="fake stale-check verdict",
            telemetry=_TELEMETRY,
        )

    async def answer_question(
        self,
        workspace: Workspace,
        context: AnswerQuestionContext,
        on_activity: OnActivity | None = None,
    ) -> AnswerQuestionResult:
        del workspace
        self.last_answer_context = context
        await self._emit_activity(on_activity)
        return AnswerQuestionResult(
            status=InvocationStatus.SUCCESS,
            answer=self.answer_text,
            telemetry=_TELEMETRY,
        )

    async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult:
        del agent_config
        return ValidationResult(valid=True, errors=[])

    async def health_check(self) -> HealthStatus:
        return HealthStatus(healthy=True, message="fake plugin", checked_at=datetime.now(UTC))


@contextmanager
def register_fake_coding_agent(plugin_id: str = "claude_code"):  # type: ignore[no-untyped-def]
    """Context manager: register a `FakeCodingAgentPlugin` under `plugin_id`,
    yielding the instance for setup + assertions. Restores prior registry
    binding on exit.

    Binds a fresh registry copy with the fake substituted; restores the prior
    binding on exit. Never mutates the canonical registry dict.
    """
    from app.domain.coding_agent import (  # noqa: PLC0415
        bind_coding_agent_registry,
        current_coding_agent_registry,
    )

    fake = FakeCodingAgentPlugin(plugin_id=plugin_id)
    prior = current_coding_agent_registry()
    fresh = prior.copy()
    fresh.replace(fake)  # type: ignore[arg-type]
    bind_coding_agent_registry(fresh)
    try:
        yield fake
    finally:
        bind_coding_agent_registry(prior)


__all__ = ["FakeCodingAgentPlugin", "register_fake_coding_agent"]
