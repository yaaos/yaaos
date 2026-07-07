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

// CommandKind enumerates the AgentCommand kinds.
type CommandKind string

const (
	KindProvisionWorkspace   CommandKind = "ProvisionWorkspace"
	KindWriteFiles           CommandKind = "WriteFiles"
	KindRefreshWorkspaceAuth CommandKind = "RefreshWorkspaceAuth"
	KindInvokeClaudeCode     CommandKind = "InvokeClaudeCode"
	KindCleanupWorkspace     CommandKind = "CleanupWorkspace"
	KindPushBranch           CommandKind = "PushBranch"
	KindConfigUpdate         CommandKind = "ConfigUpdate"
	KindShutdown             CommandKind = "Shutdown"
	KindCancelShutdown       CommandKind = "CancelShutdown"
)

// CommandHeader is embedded in every concrete AgentCommand. Carries the
// fields the supervisor needs before dispatching to a typed handler.
type CommandHeader struct {
	CommandID   string      `json:"command_id"`
	WorkspaceID string      `json:"workspace_id"`
	Traceparent string      `json:"traceparent"`
	Kind        CommandKind `json:"kind"`
	// CompletionToken is a one-time backend-minted capability the agent
	// echoes on every AgentEvent it posts for this command. The backend
	// verifies it by hash before accepting the event.
	CompletionToken string `json:"completion_token,omitempty"`
	// WorkflowExecutionID is the workflow execution that dispatched this
	// command. Stamped at enqueue time so agent-side spans can carry
	// workflow_id without a separate lookup. Empty for agent-scoped commands
	// (e.g. ConfigUpdate) that do not correlate to a workflow.
	WorkflowExecutionID string `json:"workflow_execution_id,omitempty"`
}

// RepoRef matches the spec's nested `repo` object on ProvisionWorkspace.
//
// Checkout instruction: exactly one of HeadSHA (detached pin — the
// fork-safe fetch-by-SHA path review flows use) or BranchName (named work
// branch; the agent checks it out with `git checkout -B`, tracking the
// remote when it already exists) is set by a well-formed backend command.
// When both are present (legacy shape: BranchName as a `--branch` clone
// hint alongside a required HeadSHA), HeadSHA wins and BranchName is used
// only as a clone-speed hint — see gitClone.
type RepoRef struct {
	PluginID   string `json:"plugin_id"`
	ExternalID string `json:"external_id"`
	CloneURL   string `json:"clone_url"`
	HeadSHA    string `json:"head_sha,omitempty"`
	BaseSHA    string `json:"base_sha,omitempty"`
	BranchName string `json:"branch_name,omitempty"`
}

// AuthBlock matches the spec's ProvisionWorkspace auth + RefreshWorkspaceAuth
// new_token (the latter doesn't reuse this — just shape parallel).
type AuthBlock struct {
	Kind  string `json:"kind"` // github_installation | oauth
	Token string `json:"token"`
}

type ProvisionWorkspaceCommand struct {
	CommandHeader
	Repo           RepoRef   `json:"repo"`
	History        int       `json:"history"`
	Auth           AuthBlock `json:"auth"`
	TTLSeconds     int       `json:"ttl_seconds"`
	MaxIdleSeconds int       `json:"max_idle_seconds"`
	// GitUserName/GitUserEmail are the commit identity RealHandler.ProvisionWorkspace
	// applies via `git config user.name`/`user.email` after clone —
	// backend-supplied constants (e.g. "yaaos" / "yaaos[bot]@users.noreply.github.com").
	// Without them the first skill commit on a named work branch fails.
	GitUserName  string `json:"git_user_name"`
	GitUserEmail string `json:"git_user_email"`
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
	// SkillPath is the backend-computed conventional path of the named
	// skill inside the checkout (`.claude/skills/<skill_name>/SKILL.md`).
	// RealHandler.RunClaude stats this path before spawning claude; absent
	// → completed_failure with failure_reason="skill not found: <path>".
	SkillPath string `json:"skill_path"`
}

type CleanupWorkspaceCommand struct {
	CommandHeader
}

// PushBranchCommand is push-failure recovery only: a bare re-push of the
// workspace's current HEAD after a RefreshWorkspaceAuth credential
// rotation, so claude is never re-run just to retry a push. WorkspaceID
// (on CommandHeader) is required — the workspace is expected to already be
// on its named work branch by ProvisionWorkspace's checkout invariant. No
// kind-specific fields beyond the header.
type PushBranchCommand struct {
	CommandHeader
}

// ── Events ──────────────────────────────────────────────────────────────

type EventKind string

const (
	EventProgress         EventKind = "progress"
	EventReceived         EventKind = "received"
	EventCompletedSuccess EventKind = "completed_success"
	EventCompletedFailure EventKind = "completed_failure"
	EventCompletedSkipped EventKind = "completed_skipped"
)

// Artifact carries the agent-collected file content read from
// `$TMPDIR/<command_id>.md` after an InvokeClaudeCode subprocess exits.
type Artifact struct {
	Body string `json:"body"`
}

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
	// CompletionToken echoes the originating command's CompletionToken so the
	// backend can authorize this event by hash.
	CompletionToken string `json:"completion_token,omitempty"`
	// Artifact is set on InvokeClaudeCode terminal events when the skill
	// wrote `$TMPDIR/<command_id>.md` within the agent's size cap. Nil when
	// the skill wrote no artifact file — a legitimate outcome for review
	// invocations and non-completed main-skill outcomes.
	Artifact *Artifact `json:"artifact,omitempty"`
	// ArtifactError is set when the artifact file exceeded the agent's size
	// cap or otherwise couldn't be read — distinguishes "wrote none" from
	// "wrote too much".
	ArtifactError string `json:"artifact_error,omitempty"`
}

// ── Identity / heartbeat / claim ───────────────────────────────────────

// AgentMetadata carries static OS attributes reported once at identity exchange.
type AgentMetadata struct {
	OS          string `json:"os,omitempty"`
	CPUCount    int    `json:"cpu_count,omitempty"`
	MemoryBytes int64  `json:"memory_bytes,omitempty"`
}

// IdentityExchangeRequest is the body of POST /api/v1/agent/identity.
// Kind identifies the signing mechanism (today: "aws-sts").
// Payload is the JSON-encoded sigv4-signed STS GetCallerIdentity envelope.
type IdentityExchangeRequest struct {
	Kind          string        `json:"kind"`
	AgentVersion  string        `json:"agent_version,omitempty"`
	AgentMetadata AgentMetadata `json:"agent_metadata,omitempty"`
	Payload       string        `json:"payload"`
}

// IdentityExchangeResponse is the response from POST /api/v1/agent/identity.
// InstanceID is the backend-derived pod identifier (role-session-name from
// the STS assumed-role ARN). The agent echoes it in logs but never uses it
// as a key — the backend assigns it.
type IdentityExchangeResponse struct {
	Bearer       string    `json:"bearer"`
	ExpiresAt    time.Time `json:"expires_at"`
	RenewalAfter time.Time `json:"renewal_after"`
	AgentID      string    `json:"agent_id"`
	InstanceID   string    `json:"instance_id"`
	OrgID        string    `json:"org_id"`
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
	WaitSeconds   int      `json:"wait_seconds"`
	Lifecycle     string   `json:"lifecycle"`      // "unconfigured" | "active" | "draining"
	NewWorkspaces int      `json:"new_workspaces"` // capacity for new ProvisionWorkspace commands
	WorkspaceIDs  []string `json:"workspace_ids"`  // idle Active workspaces awaiting a command
}

// AgentConfigWire is the raw JSON wire shape of the runtime configuration
// delivered via ConfigUpdateCommand. The "Wire" suffix distinguishes it from
// command.AgentConfig — the typed form Decode produces, whose OTLPToken and
// ByokSecrets values are secret.Secret. Both are plain strings here (the raw
// wire values); Decode wraps them in secret.Secret immediately, so this struct
// must never be logged before that wrapping. Environment is a plain string —
// the OTel `deployment.environment.name` resource attribute.
type AgentConfigWire struct {
	MaxWorkspaces int               `json:"max_workspaces"`
	OTLPEndpoint  string            `json:"otlp_endpoint"`
	OTLPToken     string            `json:"otlp_token"` // secret — wrapped by Decode; never log raw
	OTLPDataset   string            `json:"otlp_dataset"`
	Environment   string            `json:"environment"`
	ByokSecrets   map[string]string `json:"byok_secrets"` // provider_id → token; wrapped by Decode
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

// ShutdownCommand is an agent-scoped command requesting the agent to drain.
// Carries no workspace_id. When executed, the agent flips its local lifecycle
// to "draining", accelerates its heartbeat cadence to 5s, and triggers a clean
// exit once all active workspaces have completed. See command.ShutdownCommand
// for the typed form used after Decode.
type ShutdownCommand struct {
	CommandHeader
}

// CancelShutdownCommand is an agent-scoped command cancelling an in-progress
// drain. Carries no workspace_id. When executed, the agent flips its local
// lifecycle back to "active" and resumes accepting new workspace commands. See
// command.CancelShutdownCommand for the typed form used after Decode.
type CancelShutdownCommand struct {
	CommandHeader
}
