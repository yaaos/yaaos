"""Types + Protocol for the coding-agent abstraction.

The Protocol exposes one operation — `review(context)` — not a generic
`invoke(prompt, response_model)`. Consumers hand over domain inputs (a PR,
a diff, lessons) and receive vendor-neutral results. Prompt assembly,
output-schema definition, and JSON parsing are the plugin's job.

The plugin is expected to spawn a single parent reviewer that dispatches
subagent definitions (shipped under `app/domain/coding_agent/reviewers/`)
and synthesizes their findings — the plugin owns the orchestration shape,
not the contract.

Lives in `domain/` (not `core/`) because its types reference `vcs.Finding` and
related domain models. The plugin contract resolves through a registry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.primitives import PluginMeta
from app.core.workspace import HealthStatus, Workspace
from app.domain.memory import Lesson
from app.domain.vcs import Diff, Finding, VCSPullRequest


class InvocationStatus(StrEnum):
    SUCCESS = "success"
    PARSE_FAILURE = "parse_failure"
    AGENT_ERROR = "agent_error"
    TIMEOUT = "timeout"


class InvocationTelemetry(BaseModel):
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_ms: int = 0
    raw_output: str = ""
    raw_stderr: str = ""
    # The actual model the CLI reports having used (e.g. an alias like
    # `opus` resolves to a versioned name). Falls back to None when the
    # plugin can't determine it.
    model: str | None = None


class ActivityEvent(BaseModel):
    """One captured event from a coding-agent run.

    Pre-rendered by the plugin so the FE doesn't have to interpret raw
    Claude Code stream-json shapes — `message` is the user-facing string
    shown in the UI; `detail` is the raw event data for the expanded view.
    """

    ts: datetime
    kind: str
    message: str
    detail: dict[str, Any] = {}


OnActivity = Callable[[ActivityEvent], Awaitable[None]]


class ReviewContext(BaseModel):
    """Everything a plugin needs to produce a review of a PR.

    There's no per-agent persona anymore — the plugin spawns a single parent
    reviewer that dispatches subagents whose definitions ship with yaaos.
    """

    pr: VCSPullRequest
    diff: Diff
    lessons: list[Lesson] = []
    language_hint: str | None = None
    prior_yaaos_comment_bodies: list[str] = []
    agent_config: dict[str, Any] = {}


class ReviewResult(BaseModel):
    status: InvocationStatus
    findings: list[Finding] = []
    state: Literal["APPROVED", "CHANGES_REQUESTED", "COMMENT"] | None = None
    summary_body: str | None = None
    lesson_ids_consulted: list[UUID] = []
    telemetry: InvocationTelemetry = InvocationTelemetry()
    error_message: str | None = None


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = []


Severity = Literal["blocker", "major", "minor", "nit"]


class FindingAnchor(BaseModel):
    """Where in the code the finding applies. Plugin-side anchor shape.

    `domain/reviewer` maps this onto its own `CodeAnchor` value object
    (which adds the surrounding-content + commit-sha bits needed to
    re-resolve under line drift).
    """

    file_path: str
    line_start: int
    line_end: int


class FindingDraft(BaseModel):
    """One raw finding produced by an agent task. Schema per plan §10.1.

    The reviewer aggregate rejects any draft missing `concrete_failure_scenario`
    or below the per-severity confidence threshold before storing it. Severity
    is sticky once stored; the per-PR nit cap and per-review top-10 cap are
    applied by the aggregate, not here.
    """

    severity: Severity
    rule_id: str
    title: str
    body: str
    concrete_failure_scenario: str
    confidence: int = Field(ge=0, le=100)
    rationale: str
    anchor: FindingAnchor
    duplicate_of_rule_ids: list[str] = []


class IncrementalReviewContext(BaseModel):
    """Inputs for a `incremental_review` task — review `prev_sha..head` only.

    Prior findings + acknowledgments are passed so the agent can avoid
    re-raising issues the developer already accepted.
    """

    pr: VCSPullRequest
    diff: Diff
    prev_sha: str
    head_sha: str
    lessons: list[Lesson] = []
    language_hint: str | None = None
    prior_open_finding_summaries: list[str] = []
    prior_acknowledged_finding_summaries: list[str] = []
    agent_config: dict[str, Any] = {}


class IncrementalReviewResult(BaseModel):
    status: InvocationStatus
    findings: list[FindingDraft] = []
    telemetry: InvocationTelemetry = InvocationTelemetry()
    error_message: str | None = None


class VerifyFixContext(BaseModel):
    """Is a previously raised finding still present at HEAD?

    The reviewer supplies original anchor code, current code at the resolved
    anchor on HEAD, and the finding body. Agent reads only what's given;
    no broader exploration unless the finding's nature requires it.
    """

    original_finding_title: str
    original_finding_body: str
    original_rule_id: str
    original_code_snippet: str
    current_code_snippet: str
    current_anchor: FindingAnchor
    agent_config: dict[str, Any] = {}


class VerifyFixResult(BaseModel):
    status: InvocationStatus
    still_present: bool = False
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    reasoning: str = ""
    observed_line: int | None = None
    telemetry: InvocationTelemetry = InvocationTelemetry()
    error_message: str | None = None


class StaleCheckContext(BaseModel):
    """Does a previously raised finding still apply after the code changed?

    Used when the original anchor moved or surrounding context changed
    materially. Distinct from `verify_fix` — `verify_fix` asks \"is the bug
    fixed?\"; `stale_check` asks \"is the bug still meaningful?\".
    """

    original_finding_title: str
    original_finding_body: str
    original_rule_id: str
    current_code_snippet: str
    diff_summary: str
    agent_config: dict[str, Any] = {}


class StaleCheckResult(BaseModel):
    status: InvocationStatus
    still_applies: bool = True
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
    reasoning: str = ""
    telemetry: InvocationTelemetry = InvocationTelemetry()
    error_message: str | None = None


class CodingAgentPlugin(Protocol):
    meta: PluginMeta

    async def review(
        self,
        workspace: Workspace,
        context: ReviewContext,
        on_activity: OnActivity | None = None,
    ) -> ReviewResult: ...

    async def incremental_review(
        self,
        workspace: Workspace,
        context: IncrementalReviewContext,
        on_activity: OnActivity | None = None,
    ) -> IncrementalReviewResult: ...

    async def verify_fix(
        self,
        workspace: Workspace,
        context: VerifyFixContext,
        on_activity: OnActivity | None = None,
    ) -> VerifyFixResult: ...

    async def stale_check(
        self,
        workspace: Workspace,
        context: StaleCheckContext,
        on_activity: OnActivity | None = None,
    ) -> StaleCheckResult: ...

    async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult: ...

    async def health_check(self) -> HealthStatus: ...


class CodingAgentError(Exception):
    """Infrastructure failure (subprocess won't spawn, config table unreadable)."""


class PluginNotFoundError(LookupError):
    """Plugin id not registered."""


class CodingAgentCacheMiss(Exception):
    """Raised by the caching wrapper when a cached invocation is missing in pytest."""
