// Package workspacetest provides a no-op WorkspaceOps implementation for use
// in tests. Import only from _test.go files — depguard enforces this.
package workspacetest

import (
	"context"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/protocol"
)

// StubHandler satisfies command.WorkspaceOps with no-op success for every
// command kind. Used in integration tests that exercise the dispatch loop
// and pipe plumbing without standing up a real workspace.
type StubHandler struct{}

func (StubHandler) ProvisionWorkspace(_ context.Context, cmd *protocol.ProvisionWorkspaceCommand) (command.ProvisionResult, error) {
	return command.ProvisionResult{
		WorkspaceID: cmd.WorkspaceID,
		Path:        "/stub/" + cmd.WorkspaceID,
		Repo:        cmd.Repo.ExternalID,
		Reused:      false,
	}, nil
}

func (StubHandler) WriteFiles(_ context.Context, cmd *protocol.WriteFilesCommand) (command.WriteFilesResult, error) {
	return command.WriteFilesResult{
		WorkspaceID: cmd.WorkspaceID,
		FilesCount:  len(cmd.Files),
	}, nil
}

func (StubHandler) RefreshAuth(_ context.Context, cmd *protocol.RefreshWorkspaceAuthCommand) (command.RefreshResult, error) {
	return command.RefreshResult{WorkspaceID: cmd.WorkspaceID, Refreshed: true}, nil
}

func (StubHandler) RunClaude(_ context.Context, cmd *protocol.InvokeClaudeCodeCommand) (command.InvokeResult, error) {
	return command.InvokeResult{WorkspaceID: cmd.WorkspaceID}, nil
}

func (StubHandler) Cleanup(_ context.Context, cmd *protocol.CleanupWorkspaceCommand) (command.CleanupResult, error) {
	return command.CleanupResult{WorkspaceID: cmd.WorkspaceID, Destroyed: true}, nil
}

func (StubHandler) PushBranch(_ context.Context, cmd *protocol.PushBranchCommand) (command.PushBranchResult, error) {
	return command.PushBranchResult{WorkspaceID: cmd.WorkspaceID, Pushed: true}, nil
}
