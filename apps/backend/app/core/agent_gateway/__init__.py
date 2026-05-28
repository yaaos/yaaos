"""core/agent_gateway — wire protocol to WorkspaceAgents.

Five HTTPS endpoints under `/v1/`: identity-exchange, heartbeat,
commands/claim (long-poll), commands/{id}/events, workspaces/{id}/events,
plus a WebSocket activity stream.

Provides:
- Hand-written Pydantic wire types (mirror of `apps/backend/openapi/agent-api.yaml`).
- Per-agent in-memory dispatch FIFO + async long-poll.
- Heartbeat reconciliation (control-plane returns workspaces the agent
  should forget).
- Event ingestion with the stale-claim guard (`410 Gone` on mismatch).
- A placeholder identity verifier that accepts any non-empty bearer.
"""

from app.core.agent_gateway import bearers, web  # noqa: F401 — registers /v1/* routes
from app.core.agent_gateway.bearers import revoke_all_for_org
from app.core.agent_gateway.models import WorkspaceAgentRow
from app.core.agent_gateway.service import (
    claim_next,
    clear_queues,
    connection_status_for_org,
    enqueue_command,
    ensure_agent_row,
    has_any_reachable_agent,
    pick_agent_for_org,
    queue_depth,
    record_agent_event,
    record_heartbeat,
    record_workspace_event,
)
from app.core.agent_gateway.subscribers import (
    SubscriberRegistry,
    shutdown,
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
    AgentRef,
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
    "AgentRef",
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
    "WorkspaceAgentRow",
    "WorkspaceEvent",
    "WorkspaceEventKind",
    "WriteFilesCommand",
    "WriteFilesEntry",
    "claim_next",
    "clear_queues",
    "connection_status_for_org",
    "enqueue_command",
    "ensure_agent_row",
    "get_subscriber_registry",
    "has_any_reachable_agent",
    "pick_agent_for_org",
    "queue_depth",
    "record_agent_event",
    "record_heartbeat",
    "record_workspace_event",
    "revoke_all_for_org",
    "shutdown",
]

from app.core.shutdown_registry import register_web_shutdown_hook

register_web_shutdown_hook(shutdown)
