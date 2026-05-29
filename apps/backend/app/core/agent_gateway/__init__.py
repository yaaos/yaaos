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
- `WorkspaceAgentReportSink` Protocol + single-slot registry; `core/workspace`
  registers its implementation at import so agent_gateway never imports workspace.
"""

from app.core.agent_gateway import bearers, web  # noqa: F401 — registers /v1/* routes
from app.core.agent_gateway.bearers import revoke_all_for_org
from app.core.agent_gateway.org_arn_lookup import (
    OrgArnRef,
    lookup_org_by_arn,
    register_org_arn_lookup,
)
from app.core.agent_gateway.report_sink import (
    WorkspaceAgentReportSink,
    WorkspaceEventOutcome,
    WorkspaceEventReport,
    get_report_sink,
    register_report_sink,
)
from app.core.agent_gateway.service import (
    AgentQueues,
    bind_agent_queues,
    claim_next,
    connection_status_for_org,
    enqueue_command,
    ensure_agent_row,
    get_agent_info,
    has_any_reachable_agent,
    has_stale_agents_for_org,
    pick_agent_for_org,
    queue_depth,
    record_agent_event,
    record_heartbeat,
    record_workspace_event,
)
from app.core.agent_gateway.subscribers import (
    SubscriberRegistry,
    bind_subscriber_registry,
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
    "AgentQueues",
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
    "OrgArnRef",
    "RefreshWorkspaceAuthCommand",
    "RepoRef",
    "StaleClaimError",
    "SubscriberRegistry",
    "UnauthorizedError",
    "WorkspaceAgentReportSink",
    "WorkspaceEvent",
    "WorkspaceEventKind",
    "WorkspaceEventOutcome",
    "WorkspaceEventReport",
    "WriteFilesCommand",
    "WriteFilesEntry",
    "bind_agent_queues",
    "bind_subscriber_registry",
    "claim_next",
    "connection_status_for_org",
    "enqueue_command",
    "ensure_agent_row",
    "get_agent_info",
    "get_report_sink",
    "get_subscriber_registry",
    "has_any_reachable_agent",
    "has_stale_agents_for_org",
    "lookup_org_by_arn",
    "pick_agent_for_org",
    "queue_depth",
    "record_agent_event",
    "record_heartbeat",
    "record_workspace_event",
    "register_org_arn_lookup",
    "register_report_sink",
    "revoke_all_for_org",
    "shutdown",
]

from app.core.shutdown_registry import register_web_shutdown_hook

register_web_shutdown_hook(shutdown)
