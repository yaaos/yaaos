"""Types + Protocol for the coding-agent abstraction.

The Protocol exposes five in-process task modes plus five remote-dispatch
methods: `build_review_invocation`, `parse_review_output`,
`review_preflight_steps`, `parse_usage`, `render_activity`. Together they
define the full plugin capability surface; the shipped remote review path
exercises the build/parse/render/preflight subset, while the in-process
methods are part of the contract a fully-featured plugin satisfies. Plugins
own prompt assembly, exec spec construction, and parsing for each mode;
consumers hand over domain context and receive domain results.

`ReportedFinding` is the raw-string output twin for findings returned by
the agent. It carries no enum constraints (those live in `domain/reviewer`)
so `core/coding_agent` stays free of domain imports.

`Invocation` + `ExecSpec` are the value objects `build_review_invocation`
returns. `ExecSpec.env` carries the Anthropic API key in cleartext — the
documented carve-out for wire-bound exec (matches `otlp_token` on ConfigUpdate).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.core.agent_gateway import InvokeClaudeCodeLimits
from app.core.vcs import Diff, VCSPullRequest
from app.core.workspace import HealthStatus, Workspace

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@runtime_checkable
class LessonRef(Protocol):
    """Structural contract for a lesson passed into an incremental review.

    Lets `core/coding_agent` accept `domain/lessons.Lesson` objects without a
    core→domain import: any object exposing `id` / `title` / `body` satisfies
    it. `assemble_incremental_review_prompt` reads exactly these three fields.
    """

    @property
    def id(self) -> UUID: ...
    @property
    def title(self) -> str: ...
    @property
    def body(self) -> str: ...


# Re-exported for the canonical schema so reviewers can compare field sets.
# `Severity` stays here as the raw-string alias used by the Protocol.
Severity = Literal["blocker", "should_fix", "nit"]


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

    `seq` is the monotonic 0-based index inside the run's `ActivityLog`,
    assigned by `render_activity` after filtering null renders.
    """

    seq: int = 0
    ts: datetime
    kind: str
    message: str
    detail: dict[str, Any] = {}


OnActivity = Callable[[ActivityEvent], Awaitable[None]]


class Usage(BaseModel):
    """Per-run token usage + wallclock duration.

    Parsed from the terminal `type=result` stream-json event by
    `CodingAgentPlugin.parse_usage`. Persisted onto `coding_agent_runs`
    by `finalize_run`. Fields default to None when the agent didn't report
    them (e.g. a non-conforming or truncated terminal event).
    """

    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_ms: int | None = None


class ActivityLog(BaseModel):
    """Pre-rendered activity stream for one coding-agent run.

    Produced once per run from the terminal stdout by
    `CodingAgentPlugin.render_activity` — the same event sequence the
    in-process path streams via `OnActivity`, captured durably for the
    Activity tab. Persisted as a JSONB blob in the partitioned
    `coding_agent_activity` table.
    """

    events: tuple[ActivityEvent, ...] = ()


class FindingAnchor(BaseModel):
    """Source-code anchor for a finding — file path + line range.

    Used by `VerifyFixContext` and `AnswerQuestionContext` to identify where
    in the repo the finding was originally raised.
    `line_start` and `line_end` are 1-indexed, inclusive.
    """

    file_path: str
    line_start: int
    line_end: int


class ExecSpec(BaseModel):
    """The concrete exec block the Go agent uses to spawn the Claude Code CLI.

    `env` carries the Anthropic API key in cleartext — the accepted carve-out
    for wire-bound exec (the key must reach the agent to call `claude`), same
    as the `otlp_token` on ConfigUpdate. The dict is persisted in
    `agent_commands.payload` JSONB until the command row is retired.
    """

    argv: tuple[str, ...] = ()
    stdin: str = ""
    env: dict[str, str] = {}


class Invocation(BaseModel):
    """Output of `build_*_invocation`. Serialised into `InvokeClaudeCodeCommand.invocation`.

    `kind` identifies the skill handle the agent should run.
    `exec` carries the argv/stdin/env block.
    `limits` are the per-run wallclock caps passed through to the agent.
    """

    kind: str
    exec: ExecSpec
    limits: InvokeClaudeCodeLimits


class ReviewContext(BaseModel):
    """Context for a remote PR review dispatch.

    Carries the identifiers the skill needs to run `git diff base..head` in
    the clone and emit structured findings. No diff blob crosses the wire —
    the skill computes it from the clone.

    `output_schema` is an immutable snapshot of `finding_output_schema()`
    frozen at dispatch, so the skill validates against the exact shape the
    run launched with (no mid-run drift). `PostFindings` re-validates the
    returned stdout against the same contract.
    """

    org_id: UUID
    repo_external_id: str
    pr_external_id: str
    head_sha: str
    base_sha: str
    output_schema: Mapping[str, Any] = {}


class ReviewResult(BaseModel):
    """The plugin's review returns `ReportedFinding`s; posting is handled
    by `domain/reviewer.publish_findings`.
    """

    status: InvocationStatus
    findings: list[ReportedFinding] = []
    state: Literal["APPROVED", "CHANGES_REQUESTED", "COMMENT"] | None = None
    summary_body: str | None = None
    lesson_ids_consulted: list[UUID] = []
    telemetry: InvocationTelemetry = InvocationTelemetry()
    error_message: str | None = None


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = []


class ReportedFinding(BaseModel):
    """One raw finding produced by an agent task.

    Raw strings only — no enum validation. `domain/reviewer` validates
    `severity` and `confidence` into typed values when converting to `Finding`.
    `file` and `line` are optional — general (PR-wide) findings carry no anchor.
    """

    file: str | None = None
    line: int | None = None
    category: str
    severity: str
    confidence: str
    rationale: str
    rule_violated: str
    rule_source: str
    suggested_fix: str


class IncrementalReviewContext(BaseModel):
    """Inputs for a `incremental_review` task — review `prev_sha..head` only.

    Prior findings passed so the agent can avoid re-raising issues already
    known.

    `lessons` are `LessonRef`-shaped (`domain/lessons.Lesson` satisfies the
    Protocol structurally) so `core/coding_agent` stays free of a core→domain
    import while keeping the `.id` / `.title` / `.body` access in
    `prompts.assemble_incremental_review_prompt` typed.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    pr: VCSPullRequest
    diff: Diff
    prev_sha: str
    head_sha: str
    lessons: list[LessonRef] = []
    language_hint: str | None = None
    prior_open_finding_summaries: list[str] = []
    prior_acknowledged_finding_summaries: list[str] = []
    agent_config: dict[str, Any] = {}


class IncrementalReviewResult(BaseModel):
    status: InvocationStatus
    findings: list[ReportedFinding] = []
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


class PriorThreadMessage(BaseModel):
    """One earlier message in the thread the question lives under."""

    author_kind: Literal["yaaos", "human"]
    body: str


class AnswerQuestionContext(BaseModel):
    """A developer asked a question on a yaaos finding (`question` intent).

    The agent investigates the finding in the workspace with read-only repo
    + git tool access and emits one concise reply. The reviewer posts that
    reply back into the GitHub thread. Distinct from `verify_fix` — no
    "still present?" verdict, just an answer.
    """

    original_finding_title: str
    original_finding_body: str
    original_rule_id: str
    code_snippet: str
    current_anchor: FindingAnchor
    question: str
    prior_messages: list[PriorThreadMessage] = []
    base_sha: str = ""
    head_sha: str = ""
    language_hint: str | None = None
    agent_config: dict[str, Any] = {}


class AnswerQuestionResult(BaseModel):
    status: InvocationStatus
    answer: str = ""
    telemetry: InvocationTelemetry = InvocationTelemetry()
    error_message: str | None = None


class CodingAgentPlugin(Protocol):
    plugin_id: str

    def install_url(self, org_id: UUID) -> str | None:
        """URL to redirect the user to for plugin install. `None` for plugins
        that have no out-of-band install step (settings-only)."""
        ...

    def validate_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Validate a settings payload. Returns the canonicalized dict on
        success; raises `ValueError` on invalid input."""
        ...

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

    async def answer_question(
        self,
        workspace: Workspace,
        context: AnswerQuestionContext,
        on_activity: OnActivity | None = None,
    ) -> AnswerQuestionResult: ...

    async def validate_config(self, agent_config: dict[str, Any]) -> ValidationResult: ...

    async def health_check(self) -> HealthStatus: ...

    # ── Remote-dispatch methods (Shape B) ────────────────────────────────
    # The in-process run methods above and the remote-dispatch methods below
    # together define the full plugin capability surface. The shipped remote
    # review path exercises only the build/parse/render/preflight subset; the
    # in-process methods are part of the contract a fully-featured plugin
    # satisfies.

    async def build_review_invocation(
        self,
        ctx: ReviewContext,
        *,
        session: AsyncSession,
    ) -> Invocation:
        """Build the `Invocation` (argv/stdin/env + limits) for a PR review.

        Resolves the skill handle, decrypts the Anthropic API key, assembles
        the prompt and output-schema appendix, and returns the complete exec
        spec the agent will run. Never dispatches — caller dispatches via
        `dispatch_invoke_claude_code`.
        """
        ...

    def parse_review_output(self, stdout: str) -> list[ReportedFinding]:
        """Parse the agent's stream-json stdout into `ReportedFinding` objects.

        Finds the terminal `type=result` event, extracts the `result` field,
        and lenient-parses the JSON. Raises `ValueError` on any parse failure
        or structurally non-conforming output so `PostFindings` can gate on it.
        """
        ...

    def parse_usage(self, stdout: str) -> Usage:
        """Parse token usage + duration from the terminal stream-json event.

        Reads the last `type=result` event's `usage.input_tokens` /
        `usage.output_tokens` + `duration_ms`. Missing fields surface as
        `None`. A stream with no terminal `result` event returns an empty
        `Usage()` — never raises.
        """
        ...

    def render_activity(self, stdout: str) -> ActivityLog:
        """Pre-render the full activity stream from terminal stdout.

        Walks every parseable stream-json event, converts each to an
        `ActivityEvent`, drops null renders, and assigns monotonic
        `seq`. Returns an empty `ActivityLog` on no parseable events.
        """
        ...

    async def review_preflight_steps(
        self,
        ctx: ReviewContext,
        *,
        session: AsyncSession,
    ) -> tuple[str, ...]:
        """Return WorkflowCommand kind strings to insert before the review step.

        Returns `("SeedSkills",)` when the repo uses the yaaos skill bundle;
        returns `()` otherwise. Hardcoded to `()` until skill-assignment
        resolution is implemented.
        """
        ...


class CodingAgentError(Exception):
    """Infrastructure failure (subprocess won't spawn, config table unreadable)."""


class PluginNotFoundError(LookupError):
    """Plugin id not registered."""


class CodingAgentCacheMiss(Exception):
    """Raised by the caching wrapper when a cached invocation is missing in pytest."""
