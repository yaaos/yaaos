package command_test

import (
	"context"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/protocol"
)

// ── fakes ─────────────────────────────────────────────────────────────────────

// fakeWorkspaceOps records which ops were called and returns configurable
// results.
type fakeWorkspaceOps struct {
	provisionResult command.ProvisionResult
	provisionErr    error
	writeResult     command.WriteFilesResult
	writeErr        error
	refreshResult   command.RefreshResult
	refreshErr      error
	invokeResult    command.InvokeResult
	invokeErr       error
	cleanupResult   command.CleanupResult
	cleanupErr      error

	provisionCalled bool
	writeCalled     bool
	refreshCalled   bool
	invokeCalled    bool
	cleanupCalled   bool
}

func (f *fakeWorkspaceOps) ProvisionWorkspace(ctx context.Context, cmd *protocol.ProvisionWorkspaceCommand) (command.ProvisionResult, error) {
	f.provisionCalled = true
	return f.provisionResult, f.provisionErr
}

func (f *fakeWorkspaceOps) WriteFiles(ctx context.Context, cmd *protocol.WriteFilesCommand) (command.WriteFilesResult, error) {
	f.writeCalled = true
	return f.writeResult, f.writeErr
}

func (f *fakeWorkspaceOps) RefreshAuth(ctx context.Context, cmd *protocol.RefreshWorkspaceAuthCommand) (command.RefreshResult, error) {
	f.refreshCalled = true
	return f.refreshResult, f.refreshErr
}

func (f *fakeWorkspaceOps) RunClaude(ctx context.Context, cmd *protocol.InvokeClaudeCodeCommand) (command.InvokeResult, error) {
	f.invokeCalled = true
	return f.invokeResult, f.invokeErr
}

func (f *fakeWorkspaceOps) Cleanup(ctx context.Context, cmd *protocol.CleanupWorkspaceCommand) (command.CleanupResult, error) {
	f.cleanupCalled = true
	return f.cleanupResult, f.cleanupErr
}

// fakeAgentOps records the config passed to ApplyConfig.
type fakeAgentOps struct {
	appliedConfig *command.AgentConfig
}

func (f *fakeAgentOps) ApplyConfig(cfg command.AgentConfig) {
	f.appliedConfig = &cfg
}

// ── ProvisionWorkspaceCommand.Execute ────────────────────────────────────────

func TestProvisionWorkspaceCommand_Execute(t *testing.T) {
	ops := &fakeWorkspaceOps{
		provisionResult: command.ProvisionResult{
			Path:    "/tmp/ws-1",
			Repo:    "org/repo",
			HeadSHA: "abc123",
			Branch:  "main",
			Reused:  false,
		},
	}
	cmd := &command.ProvisionWorkspaceCommand{
		Proto: protocol.ProvisionWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   "cmd-1",
				WorkspaceID: "ws-1",
				Kind:        protocol.KindProvisionWorkspace,
			},
		},
	}
	res, err := cmd.Execute(context.Background(), ops)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if !ops.provisionCalled {
		t.Error("ProvisionWorkspace not called")
	}
	cr, ok := res.(command.ProvisionResult)
	if !ok {
		t.Fatalf("result type = %T, want command.ProvisionResult", res)
	}
	if cr.Path != "/tmp/ws-1" {
		t.Errorf("Path = %q, want /tmp/ws-1", cr.Path)
	}

	// toWire must carry the legacy keys the backend expects.
	wire := res.ToWire()
	assertWireKey(t, wire, "path", "/tmp/ws-1")
	assertWireKey(t, wire, "repo", "org/repo")
	assertWireKey(t, wire, "head_sha", "abc123")
	assertWireKey(t, wire, "branch", "main")
	if wire["reused"] != false {
		t.Errorf("wire[reused] = %v, want false", wire["reused"])
	}
}

// TestProvisionWorkspaceCommand_Execute_Reused checks the handler returns the
// right wire shape for the reused=true path.
func TestProvisionWorkspaceCommand_Execute_Reused(t *testing.T) {
	ops := &fakeWorkspaceOps{
		provisionResult: command.ProvisionResult{
			Path:   "/tmp/ws-1",
			Reused: true,
		},
	}
	cmd := &command.ProvisionWorkspaceCommand{
		Proto: protocol.ProvisionWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   "cmd-1r",
				WorkspaceID: "ws-1",
				Kind:        protocol.KindProvisionWorkspace,
			},
		},
	}
	res, err := cmd.Execute(context.Background(), ops)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	wire := res.ToWire()
	if wire["reused"] != true {
		t.Errorf("wire[reused] = %v, want true", wire["reused"])
	}
}

// ── WriteFilesCommand.Execute ─────────────────────────────────────────────────

func TestWriteFilesCommand_Execute(t *testing.T) {
	ops := &fakeWorkspaceOps{
		writeResult: command.WriteFilesResult{
			WorkspaceID: "ws-2",
			FilesCount:  3,
		},
	}
	cmd := &command.WriteFilesCommand{
		Proto: protocol.WriteFilesCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   "cmd-2",
				WorkspaceID: "ws-2",
				Kind:        protocol.KindWriteFiles,
			},
		},
	}
	res, err := cmd.Execute(context.Background(), ops)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if !ops.writeCalled {
		t.Error("WriteFiles not called")
	}
	wire := res.ToWire()
	assertWireKey(t, wire, "workspace_id", "ws-2")
	if wire["files_count"] != 3 {
		t.Errorf("wire[files_count] = %v, want 3", wire["files_count"])
	}
}

// ── RefreshWorkspaceAuthCommand.Execute ───────────────────────────────────────

func TestRefreshWorkspaceAuthCommand_Execute(t *testing.T) {
	ops := &fakeWorkspaceOps{
		refreshResult: command.RefreshResult{
			WorkspaceID: "ws-3",
			Refreshed:   true,
		},
	}
	cmd := &command.RefreshWorkspaceAuthCommand{
		Proto: protocol.RefreshWorkspaceAuthCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   "cmd-3",
				WorkspaceID: "ws-3",
				Kind:        protocol.KindRefreshWorkspaceAuth,
			},
		},
	}
	res, err := cmd.Execute(context.Background(), ops)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if !ops.refreshCalled {
		t.Error("RefreshAuth not called")
	}
	wire := res.ToWire()
	assertWireKey(t, wire, "workspace_id", "ws-3")
	if wire["refreshed"] != true {
		t.Errorf("wire[refreshed] = %v, want true", wire["refreshed"])
	}
}

// ── InvokeClaudeCodeCommand.Execute ───────────────────────────────────────────

func TestInvokeClaudeCodeCommand_Execute(t *testing.T) {
	ops := &fakeWorkspaceOps{
		invokeResult: command.InvokeResult{
			WorkspaceID: "ws-4",
			ExecResult: command.ExecResult{
				ExitCode: 0,
				Stdout:   `{"result":"ok"}`,
				Stderr:   "",
				Duration: 500 * time.Millisecond,
			},
		},
	}
	cmd := &command.InvokeClaudeCodeCommand{
		Proto: protocol.InvokeClaudeCodeCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   "cmd-4",
				WorkspaceID: "ws-4",
				Kind:        protocol.KindInvokeClaudeCode,
			},
			Limits: protocol.InvokeClaudeCodeLimits{WallclockSeconds: 60},
		},
	}
	res, err := cmd.Execute(context.Background(), ops)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if !ops.invokeCalled {
		t.Error("RunClaude not called")
	}
	wire := res.ToWire()
	assertWireKey(t, wire, "workspace_id", "ws-4")
	if wire["exit_code"] != 0 {
		t.Errorf("wire[exit_code] = %v, want 0", wire["exit_code"])
	}
	if wire["stdout"] != `{"result":"ok"}` {
		t.Errorf("wire[stdout] = %v, want {\"result\":\"ok\"}", wire["stdout"])
	}
	if wire["duration_ms"] != int64(500) {
		t.Errorf("wire[duration_ms] = %v, want 500", wire["duration_ms"])
	}
}

// ── CleanupWorkspaceCommand.Execute ───────────────────────────────────────────

func TestCleanupWorkspaceCommand_Execute(t *testing.T) {
	ops := &fakeWorkspaceOps{
		cleanupResult: command.CleanupResult{
			WorkspaceID: "ws-5",
			Destroyed:   true,
			Path:        "/tmp/ws-5",
		},
	}
	cmd := &command.CleanupWorkspaceCommand{
		Proto: protocol.CleanupWorkspaceCommand{
			CommandHeader: protocol.CommandHeader{
				CommandID:   "cmd-5",
				WorkspaceID: "ws-5",
				Kind:        protocol.KindCleanupWorkspace,
			},
		},
	}
	res, err := cmd.Execute(context.Background(), ops)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if !ops.cleanupCalled {
		t.Error("Cleanup not called")
	}
	wire := res.ToWire()
	assertWireKey(t, wire, "workspace_id", "ws-5")
	if wire["destroyed"] != true {
		t.Errorf("wire[destroyed] = %v, want true", wire["destroyed"])
	}
	assertWireKey(t, wire, "path", "/tmp/ws-5")
}

// ── ConfigUpdateCommand.Execute ───────────────────────────────────────────────

func TestConfigUpdateCommand_Execute(t *testing.T) {
	ops := &fakeAgentOps{}
	cfg := command.AgentConfig{
		MaxWorkspaces: 4,
		OTLPEndpoint:  "https://otel.example.com",
		OTLPDataset:   "prod",
	}
	cmd := &command.ConfigUpdateCommand{
		CommandHeader: protocol.CommandHeader{
			CommandID: "cmd-6",
			Kind:      protocol.KindConfigUpdate,
		},
		Config: cfg,
	}
	_, err := cmd.Execute(context.Background(), ops)
	if err != nil {
		t.Fatalf("Execute: %v", err)
	}
	if ops.appliedConfig == nil {
		t.Fatal("ApplyConfig was not called")
	}
	if ops.appliedConfig.MaxWorkspaces != 4 {
		t.Errorf("MaxWorkspaces = %d, want 4", ops.appliedConfig.MaxWorkspaces)
	}
}

// ── helpers ───────────────────────────────────────────────────────────────────

func assertWireKey(t *testing.T, wire map[string]any, key string, want any) {
	t.Helper()
	got, ok := wire[key]
	if !ok {
		t.Errorf("wire missing key %q", key)
		return
	}
	if got != want {
		t.Errorf("wire[%q] = %v (%T), want %v (%T)", key, got, got, want, want)
	}
}
