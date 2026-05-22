"""core/agent_gateway — wire protocol to WorkspaceAgents.

Five HTTPS endpoints under `/v1/`: identity-exchange, heartbeat,
commands/claim (long-poll), commands/{id}/events, workspaces/{id}/events.
The WebSocket activity stream lands in Phase 8b.

Phase 5 ships:
- Hand-written Pydantic wire types (mirror of `apps/backend/openapi/agent-api.yaml`).
- Per-agent in-memory dispatch FIFO + async long-poll.
- Heartbeat reconciliation (control-plane returns workspaces the agent
  should forget).
- Event ingestion with the stale-claim guard (`410 Gone` on mismatch).
- Placeholder identity verifier (accepts any non-empty bearer); real
  STS-replay verifier lands in Phase 7.
"""

from app.core.agent_gateway import web  # noqa: F401 — registers /v1/* routes
from app.core.agent_gateway.service import (
    _reset_queues_for_tests,
    claim_next,
    enqueue_command,
    queue_depth,
    record_agent_event,
    record_heartbeat,
    record_workspace_event,
)
from app.core.agent_gateway.subscribers import (
    SubscriberRegistry,
)
from app.core.agent_gateway.subscribers import (
    _reset_for_tests as _reset_subscriber_registry_for_tests,
)
from app.core.agent_gateway.subscribers import (
    get_registry as get_subscriber_registry,
)
from app.core.agent_gateway.types import (
    TERMINAL_EVENT_KINDS,
    AgentCommand,
    AgentCommandKind,
    AgentEvent,
    AgentEventKind,
    AuthBlock,
    CleanupWorkspaceCommand,
    CreateWorkspaceCommand,
    GatewayError,
    HeartbeatRequest,
    HeartbeatResponse,
    HeartbeatWorkspaceEntry,
    IdentityExchangeRequest,
    IdentityExchangeResponse,
    InvokeClaudeCodeCommand,
    InvokeClaudeCodeLimits,
    RefreshWorkspaceAuthCommand,
    RepoRef,
    StaleClaimError,
    UnauthorizedError,
    WorkspaceEvent,
    WorkspaceEventKind,
    WriteFilesCommand,
    WriteFilesEntry,
)

__all__ = [
    "TERMINAL_EVENT_KINDS",
    "AgentCommand",
    "AgentCommandKind",
    "AgentEvent",
    "AgentEventKind",
    "AuthBlock",
    "CleanupWorkspaceCommand",
    "CreateWorkspaceCommand",
    "GatewayError",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "HeartbeatWorkspaceEntry",
    "IdentityExchangeRequest",
    "IdentityExchangeResponse",
    "InvokeClaudeCodeCommand",
    "InvokeClaudeCodeLimits",
    "RefreshWorkspaceAuthCommand",
    "RepoRef",
    "StaleClaimError",
    "SubscriberRegistry",
    "UnauthorizedError",
    "WorkspaceEvent",
    "WorkspaceEventKind",
    "WriteFilesCommand",
    "WriteFilesEntry",
    "_reset_queues_for_tests",
    "_reset_subscriber_registry_for_tests",
    "claim_next",
    "enqueue_command",
    "get_subscriber_registry",
    "queue_depth",
    "record_agent_event",
    "record_heartbeat",
    "record_workspace_event",
]
