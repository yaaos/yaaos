package protocol

import (
	"encoding/json"
	"testing"
)

func TestAgentCommandUnmarshalCreateWorkspace(t *testing.T) {
	body := `{
		"kind": "CreateWorkspace",
		"command_id": "11111111-1111-1111-1111-111111111111",
		"workspace_id": "22222222-2222-2222-2222-222222222222",
		"traceparent": "00-aabbcc-1122-01",
		"repo": {
			"plugin_id": "github",
			"external_id": "123",
			"clone_url": "https://github.com/me/repo.git",
			"head_sha": "deadbeef"
		},
		"history": 1,
		"auth": {"kind": "github_installation", "token": "redacted"},
		"ttl_seconds": 600,
		"max_idle_seconds": 600
	}`
	var cmd AgentCommand
	if err := json.Unmarshal([]byte(body), &cmd); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if cmd.Kind != KindCreateWorkspace {
		t.Fatalf("kind = %q", cmd.Kind)
	}
	if cmd.CreateWorkspace == nil {
		t.Fatal("CreateWorkspace pointer is nil")
	}
	if cmd.CreateWorkspace.Repo.HeadSHA != "deadbeef" {
		t.Fatalf("head_sha = %q", cmd.CreateWorkspace.Repo.HeadSHA)
	}
	if h := cmd.Header(); h.CommandID != "11111111-1111-1111-1111-111111111111" {
		t.Fatalf("header CommandID = %q", h.CommandID)
	}
}

func TestAgentCommandUnmarshalInvokeClaudeCode(t *testing.T) {
	body := `{
		"kind": "InvokeClaudeCode",
		"command_id": "11111111-1111-1111-1111-111111111111",
		"workspace_id": "22222222-2222-2222-2222-222222222222",
		"traceparent": "00-aabbcc-1122-01",
		"invocation": {"model": "opus", "effort": "high"},
		"limits": {"wallclock_seconds": 600}
	}`
	var cmd AgentCommand
	if err := json.Unmarshal([]byte(body), &cmd); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if cmd.Kind != KindInvokeClaudeCode {
		t.Fatalf("kind = %q", cmd.Kind)
	}
	if cmd.InvokeClaudeCode.Limits.WallclockSeconds != 600 {
		t.Fatalf("limits = %+v", cmd.InvokeClaudeCode.Limits)
	}
}

func TestAgentCommandUnmarshalCleanupWorkspace(t *testing.T) {
	body := `{
		"kind": "CleanupWorkspace",
		"command_id": "11111111-1111-1111-1111-111111111111",
		"workspace_id": "22222222-2222-2222-2222-222222222222",
		"traceparent": "00-aabbcc-1122-01"
	}`
	var cmd AgentCommand
	if err := json.Unmarshal([]byte(body), &cmd); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if cmd.Kind != KindCleanupWorkspace || cmd.CleanupWorkspace == nil {
		t.Fatalf("decode shape wrong: %+v", cmd)
	}
}

func TestAgentCommandUnmarshalRejectsUnknownKind(t *testing.T) {
	body := `{"kind": "MakeCoffee"}`
	var cmd AgentCommand
	if err := json.Unmarshal([]byte(body), &cmd); err == nil {
		t.Fatal("expected error on unknown kind")
	}
}

func TestAgentEventRoundTrip(t *testing.T) {
	src := AgentEvent{
		CommandID:    "abc",
		Kind:         EventCompletedSuccess,
		OutcomeLabel: "success",
		Outputs:      map[string]any{"workspace_id": "ws-1"},
		Traceparent:  "00-...",
	}
	buf, err := json.Marshal(src)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var got AgentEvent
	if err := json.Unmarshal(buf, &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Kind != EventCompletedSuccess || got.Outputs["workspace_id"] != "ws-1" {
		t.Fatalf("round-trip mismatch: %+v", got)
	}
}
