package command_test

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/protocol"
)

// TestDecodeRoundTrip verifies Decode accepts valid JSON for all 6 command
// kinds and returns the right concrete type with correct Header/Timeout values.
func TestDecodeRoundTrip(t *testing.T) {
	t.Run("CreateWorkspace", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":   "cmd-1",
			"workspace_id": "ws-1",
			"traceparent":  "tp-1",
			"kind":         "CreateWorkspace",
			"repo": map[string]any{
				"plugin_id":   "gh",
				"external_id": "org/repo",
				"clone_url":   "https://github.com/org/repo",
				"head_sha":    "abc123",
			},
			"history": 10,
			"auth": map[string]any{
				"kind":  "github_installation",
				"token": "tok",
			},
			"ttl_seconds":      3600,
			"max_idle_seconds": 600,
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		hdr := cmd.Header()
		assertHeader(t, hdr, "cmd-1", "ws-1", "tp-1", protocol.KindCreateWorkspace)
		assertTimeout(t, cmd.Timeout(), 5*time.Minute)
		if _, ok := cmd.(*command.CreateWorkspaceCommand); !ok {
			t.Errorf("expected *command.CreateWorkspaceCommand, got %T", cmd)
		}
	})

	t.Run("WriteFiles", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":   "cmd-2",
			"workspace_id": "ws-2",
			"traceparent":  "tp-2",
			"kind":         "WriteFiles",
			"files": []map[string]any{
				{"path": "a.txt", "content": "hello"},
			},
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		hdr := cmd.Header()
		assertHeader(t, hdr, "cmd-2", "ws-2", "tp-2", protocol.KindWriteFiles)
		assertTimeout(t, cmd.Timeout(), 30*time.Second)
		if _, ok := cmd.(*command.WriteFilesCommand); !ok {
			t.Errorf("expected *command.WriteFilesCommand, got %T", cmd)
		}
	})

	t.Run("RefreshWorkspaceAuth", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":   "cmd-3",
			"workspace_id": "ws-3",
			"traceparent":  "tp-3",
			"kind":         "RefreshWorkspaceAuth",
			"new_token":    "new-tok",
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		hdr := cmd.Header()
		assertHeader(t, hdr, "cmd-3", "ws-3", "tp-3", protocol.KindRefreshWorkspaceAuth)
		assertTimeout(t, cmd.Timeout(), 30*time.Second)
		if _, ok := cmd.(*command.RefreshWorkspaceAuthCommand); !ok {
			t.Errorf("expected *command.RefreshWorkspaceAuthCommand, got %T", cmd)
		}
	})

	t.Run("InvokeClaudeCode_with_wallclock", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":   "cmd-4",
			"workspace_id": "ws-4",
			"traceparent":  "tp-4",
			"kind":         "InvokeClaudeCode",
			"invocation":   json.RawMessage(`{"exec":{"argv":["claude"],"stdin":"","env":{}}}`),
			"limits": map[string]any{
				"wallclock_seconds": 120,
			},
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		hdr := cmd.Header()
		assertHeader(t, hdr, "cmd-4", "ws-4", "tp-4", protocol.KindInvokeClaudeCode)
		// wallclock_seconds=120 means Timeout() returns 120s
		assertTimeout(t, cmd.Timeout(), 120*time.Second)
		if _, ok := cmd.(*command.InvokeClaudeCodeCommand); !ok {
			t.Errorf("expected *command.InvokeClaudeCodeCommand, got %T", cmd)
		}
	})

	t.Run("InvokeClaudeCode_fallback_timeout", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":   "cmd-4b",
			"workspace_id": "ws-4b",
			"traceparent":  "tp-4b",
			"kind":         "InvokeClaudeCode",
			"invocation":   json.RawMessage(`{"exec":{"argv":["claude"],"stdin":"","env":{}}}`),
			"limits": map[string]any{
				"wallclock_seconds": 0,
			},
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		// wallclock_seconds=0 → fallback 15m
		assertTimeout(t, cmd.Timeout(), 15*time.Minute)
	})

	t.Run("CleanupWorkspace", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":   "cmd-5",
			"workspace_id": "ws-5",
			"traceparent":  "tp-5",
			"kind":         "CleanupWorkspace",
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		hdr := cmd.Header()
		assertHeader(t, hdr, "cmd-5", "ws-5", "tp-5", protocol.KindCleanupWorkspace)
		assertTimeout(t, cmd.Timeout(), 30*time.Second)
		if _, ok := cmd.(*command.CleanupWorkspaceCommand); !ok {
			t.Errorf("expected *command.CleanupWorkspaceCommand, got %T", cmd)
		}
	})

	t.Run("ConfigUpdate", func(t *testing.T) {
		// Nested `config` object — the exact shape the control plane emits
		// (model_dump of ConfigUpdateCommand{config: AgentConfig{...}}). The
		// decoder must read the cap and OTLP fields out of the nested object,
		// not from flat top-level keys.
		raw := mustMarshal(t, map[string]any{
			"command_id":  "cmd-6",
			"traceparent": "tp-6",
			"kind":        "ConfigUpdate",
			"config": map[string]any{
				"max_workspaces": 5,
				"otlp_endpoint":  "https://otel.example.com",
				"otlp_token":     "secret-tok",
				"otlp_dataset":   "yaaos-prod",
			},
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		hdr := cmd.Header()
		if hdr.CommandID != "cmd-6" {
			t.Errorf("header.CommandID = %q, want %q", hdr.CommandID, "cmd-6")
		}
		if hdr.Kind != protocol.KindConfigUpdate {
			t.Errorf("header.Kind = %q, want %q", hdr.Kind, protocol.KindConfigUpdate)
		}
		cu, ok := cmd.(*command.ConfigUpdateCommand)
		if !ok {
			t.Fatalf("expected *command.ConfigUpdateCommand, got %T", cmd)
		}
		if cu.Config.MaxWorkspaces != 5 {
			t.Errorf("Config.MaxWorkspaces = %d, want 5", cu.Config.MaxWorkspaces)
		}
		if cu.Config.OTLPEndpoint != "https://otel.example.com" {
			t.Errorf("Config.OTLPEndpoint = %q, want https://otel.example.com", cu.Config.OTLPEndpoint)
		}
		if cu.Config.OTLPToken.Value() != "secret-tok" {
			t.Errorf("Config.OTLPToken.Value() = %q, want secret-tok", cu.Config.OTLPToken.Value())
		}
		if cu.Config.OTLPDataset != "yaaos-prod" {
			t.Errorf("Config.OTLPDataset = %q, want yaaos-prod", cu.Config.OTLPDataset)
		}
	})

	t.Run("ConfigUpdate rejects max_workspaces below 1", func(t *testing.T) {
		// Fail-closed: the spec requires max_workspaces >= 1. A zero/missing
		// cap must be rejected at Decode so a malformed (or future-drifted)
		// ConfigUpdate can never silently default the pool open to unlimited.
		raw := mustMarshal(t, map[string]any{
			"command_id":  "cmd-7",
			"traceparent": "tp-7",
			"kind":        "ConfigUpdate",
			"config": map[string]any{
				"max_workspaces": 0,
				"otlp_endpoint":  "",
			},
		})
		if _, err := command.Decode(raw); err == nil {
			t.Fatal("Decode: expected error for max_workspaces=0, got nil")
		}
	})
}

// TestDecodeUnknownKind verifies Decode returns an error for an unrecognised kind.
func TestDecodeUnknownKind(t *testing.T) {
	raw := mustMarshal(t, map[string]any{
		"command_id":   "cmd-x",
		"workspace_id": "ws-x",
		"kind":         "FrobnikateSomething",
	})
	_, err := command.Decode(raw)
	if err == nil {
		t.Fatal("expected error for unknown kind, got nil")
	}
}

// TestDecodeMalformedJSON verifies Decode returns an error on bad JSON.
func TestDecodeMalformedJSON(t *testing.T) {
	_, err := command.Decode([]byte(`{not valid json`))
	if err == nil {
		t.Fatal("expected error for malformed JSON, got nil")
	}
}

// TestSetTraceparent_AllKinds verifies that SetTraceparent rewrites the
// embedded CommandHeader.Traceparent for every concrete Command — the
// compiler-enforced replacement for the old supervisor type-switch. A kind
// that forgot the method would not satisfy command.Command and fail to
// appear in this list.
func TestSetTraceparent_AllKinds(t *testing.T) {
	const newTP = "00-aabbccddeeff00112233445566778899-1122334455667788-01"
	cmds := []command.Command{
		&command.CreateWorkspaceCommand{},
		&command.WriteFilesCommand{},
		&command.RefreshWorkspaceAuthCommand{},
		&command.InvokeClaudeCodeCommand{},
		&command.CleanupWorkspaceCommand{},
		&command.ConfigUpdateCommand{},
	}
	for _, c := range cmds {
		c.SetTraceparent(newTP)
		if got := c.Header().Traceparent; got != newTP {
			t.Errorf("%T: Traceparent after SetTraceparent = %q, want %q", c, got, newTP)
		}
	}
}

// ── helpers ──────────────────────────────────────────────────────────────────

func assertHeader(t *testing.T, hdr protocol.CommandHeader, wantCmdID, wantWsID, wantTP string, wantKind protocol.CommandKind) {
	t.Helper()
	if hdr.CommandID != wantCmdID {
		t.Errorf("CommandID = %q, want %q", hdr.CommandID, wantCmdID)
	}
	if hdr.WorkspaceID != wantWsID {
		t.Errorf("WorkspaceID = %q, want %q", hdr.WorkspaceID, wantWsID)
	}
	if hdr.Traceparent != wantTP {
		t.Errorf("Traceparent = %q, want %q", hdr.Traceparent, wantTP)
	}
	if hdr.Kind != wantKind {
		t.Errorf("Kind = %q, want %q", hdr.Kind, wantKind)
	}
}

func assertTimeout(t *testing.T, got, want time.Duration) {
	t.Helper()
	if got != want {
		t.Errorf("Timeout() = %v, want %v", got, want)
	}
}

func mustMarshal(t *testing.T, v any) []byte {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("mustMarshal: %v", err)
	}
	return b
}
