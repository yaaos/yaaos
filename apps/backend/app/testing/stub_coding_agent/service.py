"""Wrapper plugin that fakes any `CodingAgentPlugin` for offline tests.

The bootstrap (when `YAAOS_CODING_AGENT_STUB` is set) walks the
`domain/coding_agent` registry and replaces each registered plugin with a
`StubCodingAgentPlugin` wrapping it. From every consumer's perspective, nothing
changes — `coding_agent.review(...)` returns the same `ReviewResult` shape; it
just never touches a real CLI or vendor API.

The stub returns canned success results. It has zero knowledge of prompt
content — that's the real plugin's responsibility. `validate_config` passes
through; `health_check` reports stub mode.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.workspace import Workspace
from app.domain.coding_agent import (
    ActivityEvent,
    AnswerQuestionContext,
    AnswerQuestionResult,
    FindingAnchor,
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

log = structlog.get_logger("testing.stub_coding_agent")


_STUB_TELEMETRY = InvocationTelemetry(
    tokens_in=1000,
    tokens_out=200,
    latency_ms=10,
    raw_output="",
    raw_stderr="",
    model="opus",
)


def _canned_activity() -> list[ActivityEvent]:
    """Default sequence emitted by the stub — enough events to exercise the
    persisted activity log + SSE path without inventing realistic content."""
    now = datetime.now(UTC)
    return [
        ActivityEvent(
            ts=now,
            kind="session_start",
            message="Session started · model opus",
            detail={"model": "opus", "session_id": "stub-session"},
        ),
        ActivityEvent(
            ts=now,
            kind="subagent_dispatched",
            message="Dispatching yaaos-architecture",
            detail={"subagent": "yaaos-architecture"},
        ),
        ActivityEvent(
            ts=now,
            kind="tool_call_started",
            message="Read src/example.ts",
            detail={"tool": "Read", "input": {"file_path": "src/example.ts"}},
        ),
        ActivityEvent(
            ts=now,
            kind="result",
            message="Review complete",
            detail={"num_turns": 1},
        ),
    ]


class StubCodingAgentPlugin:
    """Wraps a real `CodingAgentPlugin`; intercepts `review`."""

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped
        self.meta = wrapped.meta

    async def review(
        self,
        workspace: Workspace,
        context: ReviewContext,
        on_activity: OnActivity | None = None,
    ) -> ReviewResult:
        del workspace
        # Emit a canned event sequence so consumers exercise the activity-log
        # path (persistence + SSE) the same way the real CLI would.
        if on_activity is not None:
            for event in _canned_activity():
                try:
                    await on_activity(event)
                except Exception:
                    log.exception("stub_coding_agent.on_activity_failed")
        # Emit one synthetic FindingDraft so e2e flows
        # that depend on findings have something to act against.
        finding = FindingDraft(
            severity="minor",
            rule_id="stub/sample-suggestion",
            title="[stub] sample suggestion",
            body="Stub finding. Used by e2e specs that exercise the finding-expansion + Teach-yaaos flow.",
            concrete_failure_scenario=(
                "Stub plugin always emits this finding so e2e specs can exercise the durable-findings flow."
            ),
            confidence=90,
            rationale="Stub plugin: emitted for e2e coverage.",
            anchor=FindingAnchor(file_path="src/example.ts", line_start=1, line_end=1),
        )
        return ReviewResult(
            status=InvocationStatus.SUCCESS,
            findings=[finding],
            state="COMMENT",
            summary_body="[stub] yaaos review",
            lesson_ids_consulted=[lesson.id for lesson in context.lessons],
            telemetry=_STUB_TELEMETRY,
        )

    async def incremental_review(
        self,
        workspace: Workspace,
        context: IncrementalReviewContext,
        on_activity: OnActivity | None = None,
    ) -> IncrementalReviewResult:
        del workspace, context, on_activity
        # One synthetic FindingDraft with `concrete_failure_scenario` populated
        # so it survives the reviewer aggregate's schema check.
        return IncrementalReviewResult(
            status=InvocationStatus.SUCCESS,
            findings=[
                FindingDraft(
                    severity="minor",
                    rule_id="stub/incremental",
                    title="[stub] incremental finding",
                    body="Stub incremental finding for e2e flows.",
                    concrete_failure_scenario="N/A — stub plugin output.",
                    confidence=90,
                    rationale="Stub plugin: emitted for e2e coverage.",
                    anchor=FindingAnchor(file_path="src/example.ts", line_start=1, line_end=1),
                )
            ],
            telemetry=_STUB_TELEMETRY,
        )

    async def verify_fix(
        self,
        workspace: Workspace,
        context: VerifyFixContext,
        on_activity: OnActivity | None = None,
    ) -> VerifyFixResult:
        del workspace, context, on_activity
        return VerifyFixResult(
            status=InvocationStatus.SUCCESS,
            still_present=False,
            confidence=0.95,
            reasoning="Stub plugin: always reports the issue as fixed.",
            telemetry=_STUB_TELEMETRY,
        )

    async def stale_check(
        self,
        workspace: Workspace,
        context: StaleCheckContext,
        on_activity: OnActivity | None = None,
    ) -> StaleCheckResult:
        del workspace, context, on_activity
        return StaleCheckResult(
            status=InvocationStatus.SUCCESS,
            still_applies=True,
            confidence=0.95,
            reasoning="Stub plugin: always reports the finding as still applying.",
            telemetry=_STUB_TELEMETRY,
        )

    async def answer_question(
        self,
        workspace: Workspace,
        context: AnswerQuestionContext,
        on_activity: OnActivity | None = None,
    ) -> AnswerQuestionResult:
        del workspace, on_activity
        # Echo the question into a deterministic canned answer so e2e flows
        # exercising the question intent can assert on the body shape.
        return AnswerQuestionResult(
            status=InvocationStatus.SUCCESS,
            answer=f"[stub] answering: {context.question[:120]}",
            telemetry=_STUB_TELEMETRY,
        )

    async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult:
        return await self._wrapped.validate_config(agent_config)

    async def health_check(self) -> HealthStatus:
        return HealthStatus(healthy=True, message="stub mode", checked_at=datetime.now(UTC))

    def install_url(self, org_id: Any) -> str | None:
        return self._wrapped.install_url(org_id)

    def validate_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return self._wrapped.validate_settings(settings)


def wrap_all_registered_plugins() -> int:
    """Replace every registered coding-agent plugin with a stub wrapping it.

    Binds a fresh registry with wrapped entries; never mutates the canonical
    registry dict.
    """
    from app.domain.coding_agent import (  # noqa: PLC0415
        CodingAgentRegistry,
        bind_coding_agent_registry,
        current_coding_agent_registry,
    )

    originals = current_coding_agent_registry().list()
    fresh = CodingAgentRegistry()
    count = 0
    for real in originals:
        if isinstance(real, StubCodingAgentPlugin):
            fresh.replace(real)
        else:
            fresh.replace(StubCodingAgentPlugin(wrapped=real))
            count += 1
    bind_coding_agent_registry(fresh)
    log.info("stub_coding_agent.wrapped_all", count=count)
    return count
