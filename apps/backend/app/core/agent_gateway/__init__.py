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
from app.core.agent_gateway.bearers import (
    revoke_all_for_agent,
    revoke_all_for_arn,
    revoke_all_for_org,
    set_bearer_verify_for_tests,
)
from app.core.agent_gateway.byok_provider import (
    clear_byok_secrets_provider,
    get_byok_secrets_provider,
    register_byok_secrets_provider,
)
from app.core.agent_gateway.org_arn_lookup import (
    OrgArnRef,
    lookup_org_by_arn,
    register_org_arn_lookup,
)
from app.core.agent_gateway.rate_limit import delete_rate_limits as delete_identity_exchange_rate_limits
from app.core.agent_gateway.report_sink import (
    WorkspaceAgentReportSink,
    WorkspaceEventOutcome,
    WorkspaceEventReport,
    get_report_sink,
    register_report_sink,
)
from app.core.agent_gateway.run_sink import (
    AgentEventEnrichment,
    AgentRunSink,
    clear_run_sink,
    get_run_sink,
    register_run_sink,
)
from app.core.agent_gateway.service import (
    CancelShutdownResult,
    ShutdownResult,
    acknowledge_command_received,
    cancel_shutdown_agents,
    claim_next,
    compute_agent_liveness_transitions,
    connection_status_for_org,
    enqueue_agent_event,
    enqueue_command,
    enqueue_command_payload,
    enqueue_config_update_for_all_org_agents,
    ensure_agent_row,
    get_agent_info,
    get_command_org_and_payload,
    get_command_run_id,
    get_command_status,
    has_any_reachable_agent,
    list_agents_for_org,
    mark_agent_configured,
    mark_agent_disconnected,
    mark_agent_offline,
    mark_agent_shutdown_complete,
    pin_command_to_agent,
    record_agent_event,
    record_heartbeat,
    record_workspace_event,
    register_agent_event_consumer,
    requeue_stale_claimed,
    retire_command,
    shutdown_agents,
    stale_agent_ids,
)
from app.core.agent_gateway.sts_verifier import set_sts_verify_for_tests
from app.core.agent_gateway.subscribers import (
    set_subscriber_registry_for_tests,
    shutdown,
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
    Artifact,
    AuthBlock,
    CancelShutdownCommand,
    ClaimRequest,
    CleanupWorkspaceCommand,
    ConfigUpdateCommand,
    DispatchContext,
    GatewayError,
    HeartbeatRequest,
    HeartbeatResponse,
    HeartbeatWorkspaceEntry,
    IdentityExchangeRequest,
    IdentityExchangeResponse,
    InvokeClaudeCodeCommand,
    InvokeClaudeCodeFields,
    InvokeClaudeCodeLimits,
    ProvisionWorkspaceCommand,
    PushBranchCommand,
    RefreshWorkspaceAuthCommand,
    RepoRef,
    ShutdownCommand,
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
    "AgentEventEnrichment",
    "AgentEventKind",
    "AgentMetadata",
    "AgentRef",
    "AgentRunSink",
    "Artifact",
    "AuthBlock",
    "CancelShutdownCommand",
    "CancelShutdownResult",
    "ClaimRequest",
    "CleanupWorkspaceCommand",
    "ConfigUpdateCommand",
    "DispatchContext",
    "GatewayError",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "HeartbeatWorkspaceEntry",
    "IdentityExchangeRequest",
    "IdentityExchangeResponse",
    "InvokeClaudeCodeCommand",
    "InvokeClaudeCodeFields",
    "InvokeClaudeCodeLimits",
    "OrgArnRef",
    "ProvisionWorkspaceCommand",
    "PushBranchCommand",
    "RefreshWorkspaceAuthCommand",
    "RepoRef",
    "ShutdownCommand",
    "ShutdownResult",
    "StaleClaimError",
    "UnauthorizedError",
    "WorkspaceAgentReportSink",
    "WorkspaceEvent",
    "WorkspaceEventKind",
    "WorkspaceEventOutcome",
    "WorkspaceEventReport",
    "WriteFilesCommand",
    "WriteFilesEntry",
    "acknowledge_command_received",
    "cancel_shutdown_agents",
    "claim_next",
    "clear_byok_secrets_provider",
    "clear_run_sink",
    "compute_agent_liveness_transitions",
    "connection_status_for_org",
    "delete_identity_exchange_rate_limits",
    "enqueue_agent_event",
    "enqueue_command",
    "enqueue_command_payload",
    "enqueue_config_update_for_all_org_agents",
    "ensure_agent_row",
    "get_agent_info",
    "get_byok_secrets_provider",
    "get_command_org_and_payload",
    "get_command_run_id",
    "get_command_status",
    "get_report_sink",
    "get_run_sink",
    "has_any_reachable_agent",
    "list_agents_for_org",
    "lookup_org_by_arn",
    "mark_agent_configured",
    "mark_agent_disconnected",
    "mark_agent_offline",
    "mark_agent_shutdown_complete",
    "pin_command_to_agent",
    "record_agent_event",
    "record_heartbeat",
    "record_workspace_event",
    "register_agent_event_consumer",
    "register_byok_secrets_provider",
    "register_org_arn_lookup",
    "register_report_sink",
    "register_run_sink",
    "requeue_stale_claimed",
    "retire_command",
    "revoke_all_for_agent",
    "revoke_all_for_arn",
    "revoke_all_for_org",
    "set_bearer_verify_for_tests",
    "set_sts_verify_for_tests",
    "set_subscriber_registry_for_tests",
    "shutdown",
    "shutdown_agents",
    "stale_agent_ids",
]

# shutdown() is registered with register_web_shutdown_hook in subscribers.py
# at module import time — no second registration needed here.
