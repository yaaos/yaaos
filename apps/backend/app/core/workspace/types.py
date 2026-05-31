"""Value objects + protocols shared across workspace consumers and plugins.

The Workspace Protocol exposes operations (run a coding-agent CLI inside the
workspace) rather than internal paths. Consumers cannot peek at where files
live; the workspace owns that detail and forward-compats Docker / K8s pod
implementations where "working_dir" wouldn't be a host-filesystem path at all.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.plugin_kit import PluginMeta

# Per-line callback used by `run_coding_agent_cli` to stream stdout in real
# time. When provided, the provider invokes it for each newline-terminated
# chunk from the CLI; when None, stdout is buffered and returned in the
# final `CodingAgentCliResult.stdout` instead.
OnStreamLine = Callable[[bytes], Awaitable[None]]


class WorkspaceStatus(StrEnum):
    CREATING = "creating"
    ACTIVE = "active"
    EXPIRED = "expired"
    DESTROYING = "destroying"
    DESTROYED = "destroyed"
    DESTROY_FAILED = "destroy_failed"


class ResourceCaps(BaseModel):
    cpu_count: int = 2
    memory_mb: int = 2048
    wallclock_seconds: int = 600
    disk_mb: int = 10240


class NetworkPolicy(StrEnum):
    DENY_ALL = "deny_all"
    GITHUB_ONLY = "github_only"
    ALLOW_ALL = "allow_all"


class RepoRefForSpec(BaseModel):
    """Minimal repo identity in a workspace spec. Mirrors domain/vcs RepoRef."""

    plugin_id: str
    external_id: str


class WorkspaceSpec(BaseModel):
    """What's required to provision a workspace. `org_id` is stamped by
    `create_workspace` before the spec reaches the plugin's `provision()`.
    """

    repo: RepoRefForSpec
    sha: str
    branch_name: str | None = None
    # PR base (the target branch being merged into — not necessarily `main`).
    # When provided, the workspace also fetches `base_sha` so the agent can
    # run `git diff base_sha..HEAD` without yaaos inlining the whole diff
    # into the prompt. Optional: standalone workspace calls (no PR context)
    # don't have a base.
    base_sha: str | None = None
    base_branch: str | None = None
    resource_caps: ResourceCaps = Field(default_factory=ResourceCaps)
    network_policy: NetworkPolicy = NetworkPolicy.GITHUB_ONLY
    # Stamped by core/workspace.create_workspace before delegating to provision().
    # Allows the workspace plugin to request auth tokens for the right org via vcs.
    org_id: UUID | None = None


class WorkspaceInfo(BaseModel):
    id: str
    provider_id: str
    sha: str
    status: WorkspaceStatus
    created_at: datetime
    activated_at: datetime | None
    expires_at: datetime
    destroyed_at: datetime | None
    age_seconds: float


class HealthStatus(BaseModel):
    healthy: bool
    message: str = ""
    checked_at: datetime


class CodingAgentCliResult(BaseModel):
    """Result of running a coding-agent CLI inside a workspace.

    Spawn failures (binary not found, can't fork) raise `WorkspaceExecError`.
    Subprocess-spawned-but-exited-non-zero is a normal CodingAgentCliResult
    with `exit_code != 0`. Timeouts produce `timed_out=True`.
    """

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int


class Workspace(Protocol):
    """The view consumers get back from `create_workspace` / `with_workspace`.

    Exposes only operations + identity. Internal paths (in-process tempdir,
    container id, pod name) are implementation details of the provider.
    """

    id: str

    async def info(self) -> WorkspaceInfo: ...

    async def run_coding_agent_cli(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: int | None = None,
        on_stream_line: OnStreamLine | None = None,
    ) -> CodingAgentCliResult: ...

    async def read_text(self, path: str) -> str | None:
        """Read a workspace-relative text file. Returns None if absent.

        Used by the incremental-review anchor re-resolution (plan §6.2
        step 4b) to find each open finding's surrounding-content block in
        the new head.
        """
        ...

    async def write_text(self, path: str, content: str) -> None:
        """Write `content` to a workspace-relative path. Refuses if a file
        already exists at that path — callers materializing review-time
        artefacts (e.g. `.mcp.json`) must not collide with repo files.
        """
        ...


class WorkspaceProvider(Protocol):
    """Plugin contract. Provision returns opaque plugin_state; `run_coding_agent_cli`
    operates against that state. The state shape is private to each plugin (e.g.,
    `{"working_dir": str}` for in-process; `{"container_id": str}` for Docker).
    """

    meta: PluginMeta

    async def provision(self, spec: WorkspaceSpec) -> dict[str, Any]: ...
    async def run_coding_agent_cli(
        self,
        plugin_state: dict[str, Any],
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        timeout_seconds: int | None = None,
        on_stream_line: OnStreamLine | None = None,
    ) -> CodingAgentCliResult: ...
    async def read_text(self, plugin_state: dict[str, Any], path: str) -> str | None: ...
    async def write_text(self, plugin_state: dict[str, Any], path: str, content: str) -> None: ...
    async def destroy(self, plugin_state: dict[str, Any]) -> None: ...
    async def health_check(self) -> HealthStatus: ...


class WorkspaceClaimState(BaseModel):
    """Projection returned by `get_workspace_claim_state`.

    Contains only what `core/agent_gateway` needs to apply the stale-claim guard
    and enqueue workflow-engine continuations — no ORM Row crosses the module
    boundary.
    """

    workspace_id: UUID
    current_holder_workflow_id: UUID | None
    status: str
    # owning agent (`workspace_agents.id`); None for in-memory/legacy rows.
    # agent_gateway compares this against the bearer's agent_id to authorize
    # command-event posts.
    owning_agent_id: UUID | None


class WorkspaceCommandState(BaseModel):
    """Projection returned by `get_workspace_command_state`.

    Contains only what `core/agent_gateway` needs to validate event ownership
    and apply status updates — no ORM Row crosses the module boundary.
    """

    workspace_id: UUID
    current_command_id: UUID | None
    status: str
    # owning agent (`workspace_agents.id`); None for in-memory/legacy rows.
    # agent_gateway compares this against the bearer's agent_id to authorize
    # workspace-event posts.
    owning_agent_id: UUID | None


class WorkspaceError(Exception):
    """Base for workspace errors."""


class WorkspaceProvisionError(WorkspaceError):
    """Raised by plugins when provision() fails."""


class WorkspaceNotFoundError(WorkspaceError, LookupError):
    """Raised by get_workspace() if the id is unknown."""


class WorkspaceExpiredError(WorkspaceError):
    """Raised when a caller acts on an already-expired workspace."""


class WorkspaceDestroyError(WorkspaceError):
    """Raised by plugins when destroy() fails."""


class WorkspaceExecError(WorkspaceError):
    """Raised by run_coding_agent_cli when the subprocess can't even be spawned
    (binary missing, fork failure). Process-ran-and-exited-non-zero is NOT an
    exec error — it's a normal CodingAgentCliResult."""
