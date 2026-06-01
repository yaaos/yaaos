package supervisor

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/identity"
	"github.com/yaaos/agent/internal/protocol"
)

// stubProvider is a test-only identity.Provider that returns a pre-canned
// signed claim payload. Implements the new Provider interface (Kind + SignClaim).
type stubProvider struct {
	SignErr error
	// payload is returned as the SignClaim result. Defaults to a minimal JSON envelope.
	payload string
}

func (s *stubProvider) Kind() string { return "aws-sts" }

func (s *stubProvider) SignClaim(_ context.Context, _ string) (json.RawMessage, error) {
	if s.SignErr != nil {
		return nil, s.SignErr
	}
	p := s.payload
	if p == "" {
		p = `{"url":"https://sts.amazonaws.com/","headers":{"authorization":"AWS4-HMAC-SHA256 Credential=test"},"body":"Action=GetCallerIdentity&Version=2011-06-15"}`
	}
	return json.RawMessage(p), nil
}

// fakeExchangeServer returns an httptest.Server that handles
// POST /api/v1/agent/identity with the given AgentID, OrgID, and Bearer.
// The caller is responsible for closing the server.
func fakeExchangeServer(t *testing.T, agentID, orgID, bearer string) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/agent/identity" {
			http.NotFound(w, r)
			return
		}
		resp := map[string]any{
			"bearer":        bearer,
			"expires_at":    time.Now().Add(time.Hour).Format(time.RFC3339),
			"renewal_after": time.Now().Add(55 * time.Minute).Format(time.RFC3339),
			"agent_id":      agentID,
			"instance_id":   "task-test-001",
			"org_id":        orgID,
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}))
}

// TestBearerRefresh_MatchingCredentials_RotatesBearer verifies that a renewal
// whose AgentID+OrgID match the pinned values rotates the bearer and expiry
// without changing the configured state.
func TestBearerRefresh_MatchingCredentials_RotatesBearer(t *testing.T) {
	srv := fakeExchangeServer(t, "agent-abc", "org-xyz", "bearer-v2")
	defer srv.Close()

	prov := &stubProvider{}

	cfg := Config{
		BaseURL:    srv.URL,
		AgentPodID: "pod-1",
		Version:    "0.0.1",
	}
	s := buildSupervisorForIdentityTest(cfg, prov, "agent-abc", "org-xyz")

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	result := s.runOneRefreshCycle(ctx, time.Now())
	if result.fatal {
		t.Errorf("matching renewal should not be fatal, got fatal=true")
	}
	if result.newBearer != "bearer-v2" {
		t.Errorf("rotated bearer: want bearer-v2, got %q", result.newBearer)
	}
}

// TestBearerRefresh_MismatchedAgentID_IsFatal verifies that a renewal whose
// AgentID differs from the pinned value is treated as an identity-integrity
// violation and returns a fatal signal.
func TestBearerRefresh_MismatchedAgentID_IsFatal(t *testing.T) {
	srv := fakeExchangeServer(t, "agent-DIFFERENT", "org-xyz", "bearer-imposter")
	defer srv.Close()

	prov := &stubProvider{}

	cfg := Config{
		BaseURL:    srv.URL,
		AgentPodID: "pod-1",
		Version:    "0.0.1",
	}
	s := buildSupervisorForIdentityTest(cfg, prov, "agent-abc", "org-xyz")

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	result := s.runOneRefreshCycle(ctx, time.Now())
	if !result.fatal {
		t.Errorf("mismatched AgentID renewal should be fatal")
	}
}

// TestBearerRefresh_MismatchedOrgID_IsFatal verifies that a renewal whose
// OrgID differs from the pinned value is also fatal.
func TestBearerRefresh_MismatchedOrgID_IsFatal(t *testing.T) {
	srv := fakeExchangeServer(t, "agent-abc", "org-DIFFERENT", "bearer-wrong-org")
	defer srv.Close()

	prov := &stubProvider{}

	cfg := Config{
		BaseURL:    srv.URL,
		AgentPodID: "pod-1",
		Version:    "0.0.1",
	}
	s := buildSupervisorForIdentityTest(cfg, prov, "agent-abc", "org-xyz")

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	result := s.runOneRefreshCycle(ctx, time.Now())
	if !result.fatal {
		t.Errorf("mismatched OrgID renewal should be fatal")
	}
}

// buildSupervisorForIdentityTest constructs a Supervisor wired with the
// given provider and pre-populated agentID+orgID (as if identity was already
// exchanged at startup). Uses a real protocol.Client pointing at the given
// cfg.BaseURL so runOneRefreshCycle can call exchangeIdentity over HTTP.
func buildSupervisorForIdentityTest(cfg Config, prov identity.Provider, agentID, orgID string) *Supervisor {
	if cfg.Concurrency <= 0 {
		cfg.Concurrency = 1
	}
	if cfg.HeartbeatInterval <= 0 {
		cfg.HeartbeatInterval = 30 * time.Second
	}
	if cfg.ClaimWaitSeconds <= 0 {
		cfg.ClaimWaitSeconds = 30
	}
	if cfg.ActivityBatchInterval <= 0 {
		cfg.ActivityBatchInterval = 250 * time.Millisecond
	}
	if cfg.Spawn == nil {
		cfg.Spawn = dummySpawn
	}
	s := &Supervisor{
		cfg:      cfg,
		client:   protocol.NewClient(cfg.BaseURL, nil),
		log:      nullLogger{},
		agentID:  agentID,
		orgID:    orgID,
		provider: prov,
	}
	return s
}

// dummySpawn satisfies SpawnFunc without starting a process.
func dummySpawn(_ context.Context, _ string) (WorkspaceRunner, error) {
	return nil, nil
}

// TestHostFromURL covers host extraction across URL shapes, including the
// edge cases the hand-rolled parser used to get wrong.
func TestHostFromURL(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{"https_with_path", "https://api.yaaos.dev/api/v1", "api.yaaos.dev"},
		{"https_with_port", "https://api.yaaos.dev:8443/api", "api.yaaos.dev:8443"},
		{"ipv6_literal", "http://[::1]:8080/api", "[::1]:8080"},
		{"embedded_credentials", "http://user@host/", "host"},
		{"no_trailing_slash", "http://host", "host"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := hostFromURL(tc.in); got != tc.want {
				t.Errorf("hostFromURL(%q): want %q, got %q", tc.in, tc.want, got)
			}
		})
	}
}
