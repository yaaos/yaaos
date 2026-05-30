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

from pydantic import BaseModel, ConfigDict, Field

# ── Discriminator + shared base ─────────────────────────────────────────


class AgentCommandKind(StrEnum):
    CREATE_WORKSPACE = "CreateWorkspace"
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


class CreateWorkspaceCommand(_CommandBase):
    kind: Literal[AgentCommandKind.CREATE_WORKSPACE] = AgentCommandKind.CREATE_WORKSPACE
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
    """

    model_config = ConfigDict(frozen=True)
    max_workspaces: int = Field(ge=1)
    otlp_endpoint: str = ""
    otlp_token: str = ""  # Secret on the wire — never log this field.
    otlp_dataset: str = ""


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
    CreateWorkspaceCommand
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


class IdentityExchangeRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    agent_pod_id: UUID
    version: str | None = None
    signed_request: str


class IdentityExchangeResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    bearer: str
    expires_at: datetime
    agent_id: UUID
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
    active_workspace_ids: tuple[UUID, ...] = ()


# ── Agent reference ───────────────────────────────────────────────────


class AgentRef(BaseModel):
    """Minimal agent-pod reference returned by `pick_agent_for_org`.

    Callers only need `agent_pod_id` to enqueue commands; `agent_id` is
    the row PK used when the caller needs to address the row directly (e.g.
    queue-depth checks).
    """

    model_config = ConfigDict(frozen=True)
    agent_id: UUID
    agent_pod_id: UUID


# ── Errors ─────────────────────────────────────────────────────────────


class GatewayError(Exception):
    """Base for gateway errors. HTTP layer maps subclasses to status codes."""


class StaleClaimError(GatewayError):
    """Raised when an event's `command_id` no longer matches the workspace's
    `current_command_id`. Endpoint returns 410 Gone."""


class UnauthorizedError(GatewayError):
    """Raised by the placeholder identity verifier on empty / missing bearer."""
