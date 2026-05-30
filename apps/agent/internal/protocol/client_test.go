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
		if r.URL.Path != "/api/v1/identity/exchange" {
			t.Fatalf("unexpected path: %s", r.URL.Path)
		}
		_ = json.NewEncoder(w).Encode(IdentityExchangeResponse{
			Bearer:    "test-bearer",
			ExpiresAt: time.Now().Add(time.Hour).UTC(),
			AgentID:   "agent-1",
		})
	}))
	defer server.Close()

	cli := NewClient(server.URL, nil)
	resp, err := cli.ExchangeIdentity(context.Background(), IdentityExchangeRequest{
		AgentPodID:    "pod-1",
		SignedRequest: "stub",
	})
	if err != nil {
		t.Fatalf("exchange: %v", err)
	}
	if resp.Bearer != "test-bearer" || resp.AgentID != "agent-1" {
		t.Fatalf("unexpected response: %+v", resp)
	}
}

func TestClaimCommand204ReturnsErrNoCommand(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer x" {
			t.Fatalf("bearer header = %q", got)
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	cli := NewClient(server.URL, nil)
	cli.SetBearer("x")
	_, err := cli.ClaimCommand(context.Background(), "agent-1", ClaimRequest{WaitSeconds: 0})
	if !errors.Is(err, ErrNoCommand) {
		t.Fatalf("expected ErrNoCommand, got %v", err)
	}
}

func TestClaimCommand200ReturnsRawBytes(t *testing.T) {
	const payload = `{"kind":"CleanupWorkspace","command_id":"cmd-1","workspace_id":"ws-1","traceparent":"00-..."}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(payload))
	}))
	defer server.Close()

	cli := NewClient(server.URL, nil)
	cli.SetBearer("x")
	raw, err := cli.ClaimCommand(context.Background(), "agent-1", ClaimRequest{WaitSeconds: 0})
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

func TestPostCommandEvent410IsStaleClaim(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusGone)
	}))
	defer server.Close()

	cli := NewClient(server.URL, nil)
	cli.SetBearer("x")
	err := cli.PostCommandEvent(context.Background(), "cmd-1", AgentEvent{
		CommandID:   "cmd-1",
		Kind:        EventCompletedSuccess,
		Traceparent: "00-...",
	})
	if !errors.Is(err, ErrStaleClaim) {
		t.Fatalf("expected ErrStaleClaim, got %v", err)
	}
}

func TestHeartbeatHappyPath(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(HeartbeatResponse{
			ReconciledAt:        time.Now().UTC(),
			ForgottenWorkspaces: []string{"ws-orphan"},
		})
	}))
	defer server.Close()

	cli := NewClient(server.URL, nil)
	cli.SetBearer("x")
	resp, err := cli.Heartbeat(context.Background(), "agent-1", HeartbeatRequest{
		ReportedAt: time.Now().UTC(),
	})
	if err != nil {
		t.Fatalf("heartbeat: %v", err)
	}
	if len(resp.ForgottenWorkspaces) != 1 || resp.ForgottenWorkspaces[0] != "ws-orphan" {
		t.Fatalf("unexpected response: %+v", resp)
	}
}
