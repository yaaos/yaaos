// Package protocol mirrors the wire types defined in
// apps/backend/openapi/agent-api.yaml.
//
// Hand-written. Field tags match the JSON keys the backend emits and accepts.
//
// AgentCommand is a discriminated union over `kind`. The wire form is a
// flat JSON object — the decoder peeks at `kind` and routes into the
// right concrete type via UnmarshalJSON on the wrapper.
package protocol

import (
	"encoding/json"
	"fmt"
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

// AgentCommand is the discriminated union returned by the claim endpoint.
// Exactly one of the typed pointers is non-nil after a successful
// UnmarshalJSON call; the other fields are nil.
type AgentCommand struct {
	Kind                 CommandKind
	CreateWorkspace      *CreateWorkspaceCommand
	WriteFiles           *WriteFilesCommand
	RefreshWorkspaceAuth *RefreshWorkspaceAuthCommand
	InvokeClaudeCode     *InvokeClaudeCodeCommand
	CleanupWorkspace     *CleanupWorkspaceCommand
}

// UnmarshalJSON peeks at the `kind` field to decide which concrete type
// to decode into. Unknown kinds are an error — the supervisor MUST refuse
// to dispatch a command shape it doesn't understand.
func (c *AgentCommand) UnmarshalJSON(data []byte) error {
	var probe struct {
		Kind CommandKind `json:"kind"`
	}
	if err := json.Unmarshal(data, &probe); err != nil {
		return fmt.Errorf("protocol: probe kind: %w", err)
	}
	c.Kind = probe.Kind
	switch probe.Kind {
	case KindCreateWorkspace:
		var v CreateWorkspaceCommand
		if err := json.Unmarshal(data, &v); err != nil {
			return err
		}
		c.CreateWorkspace = &v
	case KindWriteFiles:
		var v WriteFilesCommand
		if err := json.Unmarshal(data, &v); err != nil {
			return err
		}
		c.WriteFiles = &v
	case KindRefreshWorkspaceAuth:
		var v RefreshWorkspaceAuthCommand
		if err := json.Unmarshal(data, &v); err != nil {
			return err
		}
		c.RefreshWorkspaceAuth = &v
	case KindInvokeClaudeCode:
		var v InvokeClaudeCodeCommand
		if err := json.Unmarshal(data, &v); err != nil {
			return err
		}
		c.InvokeClaudeCode = &v
	case KindCleanupWorkspace:
		var v CleanupWorkspaceCommand
		if err := json.Unmarshal(data, &v); err != nil {
			return err
		}
		c.CleanupWorkspace = &v
	default:
		return fmt.Errorf("protocol: unknown command kind %q", probe.Kind)
	}
	return nil
}

// Header returns the embedded CommandHeader from whichever concrete type
// is set. Useful for logging + ack flow regardless of kind.
func (c *AgentCommand) Header() CommandHeader {
	switch c.Kind {
	case KindCreateWorkspace:
		return c.CreateWorkspace.CommandHeader
	case KindWriteFiles:
		return c.WriteFiles.CommandHeader
	case KindRefreshWorkspaceAuth:
		return c.RefreshWorkspaceAuth.CommandHeader
	case KindInvokeClaudeCode:
		return c.InvokeClaudeCode.CommandHeader
	case KindCleanupWorkspace:
		return c.CleanupWorkspace.CommandHeader
	default:
		return CommandHeader{}
	}
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

type WorkspaceEventKind string

const (
	WSEventCreated   WorkspaceEventKind = "created"
	WSEventReady     WorkspaceEventKind = "ready"
	WSEventExited    WorkspaceEventKind = "exited"
	WSEventDestroyed WorkspaceEventKind = "destroyed"
	WSEventFailed    WorkspaceEventKind = "failed"
)

type WorkspaceEvent struct {
	WorkspaceID string             `json:"workspace_id"`
	CommandID   string             `json:"command_id"`
	Kind        WorkspaceEventKind `json:"kind"`
	Message     string             `json:"message,omitempty"`
	ReportedAt  time.Time          `json:"reported_at"`
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
	WaitSeconds int `json:"wait_seconds"`
}
