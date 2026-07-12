// Package command owns the polymorphic Command interface, all concrete command
// types, the WorkspaceOps/AgentOps capability seams, typed results, and the
// Decode factory. Nothing in this package executes real I/O — that lives in
// the ops implementations (internal/workspace for WorkspaceOps, supervisor for
// AgentOps).
package command

import (
	"context"

	"github.com/yaaos/agent/internal/protocol"
)

// WorkspaceOps is the capability seam a WorkspaceCommand calls to do its work.
// The concrete implementation is internal/workspace.RealHandler; tests supply
// a fake. Each method receives the typed wire command so implementations pull
// fields directly.
//
// See apps/agent/docs/command.md for the layer contract.
type WorkspaceOps interface {
	ProvisionWorkspace(ctx context.Context, cmd *protocol.ProvisionWorkspaceCommand) (ProvisionResult, error)
	WriteFiles(ctx context.Context, cmd *protocol.WriteFilesCommand) (WriteFilesResult, error)
	RefreshAuth(ctx context.Context, cmd *protocol.RefreshWorkspaceAuthCommand) (RefreshResult, error)
	RunClaude(ctx context.Context, cmd *protocol.InvokeClaudeCodeCommand) (InvokeResult, error)
	// RunCodex takes the command-layer type (not the raw proto) so the
	// secret-wrapped AuthJSON is accessible without carrying plaintext.
	RunCodex(ctx context.Context, cmd *InvokeCodexCommand) (InvokeResult, error)
	Cleanup(ctx context.Context, cmd *protocol.CleanupWorkspaceCommand) (CleanupResult, error)
	PushBranch(ctx context.Context, cmd *protocol.PushBranchCommand) (PushBranchResult, error)
}

// AgentOps is the capability seam AgentCommands call to act on the supervisor.
// The supervisor implements this interface; tests supply a fake.
type AgentOps interface {
	ApplyConfig(cfg AgentConfig)
	// RequestShutdown transitions the agent's local lifecycle to "draining".
	// The agent stops accepting new workspaces, accelerates its heartbeat to 5s,
	// and exits once all active workspaces have completed.
	RequestShutdown()
	// CancelShutdown reverts a prior RequestShutdown, transitioning the agent's
	// local lifecycle back to "active". The agent resumes accepting new workspaces.
	CancelShutdown()
}
