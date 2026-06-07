"""core/agent_gateway — wire protocol to WorkspaceAgents.

HTTPS endpoints under `/api/v1/agent/`: identity (exchange + graceful-shutdown
DELETE), heartbeat, commands/claim (long-poll), commands/{id}/events,
workspaces/{id}/events, plus a WebSocket activity stream. Agent identity on
every operational channel is derived from the bearer (no `{agent_id}` in URLs).

Provides:
- Hand-written Pydantic wire types (mirror of `apps/backend/openapi/agent-api.yaml`).
- Durable command dispatch via the `agent_commands` table + capacity-pull
  `claim_next` (lease: pending→claimed→delivered→done) with a requeue reaper.
- STS identity verification (sigv4 GetCallerIdentity replay) issuing 1h bearers.
- Liveness sweeper (`compute_agent_liveness_transitions`) + agents-list query.
- Heartbeat reconciliation (control-plane returns workspaces the agent
  should forget).
- Event ingestion with the stale-claim guard (`410 Gone` on mismatch).
- `WorkspaceAgentReportSink` Protocol + single-slot registry; `core/workspace`
  registers its implementation at import so agent_gateway never imports workspace.
"""

from app.core.agent_gateway import bearers, web  # noqa: F401 — registers /v1/* routes
from app.core.agent_gateway.bearers import revoke_all_for_agent, revoke_all_for_arn, revoke_all_for_org
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
    acknowledge_command_received,
    claim_next,
    compute_agent_liveness_transitions,
    connection_status_for_org,
    enqueue_command,
    ensure_agent_row,
    get_agent_info,
    get_command_org_and_payload,
    has_any_reachable_agent,
    list_agents_for_org,
    mark_agent_shutdown,
    pin_command_to_agent,
    record_agent_event,
    record_heartbeat,
    record_workspace_event,
    requeue_stale_claimed,
    retire_command,
    stale_agent_ids,
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
    AgentConfig,
    AgentEvent,
    AgentEventKind,
    AgentMetadata,
    AgentRef,
    AuthBlock,
    ClaimRequest,
    CleanupWorkspaceCommand,
    ConfigUpdateCommand,
    EnumerateSkillsCommand,
    GatewayError,
    HeartbeatRequest,
    HeartbeatResponse,
    HeartbeatWorkspaceEntry,
    IdentityExchangeRequest,
    IdentityExchangeResponse,
    InvokeClaudeCodeCommand,
    InvokeClaudeCodeLimits,
    ProvisionWorkspaceCommand,
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
    "AgentConfig",
    "AgentEvent",
    "AgentEventKind",
    "AgentMetadata",
    "AgentRef",
    "AuthBlock",
    "ClaimRequest",
    "CleanupWorkspaceCommand",
    "ConfigUpdateCommand",
    "EnumerateSkillsCommand",
    "GatewayError",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "HeartbeatWorkspaceEntry",
    "IdentityExchangeRequest",
    "IdentityExchangeResponse",
    "InvokeClaudeCodeCommand",
    "InvokeClaudeCodeLimits",
    "OrgArnRef",
    "ProvisionWorkspaceCommand",
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
    "acknowledge_command_received",
    "bind_subscriber_registry",
    "claim_next",
    "compute_agent_liveness_transitions",
    "connection_status_for_org",
    "enqueue_command",
    "ensure_agent_row",
    "get_agent_info",
    "get_command_org_and_payload",
    "get_report_sink",
    "get_subscriber_registry",
    "has_any_reachable_agent",
    "list_agents_for_org",
    "lookup_org_by_arn",
    "mark_agent_shutdown",
    "pin_command_to_agent",
    "record_agent_event",
    "record_heartbeat",
    "record_workspace_event",
    "register_org_arn_lookup",
    "register_report_sink",
    "requeue_stale_claimed",
    "retire_command",
    "revoke_all_for_agent",
    "revoke_all_for_arn",
    "revoke_all_for_org",
    "shutdown",
    "stale_agent_ids",
]

from app.core.shutdown_registry import register_web_shutdown_hook

register_web_shutdown_hook(shutdown)
