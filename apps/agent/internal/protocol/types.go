// Package protocol mirrors the wire types defined in
// apps/backend/openapi/agent-api.yaml.
//
// Hand-written. Field tags match the JSON keys the backend emits and accepts.
//
// Wire commands arrive as flat JSON discriminated by `kind`. Consumers call
// command.Decode (internal/command) to unmarshal raw bytes into the concrete
// typed command — this package does not decode the union itself.
package protocol

import (
	"encoding/json"
	"time"
)

// CommandKind enumerates the five AgentCommand kinds.
type CommandKind string

const (
	KindCreateWorkspace      CommandKind = "CreateWorkspace"
	KindWriteFiles           CommandKind = "WriteFiles"
	KindRefreshWorkspaceAuth CommandKind = "RefreshWorkspaceAuth"
	KindInvokeClaudeCode     CommandKind = "InvokeClaudeCode"
	KindCleanupWorkspace     CommandKind = "CleanupWorkspace"
	KindConfigUpdate         CommandKind = "ConfigUpdate"
)

// CommandHeader is embedded in every concrete AgentCommand. Carries the
// fields the supervisor needs before dispatching to a typed handler.
type CommandHeader struct {
	CommandID   string      `json:"command_id"`
	WorkspaceID string      `json:"workspace_id"`
	Traceparent string      `json:"traceparent"`
	Kind        CommandKind `json:"kind"`
}

// RepoRef matches the spec's nested `repo` object on CreateWorkspace.
type RepoRef struct {
	PluginID   string `json:"plugin_id"`
	ExternalID string `json:"external_id"`
	CloneURL   string `json:"clone_url"`
	HeadSHA    string `json:"head_sha"`
	BaseSHA    string `json:"base_sha,omitempty"`
	BranchName string `json:"branch_name,omitempty"`
}

// AuthBlock matches the spec's CreateWorkspace auth + RefreshWorkspaceAuth
// new_token (the latter doesn't reuse this — just shape parallel).
type AuthBlock struct {
	Kind  string `json:"kind"` // github_installation | oauth
	Token string `json:"token"`
}

type CreateWorkspaceCommand struct {
	CommandHeader
	Repo           RepoRef   `json:"repo"`
	History        int       `json:"history"`
	Auth           AuthBlock `json:"auth"`
	TTLSeconds     int       `json:"ttl_seconds"`
	MaxIdleSeconds int       `json:"max_idle_seconds"`
}

type WriteFilesEntry struct {
	Path    string `json:"path"`
	Content string `json:"content"`
	Mode    string `json:"mode,omitempty"`
}

type WriteFilesCommand struct {
	CommandHeader
	Files []WriteFilesEntry `json:"files"`
}

type RefreshWorkspaceAuthCommand struct {
	CommandHeader
	NewToken string `json:"new_token"`
}

type InvokeClaudeCodeLimits struct {
	WallclockSeconds int `json:"wallclock_seconds"`
}

type InvokeClaudeCodeCommand struct {
	CommandHeader
	// Invocation is intentionally permissive at the wire layer — its shape
	// is owned by domain/coding_agent.
	Invocation json.RawMessage        `json:"invocation"`
	MCPServers []map[string]any       `json:"mcp_servers,omitempty"`
	Limits     InvokeClaudeCodeLimits `json:"limits"`
	ResultSpec map[string]any         `json:"result_spec,omitempty"`
}

type CleanupWorkspaceCommand struct {
	CommandHeader
}

// ── Events ──────────────────────────────────────────────────────────────

type EventKind string

const (
	EventProgress         EventKind = "progress"
	EventCompletedSuccess EventKind = "completed_success"
	EventCompletedFailure EventKind = "completed_failure"
	EventCompletedSkipped EventKind = "completed_skipped"
)

// AgentEvent is the agent → backend POST body for /api/v1/commands/{id}/events.
type AgentEvent struct {
	CommandID     string         `json:"command_id"`
	Kind          EventKind      `json:"kind"`
	OutcomeLabel  string         `json:"outcome_label,omitempty"`
	Outputs       map[string]any `json:"outputs,omitempty"`
	FailureReason string         `json:"failure_reason,omitempty"`
	Attempt       int            `json:"attempt,omitempty"`
	ReportedAt    time.Time      `json:"reported_at"`
	Traceparent   string         `json:"traceparent"`
}

// ── Identity / heartbeat / claim ───────────────────────────────────────

type IdentityExchangeRequest struct {
	AgentPodID    string `json:"agent_pod_id"`
	Version       string `json:"version,omitempty"`
	SignedRequest string `json:"signed_request"`
}

type IdentityExchangeResponse struct {
	Bearer    string    `json:"bearer"`
	ExpiresAt time.Time `json:"expires_at"`
	AgentID   string    `json:"agent_id"`
	OrgID     string    `json:"org_id"`
}

type HeartbeatWorkspaceEntry struct {
	WorkspaceID      string `json:"workspace_id"`
	Status           string `json:"status"` // running | exited | unknown
	CurrentCommandID string `json:"current_command_id,omitempty"`
}

type HeartbeatRequest struct {
	ReportedAt time.Time                 `json:"reported_at"`
	Workspaces []HeartbeatWorkspaceEntry `json:"workspaces"`
}

type HeartbeatResponse struct {
	ReconciledAt        time.Time `json:"reconciled_at"`
	ForgottenWorkspaces []string  `json:"forgotten_workspaces,omitempty"`
}

type ClaimRequest struct {
	WaitSeconds        int      `json:"wait_seconds"`
	Lifecycle          string   `json:"lifecycle"`            // "unconfigured" | "configured"
	ActiveWorkspaceIDs []string `json:"active_workspace_ids"` // IDs of Active-state workspaces
}

// AgentConfigWire is the raw JSON wire shape of the runtime configuration
// delivered via ConfigUpdateCommand. The "Wire" suffix distinguishes it from
// command.AgentConfig — the typed form Decode produces, whose OTLPToken is a
// secret.Secret. OTLPToken is a plain string here (the raw wire value); Decode
// wraps it in secret.Secret immediately, so this struct must never be logged
// before that wrapping.
type AgentConfigWire struct {
	MaxWorkspaces int    `json:"max_workspaces"`
	OTLPEndpoint  string `json:"otlp_endpoint"`
	OTLPToken     string `json:"otlp_token"` // secret — wrapped by Decode; never log raw
	OTLPDataset   string `json:"otlp_dataset"`
}

// ConfigUpdateCommand is the agent-scoped command that delivers runtime
// configuration. It applies globally to the agent process; the workspace_id
// inherited from the embedded CommandHeader is always empty for this kind.
// The config payload is nested under `config` (the workspace commands are
// flat). See command.ConfigUpdateCommand for the typed form used after Decode.
type ConfigUpdateCommand struct {
	CommandHeader
	Config AgentConfigWire `json:"config"`
}
