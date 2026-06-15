package protocol

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestExchangeIdentityHappyPath(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agent/identity" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		_ = json.NewEncoder(w).Encode(IdentityExchangeResponse{
			Bearer:       "test-bearer",
			ExpiresAt:    time.Now().Add(time.Hour).UTC(),
			RenewalAfter: time.Now().Add(55 * time.Minute).UTC(),
			AgentID:      "agent-1",
			InstanceID:   "task-abc-123",
		})
	}))
	defer server.Close()

	cli := NewClient(server.URL, nil)
	resp, err := cli.ExchangeIdentity(context.Background(), IdentityExchangeRequest{
		Kind:    "aws-sts",
		Payload: `{"url":"https://sts.amazonaws.com/","headers":{},"body":""}`,
	})
	if err != nil {
		t.Fatalf("exchange: %v", err)
	}
	if resp.Bearer != "test-bearer" || resp.AgentID != "agent-1" {
		t.Fatalf("unexpected response: %+v", resp)
	}
	if resp.InstanceID != "task-abc-123" {
		t.Fatalf("instance_id: want %q, got %q", "task-abc-123", resp.InstanceID)
	}
}

func TestClaimCommand204ReturnsErrNoCommand(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agent/commands/claim" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer x" {
			t.Fatalf("bearer header = %q", got)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	cli := NewClient(server.URL, nil)
	cli.SetBearer("x")
	_, err := cli.ClaimCommand(context.Background(), ClaimRequest{WaitSeconds: 0})
	if !errors.Is(err, ErrNoCommand) {
		t.Fatalf("expected ErrNoCommand, got %v", err)
	}
}

func TestClaimCommand200ReturnsRawBytes(t *testing.T) {
	const payload = `{"kind":"CleanupWorkspace","command_id":"cmd-1","workspace_id":"ws-1","traceparent":"00-..."}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agent/commands/claim" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(payload))
	}))
	defer server.Close()

	cli := NewClient(server.URL, nil)
	cli.SetBearer("x")
	raw, err := cli.ClaimCommand(context.Background(), ClaimRequest{WaitSeconds: 0})
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	// The caller (supervisor) passes raw to command.Decode; here we just
	// verify the JSON is returned verbatim and parseable.
	var probe struct {
		Kind      string `json:"kind"`
		CommandID string `json:"command_id"`
	}
	if err := json.Unmarshal(raw, &probe); err != nil {
		t.Fatalf("parse raw bytes: %v", err)
	}
	if probe.Kind != "CleanupWorkspace" {
		t.Errorf("kind: want CleanupWorkspace got %q", probe.Kind)
	}
	if probe.CommandID != "cmd-1" {
		t.Errorf("command_id: want cmd-1 got %q", probe.CommandID)
	}
}

func TestPostCommandEvent200ReturnsAck(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"command_event_outcome":"event_recorded"}`))
	}))
	defer server.Close()

	cli := NewClient(server.URL, nil)
	cli.SetBearer("x")
	ack, err := cli.PostCommandEvent(context.Background(), "cmd-1", AgentEvent{
		CommandID:   "cmd-1",
		Kind:        EventCompletedSuccess,
		Traceparent: "00-...",
	})
	if err != nil {
		t.Fatalf("PostCommandEvent: %v", err)
	}
	if ack.Outcome != CommandEventOutcomeRecorded {
		t.Errorf("outcome: want %q, got %q", CommandEventOutcomeRecorded, ack.Outcome)
	}
}

// TestPostCommandEventReturnsErrStaleClaimOn410 verifies that a 410 Gone
// response from the events endpoint returns ErrStaleClaim (not a generic error
// and not nil). The supervisor uses errors.Is(err, ErrStaleClaim) to drop the
// event without retry.
func TestPostCommandEventReturnsErrStaleClaimOn410(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusGone)
	}))
	defer server.Close()

	cli := NewClient(server.URL, nil)
	cli.SetBearer("x")
	_, err := cli.PostCommandEvent(context.Background(), "cmd-stale", AgentEvent{
		CommandID:  "cmd-stale",
		Kind:       EventCompletedSuccess,
		ReportedAt: time.Now().UTC(),
	})
	if err == nil {
		t.Fatal("PostCommandEvent on 410: want error, got nil")
	}
	if !errors.Is(err, ErrStaleClaim) {
		t.Errorf("PostCommandEvent on 410: want errors.Is(err, ErrStaleClaim), got %v", err)
	}
}

func TestHeartbeatHappyPath(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agent/heartbeat" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		_ = json.NewEncoder(w).Encode(HeartbeatResponse{
			ReconciledAt:        time.Now().UTC(),
			ForgottenWorkspaces: []string{"ws-orphan"},
		})
	}))
	defer server.Close()

	cli := NewClient(server.URL, nil)
	cli.SetBearer("x")
	resp, err := cli.Heartbeat(context.Background(), HeartbeatRequest{
		ReportedAt: time.Now().UTC(),
	})
	if err != nil {
		t.Fatalf("heartbeat: %v", err)
	}
	if len(resp.ForgottenWorkspaces) != 1 || resp.ForgottenWorkspaces[0] != "ws-orphan" {
		t.Fatalf("unexpected response: %+v", resp)
	}
}
