"""Wire types for the WorkspaceAgent protocol.

Hand-written mirror of `apps/backend/openapi/agent-api.yaml`. Future
Phase-5+ work wires codegen; until then this file is the canonical
backend-side schema. The discriminated `AgentCommand` union matches the
OpenAPI `oneOf(discriminator: kind)` shape.

All commands + events carry `traceparent` (W3C trace context).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer, model_validator

# ── Discriminator + shared base ─────────────────────────────────────────


class AgentCommandKind(StrEnum):
    PROVISION_WORKSPACE = "ProvisionWorkspace"
    WRITE_FILES = "WriteFiles"
    REFRESH_WORKSPACE_AUTH = "RefreshWorkspaceAuth"
    INVOKE_CLAUDE_CODE = "InvokeClaudeCode"
    CLEANUP_WORKSPACE = "CleanupWorkspace"
    PUSH_BRANCH = "PushBranch"
    CONFIG_UPDATE = "ConfigUpdate"
    SHUTDOWN = "Shutdown"
    CANCEL_SHUTDOWN = "CancelShutdown"


class _CommandBase(BaseModel):
    model_config = ConfigDict(frozen=True)
    command_id: UUID
    workspace_id: UUID
    traceparent: str
    # Per-command completion capability token, minted at claim and echoed back on
    # the terminal/progress AgentEvent. Plain `str` (not SecretStr): it must
    # serialize its real value to the agent over the wire — same rationale as
    # `AuthBlock.token`. NEVER log this field.
    completion_token: str | None = None
    # Pipeline run that dispatched this command. Stamped at enqueue time so
    # agent-side spans can carry run_id without a separate lookup. NULL for
    # agent-scoped commands (ConfigUpdate) that do not correlate to a run.
    run_id: UUID | None = None


# ── The five concrete AgentCommand kinds ────────────────────────────────


# Backend-supplied commit identity applied via `git config user.name`/
# `user.email` in every provisioned checkout. Not user-configurable — the
# first skill commit fails without an identity, and there is no per-org or
# per-repo override today.
DEFAULT_GIT_USER_NAME = "yaaos"
DEFAULT_GIT_USER_EMAIL = "yaaos[bot]@users.noreply.github.com"


class RepoRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    plugin_id: str
    external_id: str
    clone_url: str
    # Checkout instruction: exactly one of head_sha (detached pin — the
    # fork-safe fetch-by-SHA path review flows use) or branch_name (named
    # work branch; the agent checks it out with `git checkout -B`, tracking
    # the remote when it already exists) must be set.
    head_sha: str | None = None
    base_sha: str | None = None
    branch_name: str | None = None

    @model_validator(mode="after")
    def _check_checkout_mode(self) -> RepoRef:
        if bool(self.head_sha) == bool(self.branch_name):
            raise ValueError("RepoRef requires exactly one of head_sha or branch_name (checkout instruction)")
        return self


class AuthBlock(BaseModel):
    model_config = ConfigDict(frozen=True)
    kind: Literal["github_installation", "oauth"]
    token: str


class ProvisionWorkspaceCommand(_CommandBase):
    kind: Literal[AgentCommandKind.PROVISION_WORKSPACE] = AgentCommandKind.PROVISION_WORKSPACE
    repo: RepoRef
    history: int = Field(ge=1)
    auth: AuthBlock
    ttl_seconds: int = Field(ge=60)
    max_idle_seconds: int = Field(ge=60)
    # Commit identity the agent applies via `git config user.name`/
    # `user.email` after clone. Backend-supplied constants — see
    # DEFAULT_GIT_USER_NAME / DEFAULT_GIT_USER_EMAIL above.
    git_user_name: str = DEFAULT_GIT_USER_NAME
    git_user_email: str = DEFAULT_GIT_USER_EMAIL


class WriteFilesEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    path: str
    content: str
    mode: str | None = None


class WriteFilesCommand(_CommandBase):
    kind: Literal[AgentCommandKind.WRITE_FILES] = AgentCommandKind.WRITE_FILES
    files: tuple[WriteFilesEntry, ...]


class RefreshWorkspaceAuthCommand(_CommandBase):
    kind: Literal[AgentCommandKind.REFRESH_WORKSPACE_AUTH] = AgentCommandKind.REFRESH_WORKSPACE_AUTH
    new_token: str


class InvokeClaudeCodeLimits(BaseModel):
    model_config = ConfigDict(frozen=True)
    wallclock_seconds: int = Field(ge=1)


class InvokeClaudeCodeCommand(_CommandBase):
    kind: Literal[AgentCommandKind.INVOKE_CLAUDE_CODE] = AgentCommandKind.INVOKE_CLAUDE_CODE
    # The `invocation` body is intentionally permissive at the wire layer;
    # its shape is owned by `domain/coding_agent`.
    invocation: dict[str, Any]
    mcp_servers: tuple[dict[str, Any], ...] = ()
    limits: InvokeClaudeCodeLimits
    result_spec: dict[str, Any] = Field(default_factory=dict)
    # Conventional path of the named skill inside the checkout
    # (`.claude/skills/<skill_name>/SKILL.md`), computed by
    # `core/coding_agent.dispatch_invocation`. The agent stats this path
    # before spawning claude and fails deterministically when it's absent —
    # zero agent policy, the convention lives here.
    skill_path: str


class CleanupWorkspaceCommand(_CommandBase):
    kind: Literal[AgentCommandKind.CLEANUP_WORKSPACE] = AgentCommandKind.CLEANUP_WORKSPACE


class PushBranchCommand(_CommandBase):
    """Push-failure recovery only: a bare re-push of the workspace's current
    HEAD after a `refresh-auth` credential rotation, so claude is never
    re-run just to retry a push. `workspace_id` (on `_CommandBase`) is
    required — the workspace is expected to already be on its named work
    branch by provision's checkout invariant.
    """

    kind: Literal[AgentCommandKind.PUSH_BRANCH] = AgentCommandKind.PUSH_BRANCH


class InvokeClaudeCodeFields(BaseModel):
    """Kind-specific payload fields for an `InvokeClaudeCode` command.

    Carries only the command-kind-specific fields — no envelope keys
    (`kind`, `command_id`, `workspace_id`, `traceparent`, `completion_token`,
    `run_id`). Those are owned and injected by
    `enqueue_command_payload` after this model is serialised, ensuring
    they cannot be overwritten by the caller.

    `model_dump(mode="json")` yields the flat keys the Go agent expects and
    that `_COMMAND_ADAPTER` deserialises back to `InvokeClaudeCodeCommand`.
    """

    model_config = ConfigDict(frozen=True)
    # The `invocation` body is intentionally permissive at the wire layer;
    # its shape is owned by `domain/coding_agent`.
    invocation: dict[str, Any]
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    limits: dict[str, Any]
    result_spec: dict[str, Any] = Field(default_factory=dict)
    # See InvokeClaudeCodeCommand.skill_path.
    skill_path: str


class AgentConfig(BaseModel):
    """Runtime configuration delivered to the agent via ConfigUpdateCommand.

    max_workspaces is the per-org cap on concurrent Active workspaces, sourced
    from `orgs.workspace_max_count` (NOT NULL, default 4).  PATCH /api/orgs
    writes the column and fan-outs a fresh ConfigUpdate to every agent in the
    org so the new cap takes effect on the next claim.
    The OTLP fields carry the agent's telemetry export destination;
    otlp_token is treated as a secret and must not be logged.
    environment is the OTel deployment.environment.name resource attribute,
    sourced from Settings.environment (required at backend boot).
    api_keys carries per-provider API keys (provider_id → SecretStr) that
    the agent injects as env vars at Claude exec time (e.g. anthropic →
    ANTHROPIC_API_KEY). Values are treated as secrets: Python mode stays
    redacted; wire JSON (model_dump mode="json") unwraps to plaintext so the
    agent receives the real value. Never log.
    """

    model_config = ConfigDict(frozen=True)
    max_workspaces: int = Field(ge=1)
    otlp_endpoint: str | None = None
    otlp_token: SecretStr | None = None
    otlp_dataset: str | None = None
    environment: str | None = None
    api_keys: dict[str, SecretStr] = Field(default_factory=dict)

    @field_serializer("otlp_token", when_used="json")
    def _serialize_otlp_token(self, v: SecretStr | None) -> str | None:
        """Unwrap the bearer at the JSON wire-encode boundary only.

        Pydantic's default SecretStr JSON serialization emits '**********';
        this serializer replaces it with the raw value so the agent receives
        the actual token. Never called by model_dump() (Python mode) so
        str/repr/model_dump stay redacted.
        """
        return v.get_secret_value() if v is not None else None

    @field_serializer("api_keys", when_used="json")
    def _serialize_api_keys(self, v: dict[str, SecretStr]) -> dict[str, str]:
        """Unwrap every per-provider secret at the JSON wire-encode boundary only.

        Python mode (model_dump) keeps SecretStr wrappers so str/repr/logs
        stay redacted. Wire JSON unwraps to plaintext so the agent receives
        the actual values. Never called by model_dump() without mode='json'.
        """
        return {k: s.get_secret_value() for k, s in v.items()}


class ConfigUpdateCommand(BaseModel):
    """Agent-scoped command that delivers runtime config. Carries no workspace_id
    — the agent applies it globally. Workspace commands are gated until the
    agent receives at least one ConfigUpdate.

    `completion_token` follows the same bearer-token discipline as workspace
    commands: minted at claim, echoed on the terminal AgentEvent, verified by
    hash in `record_agent_event`. NULL when the field is absent on the wire
    (pre-token agent versions).
    """

    model_config = ConfigDict(frozen=True)
    command_id: UUID
    traceparent: str
    kind: Literal[AgentCommandKind.CONFIG_UPDATE] = AgentCommandKind.CONFIG_UPDATE
    config: AgentConfig
    # Per-command completion capability token — minted at claim, echoed back on
    # the terminal AgentEvent. Plain str; must serialize its real value to the
    # agent over the wire. NEVER log this field.
    completion_token: str | None = None


class ShutdownCommand(BaseModel):
    """Agent-scoped command requesting the agent to drain. Carries no workspace_id.

    When the agent executes this command, it flips its local lifecycle to
    "draining", accelerates its heartbeat to 5s, and triggers a clean exit
    once all active workspaces have completed.
    """

    model_config = ConfigDict(frozen=True)
    command_id: UUID
    traceparent: str
    kind: Literal[AgentCommandKind.SHUTDOWN] = AgentCommandKind.SHUTDOWN
    completion_token: str | None = None
    run_id: UUID | None = None


class CancelShutdownCommand(BaseModel):
    """Agent-scoped command cancelling an in-progress drain. Carries no workspace_id.

    When the agent executes this command, it flips its local lifecycle back to
    "active" and resumes accepting new workspace commands.
    """

    model_config = ConfigDict(frozen=True)
    command_id: UUID
    traceparent: str
    kind: Literal[AgentCommandKind.CANCEL_SHUTDOWN] = AgentCommandKind.CANCEL_SHUTDOWN
    completion_token: str | None = None
    run_id: UUID | None = None


AgentCommand = Annotated[
    ProvisionWorkspaceCommand
    | WriteFilesCommand
    | RefreshWorkspaceAuthCommand
    | InvokeClaudeCodeCommand
    | CleanupWorkspaceCommand
    | PushBranchCommand
    | ConfigUpdateCommand
    | ShutdownCommand
    | CancelShutdownCommand,
    Field(discriminator="kind"),
]


# ── Events ─────────────────────────────────────────────────────────────


class AgentEventKind(StrEnum):
    PROGRESS = "progress"
    RECEIVED = "received"
    COMPLETED_SUCCESS = "completed_success"
    COMPLETED_FAILURE = "completed_failure"
    COMPLETED_SKIPPED = "completed_skipped"


TERMINAL_EVENT_KINDS: frozenset[AgentEventKind] = frozenset(
    {
        AgentEventKind.COMPLETED_SUCCESS,
        AgentEventKind.COMPLETED_FAILURE,
        AgentEventKind.COMPLETED_SKIPPED,
    }
)


class Artifact(BaseModel):
    """Agent-collected artifact body for an InvokeClaudeCode terminal event.

    Populated when the skill wrote `$TMPDIR/<command_id>.md` and the file fit
    under the agent's size cap. `AgentEvent.artifact_error` explains why this
    is null despite a completed invocation (over-cap or read failure);
    both fields null means the skill legitimately wrote no artifact.
    """

    model_config = ConfigDict(frozen=True)
    body: str


class AgentEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    command_id: UUID
    kind: AgentEventKind
    outcome_label: str | None = None
    outputs: dict[str, Any] = Field(default_factory=dict)
    failure_reason: str | None = None
    attempt: int = 0
    reported_at: datetime
    traceparent: str
    # Echoed back from the command's `completion_token`. Verified (constant-time)
    # against `agent_commands.completion_token_hash` before any side effect.
    # NEVER log this field.
    completion_token: str | None = None
    # Agent-collected artifact content (see Artifact) — set on InvokeClaudeCode
    # terminal events when the skill wrote `$TMPDIR/<command_id>.md`.
    artifact: Artifact | None = None
    # Set when the artifact file exceeded the agent's size cap or otherwise
    # couldn't be read — distinguishes "wrote none" from "wrote too much".
    artifact_error: str | None = None

    def is_terminal(self) -> bool:
        return self.kind in TERMINAL_EVENT_KINDS


class WorkspaceEventKind(StrEnum):
    CREATED = "created"
    READY = "ready"
    EXITED = "exited"
    DESTROYED = "destroyed"
    FAILED = "failed"


class WorkspaceEvent(BaseModel):
    model_config = ConfigDict(frozen=True)
    workspace_id: UUID
    command_id: UUID
    kind: WorkspaceEventKind
    message: str | None = None
    reported_at: datetime


# ── Identity / heartbeat / claim ───────────────────────────────────────


class AgentMetadata(BaseModel):
    """Static OS metadata reported once at identity exchange. All fields optional —
    agents that cannot determine a value omit it."""

    model_config = ConfigDict(frozen=True)
    os: str | None = None
    cpu_count: int | None = None
    memory_bytes: int | None = None


class IdentityExchangeRequest(BaseModel):
    """Body of `POST /api/v1/agent/identity`.

    `kind` identifies the signing mechanism (today: `aws-sts`).
    `payload` is the JSON-encoded sigv4-signed STS GetCallerIdentity envelope.
    `agent_metadata` carries static OS attributes (os, cpu_count, memory_bytes).
    """

    model_config = ConfigDict(frozen=True)
    kind: str
    agent_version: str | None = None
    agent_metadata: AgentMetadata = AgentMetadata()
    payload: str


class IdentityExchangeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    bearer: str
    expires_at: datetime
    renewal_after: datetime
    agent_id: UUID
    instance_id: str
    org_id: UUID


class HeartbeatWorkspaceEntry(BaseModel):
    model_config = ConfigDict(frozen=True)
    workspace_id: UUID
    status: Literal["running", "exited", "unknown"]
    current_command_id: UUID | None = None


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    reported_at: datetime
    workspaces: tuple[HeartbeatWorkspaceEntry, ...] = ()


class HeartbeatResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    reconciled_at: datetime
    forgotten_workspaces: tuple[UUID, ...] = ()


class ClaimRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    wait_seconds: int = Field(ge=0, le=55)
    lifecycle: Literal["unconfigured", "active", "draining", "shutdown"] = "unconfigured"
    # new_workspaces: capacity for new ProvisionWorkspace commands (max_workspaces - active count).
    new_workspaces: int = Field(ge=0, default=0)
    # workspace_ids: idle workspaces awaiting a command (subset of Active workspaces).
    workspace_ids: tuple[UUID, ...] = ()


# ── Agent reference ───────────────────────────────────────────────────


class AgentRef(BaseModel):
    """Minimal agent-pod reference for an agent pod.

    `agent_id` is the row PK used when the caller needs to address the row
    directly (e.g. claim routing). `instance_id` is the role-session-name
    derived from the STS ARN — the backend-assigned stable pod identifier.
    """

    model_config = ConfigDict(frozen=True)
    agent_id: UUID
    instance_id: str


# ── Dispatch context ─────────────────────────────────────────────────────


class DispatchContext(BaseModel):
    """Correlation context passed to every dispatch helper in
    `core/coding_agent` and `core/workspace` (`dispatch_invocation`,
    `dispatch_provision`, `dispatch_cleanup`, `dispatch_auth_refresh`,
    `dispatch_push`, `dispatch_via_workspace`).

    Lives here (not in `domain/pipelines`) so the two `core` modules that
    consume it never import a `domain` module — `core/coding_agent` and
    `core/workspace` sit below `domain/pipelines` in the layer graph.
    `domain/pipelines`' run engine is the sole production constructor.
    """

    model_config = ConfigDict(frozen=True)
    run_id: UUID
    ticket_id: UUID
    stage_execution_id: UUID
    attempt: int
    traceparent: str | None = None


# ── Errors ─────────────────────────────────────────────────────────────


class GatewayError(Exception):
    """Base for gateway errors. HTTP layer maps subclasses to status codes."""


class StaleClaimError(GatewayError):
    """Raised when an event's `command_id` no longer matches a live command row
    (already retired, or token mismatch). The endpoint maps this to `410 Gone` —
    every 410 on `/events` is a real stale-claim, paging-worthy in steady state."""


class CommandEventAck(BaseModel):
    """Response body for a successful `POST /api/v1/commands/{id}/events`.

    `command_event_outcome` is `event_recorded` — the event was persisted and any
    run side-effects fired. The stale-claim case does not return this body;
    it returns `410 Gone` with `{"error": "stale_claim"}`.
    """

    command_event_outcome: str


class UnauthorizedError(GatewayError):
    """Raised by the placeholder identity verifier on empty / missing bearer."""
