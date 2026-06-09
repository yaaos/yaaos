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
	Cleanup(ctx context.Context, cmd *protocol.CleanupWorkspaceCommand) (CleanupResult, error)
}

// AgentOps is the capability seam a ConfigUpdateCommand calls. The supervisor
// implements this interface.
type AgentOps interface {
	ApplyConfig(cfg AgentConfig)
}
