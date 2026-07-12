package command_test

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/command"
	"github.com/yaaos/agent/internal/protocol"
)

// TestDecodeRoundTrip verifies Decode accepts valid JSON for all 7 command
// kinds and returns the right concrete type with correct Header/Timeout values.
func TestDecodeRoundTrip(t *testing.T) {
	t.Run("ProvisionWorkspace", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":   "cmd-1",
			"workspace_id": "ws-1",
			"traceparent":  "tp-1",
			"kind":         "ProvisionWorkspace",
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
		assertHeader(t, hdr, "cmd-1", "ws-1", "tp-1", protocol.KindProvisionWorkspace)
		assertTimeout(t, cmd.Timeout(), 5*time.Minute)
		if _, ok := cmd.(*command.ProvisionWorkspaceCommand); !ok {
			t.Errorf("expected *command.ProvisionWorkspaceCommand, got %T", cmd)
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

	t.Run("PushBranch", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":       "cmd-push-1",
			"workspace_id":     "ws-push-1",
			"traceparent":      "tp-push-1",
			"kind":             "PushBranch",
			"completion_token": "ct-push-1",
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		hdr := cmd.Header()
		assertHeader(t, hdr, "cmd-push-1", "ws-push-1", "tp-push-1", protocol.KindPushBranch)
		if hdr.CompletionToken != "ct-push-1" {
			t.Errorf("header.CompletionToken = %q, want ct-push-1", hdr.CompletionToken)
		}
		assertTimeout(t, cmd.Timeout(), 120*time.Second)
		if _, ok := cmd.(*command.PushBranchCommand); !ok {
			t.Errorf("expected *command.PushBranchCommand, got %T", cmd)
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
				"environment":    "staging",
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
		if cu.Config.Environment != "staging" {
			t.Errorf("Config.Environment = %q, want staging", cu.Config.Environment)
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

	t.Run("Shutdown", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":       "cmd-sd-1",
			"traceparent":      "tp-sd-1",
			"kind":             "Shutdown",
			"completion_token": "ct-sd-1",
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		hdr := cmd.Header()
		if hdr.CommandID != "cmd-sd-1" {
			t.Errorf("header.CommandID = %q, want cmd-sd-1", hdr.CommandID)
		}
		if hdr.Kind != protocol.KindShutdown {
			t.Errorf("header.Kind = %q, want %q", hdr.Kind, protocol.KindShutdown)
		}
		if hdr.CompletionToken != "ct-sd-1" {
			t.Errorf("header.CompletionToken = %q, want ct-sd-1", hdr.CompletionToken)
		}
		assertTimeout(t, cmd.Timeout(), 30*time.Second)
		if _, ok := cmd.(*command.ShutdownCommand); !ok {
			t.Errorf("expected *command.ShutdownCommand, got %T", cmd)
		}
	})

	t.Run("CancelShutdown", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":       "cmd-cs-1",
			"traceparent":      "tp-cs-1",
			"kind":             "CancelShutdown",
			"completion_token": "ct-cs-1",
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		hdr := cmd.Header()
		if hdr.CommandID != "cmd-cs-1" {
			t.Errorf("header.CommandID = %q, want cmd-cs-1", hdr.CommandID)
		}
		if hdr.Kind != protocol.KindCancelShutdown {
			t.Errorf("header.Kind = %q, want %q", hdr.Kind, protocol.KindCancelShutdown)
		}
		if hdr.CompletionToken != "ct-cs-1" {
			t.Errorf("header.CompletionToken = %q, want ct-cs-1", hdr.CompletionToken)
		}
		assertTimeout(t, cmd.Timeout(), 30*time.Second)
		if _, ok := cmd.(*command.CancelShutdownCommand); !ok {
			t.Errorf("expected *command.CancelShutdownCommand, got %T", cmd)
		}
	})

	t.Run("InvokeCodex_with_wallclock", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":   "cmd-cx",
			"workspace_id": "ws-cx",
			"traceparent":  "tp-cx",
			"kind":         "InvokeCodex",
			"invocation":   json.RawMessage(`{"exec":{"argv":["codex","exec"],"stdin":"","env":{}}}`),
			"limits": map[string]any{
				"wallclock_seconds": 90,
			},
			"skill_path": ".codex/skills/myskill/SKILL.md",
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		hdr := cmd.Header()
		assertHeader(t, hdr, "cmd-cx", "ws-cx", "tp-cx", protocol.KindInvokeCodex)
		assertTimeout(t, cmd.Timeout(), 90*time.Second)
		if _, ok := cmd.(*command.InvokeCodexCommand); !ok {
			t.Errorf("expected *command.InvokeCodexCommand, got %T", cmd)
		}
	})

	t.Run("InvokeCodex_fallback_timeout", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":   "cmd-cxf",
			"workspace_id": "ws-cxf",
			"traceparent":  "tp-cxf",
			"kind":         "InvokeCodex",
			"invocation":   json.RawMessage(`{"exec":{"argv":["codex"],"stdin":"","env":{}}}`),
			"limits": map[string]any{
				"wallclock_seconds": 0,
			},
			"skill_path": ".codex/skills/s/SKILL.md",
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		// wallclock_seconds=0 → fallback 15m
		assertTimeout(t, cmd.Timeout(), 15*time.Minute)
	})

	t.Run("InvokeCodex_auth_json_wraps_and_zeroes_proto", func(t *testing.T) {
		// auth_json must be wrapped as a secret in command.InvokeCodexCommand
		// and zeroed in the proto field so the plaintext never lives on the
		// supervisor-side proto struct.
		raw := mustMarshal(t, map[string]any{
			"command_id":   "cmd-cxa",
			"workspace_id": "ws-cxa",
			"traceparent":  "tp-cxa",
			"kind":         "InvokeCodex",
			"invocation":   json.RawMessage(`{"exec":{"argv":["codex"],"stdin":"","env":{}}}`),
			"limits":       map[string]any{"wallclock_seconds": 60},
			"skill_path":   ".codex/skills/s/SKILL.md",
			"auth_json":    `{"token":"secret-tok"}`,
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		cx, ok := cmd.(*command.InvokeCodexCommand)
		if !ok {
			t.Fatalf("expected *command.InvokeCodexCommand, got %T", cmd)
		}
		// Proto field must be zeroed after wrapping.
		if cx.Proto.AuthJSON != "" {
			t.Errorf("Proto.AuthJSON should be zeroed after Decode, got %q", cx.Proto.AuthJSON)
		}
		// Secret wraps the actual value.
		if got := cx.AuthJSON.Value(); got != `{"token":"secret-tok"}` {
			t.Errorf("AuthJSON.Value() = %q, want {\"token\":\"secret-tok\"}", got)
		}
	})

	t.Run("InvokeCodex_MarshalWire_restores_auth_json", func(t *testing.T) {
		// MarshalWire must restore auth_json from the secret so the workspace
		// child process receives it after the IPC hop. Proto.AuthJSON is
		// re-zeroed after marshal.
		raw := mustMarshal(t, map[string]any{
			"command_id":   "cmd-cxm",
			"workspace_id": "ws-cxm",
			"traceparent":  "tp-cxm",
			"kind":         "InvokeCodex",
			"invocation":   json.RawMessage(`{"exec":{"argv":["codex"],"stdin":"","env":{}}}`),
			"limits":       map[string]any{"wallclock_seconds": 60},
			"skill_path":   ".codex/skills/s/SKILL.md",
			"auth_json":    `{"token":"wire-tok"}`,
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		cx := cmd.(*command.InvokeCodexCommand)

		wire, err := cx.MarshalWire()
		if err != nil {
			t.Fatalf("MarshalWire: %v", err)
		}
		// After MarshalWire, Proto.AuthJSON must be re-zeroed.
		if cx.Proto.AuthJSON != "" {
			t.Errorf("Proto.AuthJSON should be zeroed after MarshalWire, got %q", cx.Proto.AuthJSON)
		}
		// The serialized bytes must contain the auth_json value.
		if !strings.Contains(string(wire), `"auth_json"`) {
			t.Errorf("MarshalWire output missing auth_json field: %s", wire)
		}
		if !strings.Contains(string(wire), "wire-tok") {
			t.Errorf("MarshalWire output missing auth_json value: %s", wire)
		}
		// Decoding the wire bytes in the workspace child must produce the
		// correct InvokeCodexCommand with the secret populated.
		cmd2, err := command.Decode(wire)
		if err != nil {
			t.Fatalf("Decode wire: %v", err)
		}
		cx2, ok := cmd2.(*command.InvokeCodexCommand)
		if !ok {
			t.Fatalf("re-decoded type = %T, want *command.InvokeCodexCommand", cmd2)
		}
		if got := cx2.AuthJSON.Value(); got != `{"token":"wire-tok"}` {
			t.Errorf("re-decoded AuthJSON.Value() = %q, want wire-tok", got)
		}
	})

	t.Run("completion_token survives decode", func(t *testing.T) {
		raw := mustMarshal(t, map[string]any{
			"command_id":       "cmd-tok",
			"workspace_id":     "ws-tok",
			"traceparent":      "tp-tok",
			"completion_token": "tok-123",
			"kind":             "WriteFiles",
			"files": []map[string]any{
				{"path": "a.txt", "content": "hello"},
			},
		})
		cmd, err := command.Decode(raw)
		if err != nil {
			t.Fatalf("Decode: %v", err)
		}
		if got := cmd.Header().CompletionToken; got != "tok-123" {
			t.Errorf("completion_token: want tok-123 got %q", got)
		}
	})
}

// TestDecode_ConfigUpdate_Validation is a table-driven test for the full
// ConfigUpdate validation matrix: valid inputs pass, each invalid input
// returns a decode error with a relevant message.
func TestDecode_ConfigUpdate_Validation(t *testing.T) {
	cases := []struct {
		name        string
		maxWS       int
		otlp        string
		wantErr     bool
		errContains string
	}{
		{
			name:    "valid max_workspaces and otlp_endpoint",
			maxWS:   4,
			otlp:    "https://otlp.example.com",
			wantErr: false,
		},
		{
			name:    "valid empty otlp_endpoint (OTLP disabled)",
			maxWS:   4,
			otlp:    "",
			wantErr: false,
		},
		{
			name:        "invalid max_workspaces zero",
			maxWS:       0,
			otlp:        "",
			wantErr:     true,
			errContains: "max_workspaces",
		},
		{
			name:        "invalid otlp_endpoint not a url",
			maxWS:       4,
			otlp:        ":://broken",
			wantErr:     true,
			errContains: "otlp_endpoint",
		},
		{
			name:        "invalid otlp_endpoint missing host",
			maxWS:       4,
			otlp:        "http://",
			wantErr:     true,
			errContains: "otlp_endpoint",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			raw := mustMarshal(t, map[string]any{
				"command_id":  "cmd-v",
				"traceparent": "tp-v",
				"kind":        "ConfigUpdate",
				"config": map[string]any{
					"max_workspaces": tc.maxWS,
					"otlp_endpoint":  tc.otlp,
				},
			})
			_, err := command.Decode(raw)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("Decode: expected error, got nil")
				}
				if tc.errContains != "" && !strings.Contains(err.Error(), tc.errContains) {
					t.Errorf("error %q does not contain %q", err.Error(), tc.errContains)
				}
				return
			}
			if err != nil {
				t.Fatalf("Decode: unexpected error: %v", err)
			}
		})
	}
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
		&command.ProvisionWorkspaceCommand{},
		&command.WriteFilesCommand{},
		&command.RefreshWorkspaceAuthCommand{},
		&command.InvokeClaudeCodeCommand{},
		&command.InvokeCodexCommand{},
		&command.CleanupWorkspaceCommand{},
		&command.PushBranchCommand{},
		&command.ConfigUpdateCommand{},
		&command.ShutdownCommand{},
		&command.CancelShutdownCommand{},
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
