package command

import (
	"context"
	"encoding/json"
	"time"

	"github.com/yaaos/agent/internal/protocol"
)

// Per-kind default timeouts, owned by the command type.
const (
	defaultCreateWorkspaceTimeout      = 5 * time.Minute
	defaultWriteFilesTimeout           = 30 * time.Second
	defaultRefreshWorkspaceAuthTimeout = 30 * time.Second
	defaultCleanupWorkspaceTimeout     = 30 * time.Second
	defaultInvokeClaudeCodeTimeout     = 15 * time.Minute
)

// ── CreateWorkspaceCommand ────────────────────────────────────────────────────

// CreateWorkspaceCommand clones a repo into a fresh temp dir and registers the
// workspace. It implements WorkspaceCommand.
type CreateWorkspaceCommand struct {
	Proto protocol.CreateWorkspaceCommand
}

// Header implements Command.
func (c *CreateWorkspaceCommand) Header() protocol.CommandHeader {
	return c.Proto.CommandHeader
}

// Timeout implements Command. Git clone may be slow for large repos.
func (c *CreateWorkspaceCommand) Timeout() time.Duration {
	return defaultCreateWorkspaceTimeout
}

// Execute calls ops.CloneWorkspace and returns a CreateResult. The result's
// Path field is what the supervisor's registry keys on.
func (c *CreateWorkspaceCommand) Execute(ctx context.Context, ops WorkspaceOps) (Result, error) {
	return ops.CloneWorkspace(ctx, &c.Proto)
}

// MarshalWire returns the flat JSON representation of this command.
func (c *CreateWorkspaceCommand) MarshalWire() ([]byte, error) { return json.Marshal(c.Proto) }

// SetTraceparent implements Command.
func (c *CreateWorkspaceCommand) SetTraceparent(tp string) { c.Proto.Traceparent = tp }

// ── WriteFilesCommand ─────────────────────────────────────────────────────────

// WriteFilesCommand writes a batch of files into an existing workspace dir.
type WriteFilesCommand struct {
	Proto protocol.WriteFilesCommand
}

// Header implements Command.
func (c *WriteFilesCommand) Header() protocol.CommandHeader {
	return c.Proto.CommandHeader
}

// Timeout implements Command.
func (c *WriteFilesCommand) Timeout() time.Duration {
	return defaultWriteFilesTimeout
}

// Execute calls ops.WriteFiles and returns a WriteFilesResult.
func (c *WriteFilesCommand) Execute(ctx context.Context, ops WorkspaceOps) (Result, error) {
	return ops.WriteFiles(ctx, &c.Proto)
}

// MarshalWire returns the flat JSON representation of this command.
func (c *WriteFilesCommand) MarshalWire() ([]byte, error) { return json.Marshal(c.Proto) }

// SetTraceparent implements Command.
func (c *WriteFilesCommand) SetTraceparent(tp string) { c.Proto.Traceparent = tp }

// ── RefreshWorkspaceAuthCommand ───────────────────────────────────────────────

// RefreshWorkspaceAuthCommand rotates the auth token held in the workspace slot.
type RefreshWorkspaceAuthCommand struct {
	Proto protocol.RefreshWorkspaceAuthCommand
}

// Header implements Command.
func (c *RefreshWorkspaceAuthCommand) Header() protocol.CommandHeader {
	return c.Proto.CommandHeader
}

// Timeout implements Command.
func (c *RefreshWorkspaceAuthCommand) Timeout() time.Duration {
	return defaultRefreshWorkspaceAuthTimeout
}

// Execute calls ops.RefreshAuth and returns a RefreshResult.
func (c *RefreshWorkspaceAuthCommand) Execute(ctx context.Context, ops WorkspaceOps) (Result, error) {
	return ops.RefreshAuth(ctx, &c.Proto)
}

// MarshalWire returns the flat JSON representation of this command.
func (c *RefreshWorkspaceAuthCommand) MarshalWire() ([]byte, error) { return json.Marshal(c.Proto) }

// SetTraceparent implements Command.
func (c *RefreshWorkspaceAuthCommand) SetTraceparent(tp string) { c.Proto.Traceparent = tp }

// ── InvokeClaudeCodeCommand ───────────────────────────────────────────────────

// InvokeClaudeCodeCommand runs the Claude Code subprocess inside the workspace.
// Timeout prefers the wire-supplied Limits.WallclockSeconds; falls back to
// defaultInvokeClaudeCodeTimeout when the wire value is absent or zero.
type InvokeClaudeCodeCommand struct {
	Proto protocol.InvokeClaudeCodeCommand
}

// Header implements Command.
func (c *InvokeClaudeCodeCommand) Header() protocol.CommandHeader {
	return c.Proto.CommandHeader
}

// Timeout implements Command. Reads Limits.WallclockSeconds from the wire;
// the control plane sets this per invocation so the agent never caps a
// legitimately long run. Falls back to 15 m if the field is absent or zero.
func (c *InvokeClaudeCodeCommand) Timeout() time.Duration {
	if c.Proto.Limits.WallclockSeconds > 0 {
		return time.Duration(c.Proto.Limits.WallclockSeconds) * time.Second
	}
	return defaultInvokeClaudeCodeTimeout
}

// Execute calls ops.RunClaude and returns an InvokeResult.
func (c *InvokeClaudeCodeCommand) Execute(ctx context.Context, ops WorkspaceOps) (Result, error) {
	return ops.RunClaude(ctx, &c.Proto)
}

// MarshalWire returns the flat JSON representation of this command.
func (c *InvokeClaudeCodeCommand) MarshalWire() ([]byte, error) { return json.Marshal(c.Proto) }

// SetTraceparent implements Command.
func (c *InvokeClaudeCodeCommand) SetTraceparent(tp string) { c.Proto.Traceparent = tp }

// ── CleanupWorkspaceCommand ───────────────────────────────────────────────────

// CleanupWorkspaceCommand tears down a workspace dir.
type CleanupWorkspaceCommand struct {
	Proto protocol.CleanupWorkspaceCommand
}

// Header implements Command.
func (c *CleanupWorkspaceCommand) Header() protocol.CommandHeader {
	return c.Proto.CommandHeader
}

// Timeout implements Command.
func (c *CleanupWorkspaceCommand) Timeout() time.Duration {
	return defaultCleanupWorkspaceTimeout
}

// Execute calls ops.Cleanup and returns a CleanupResult.
func (c *CleanupWorkspaceCommand) Execute(ctx context.Context, ops WorkspaceOps) (Result, error) {
	return ops.Cleanup(ctx, &c.Proto)
}

// MarshalWire returns the flat JSON representation of this command.
func (c *CleanupWorkspaceCommand) MarshalWire() ([]byte, error) { return json.Marshal(c.Proto) }

// SetTraceparent implements Command.
func (c *CleanupWorkspaceCommand) SetTraceparent(tp string) { c.Proto.Traceparent = tp }
