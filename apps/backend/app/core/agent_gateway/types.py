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

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer

# ── Discriminator + shared base ─────────────────────────────────────────


class AgentCommandKind(StrEnum):
    PROVISION_WORKSPACE = "ProvisionWorkspace"
    WRITE_FILES = "WriteFiles"
    REFRESH_WORKSPACE_AUTH = "RefreshWorkspaceAuth"
    INVOKE_CLAUDE_CODE = "InvokeClaudeCode"
    CLEANUP_WORKSPACE = "CleanupWorkspace"
    CONFIG_UPDATE = "ConfigUpdate"


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
    # Workflow execution that dispatched this command. Stamped at enqueue time so
    # agent-side spans can carry workflow_id without a separate lookup. NULL for
    # agent-scoped commands (ConfigUpdate) that do not correlate to a workflow.
    workflow_execution_id: UUID | None = None


# ── The five concrete AgentCommand kinds ────────────────────────────────


class RepoRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    plugin_id: str
    external_id: str
    clone_url: str
    head_sha: str
    base_sha: str | None = None
    branch_name: str | None = None


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


class CleanupWorkspaceCommand(_CommandBase):
    kind: Literal[AgentCommandKind.CLEANUP_WORKSPACE] = AgentCommandKind.CLEANUP_WORKSPACE


class AgentConfig(BaseModel):
    """Runtime configuration delivered to the agent via ConfigUpdateCommand.

    max_workspaces is the org/global default cap on concurrent Active workspaces.
    The OTLP fields carry the agent's telemetry export destination;
    otlp_token is treated as a secret and must not be logged.
    environment is the OTel deployment.environment.name resource attribute,
    sourced from Settings.environment (required at backend boot).
    """

    model_config = ConfigDict(frozen=True)
    max_workspaces: int = Field(ge=1)
    otlp_endpoint: str | None = None
    otlp_token: SecretStr | None = None
    otlp_dataset: str | None = None
    environment: str | None = None

    @field_serializer("otlp_token", when_used="json")
    def _serialize_otlp_token(self, v: SecretStr | None) -> str | None:
        """Unwrap the bearer at the JSON wire-encode boundary only.

        Pydantic's default SecretStr JSON serialization emits '**********';
        this serializer replaces it with the raw value so the agent receives
        the actual token. Never called by model_dump() (Python mode) so
        str/repr/model_dump stay redacted.
        """
        return v.get_secret_value() if v is not None else None


class ConfigUpdateCommand(BaseModel):
    """Agent-scoped command that delivers runtime config. Carries no workspace_id
    — the agent applies it globally. Workspace commands are gated until the
    agent receives at least one ConfigUpdate."""

    model_config = ConfigDict(frozen=True)
    command_id: UUID
    traceparent: str
    kind: Literal[AgentCommandKind.CONFIG_UPDATE] = AgentCommandKind.CONFIG_UPDATE
    config: AgentConfig


AgentCommand = Annotated[
    ProvisionWorkspaceCommand
    | WriteFilesCommand
    | RefreshWorkspaceAuthCommand
    | InvokeClaudeCodeCommand
    | CleanupWorkspaceCommand
    | ConfigUpdateCommand,
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
    lifecycle: Literal["unconfigured", "configured"] = "unconfigured"
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


# ── Errors ─────────────────────────────────────────────────────────────


class GatewayError(Exception):
    """Base for gateway errors. HTTP layer maps subclasses to status codes."""


class StaleClaimError(GatewayError):
    """Raised when an event's `command_id` no longer matches the workspace's
    `current_command_id`. Endpoint returns 410 Gone."""


class UnauthorizedError(GatewayError):
    """Raised by the placeholder identity verifier on empty / missing bearer."""
