// Tests for synchronous dispatch of AgentCommands (e.g. ConfigUpdate).
//
// AgentCommands run inline on the claim worker rather than in a spawned
// goroutine, so the side effect (ApplyConfig storing the config pointer) is
// visible to the next claim cycle. Without this, the boot sequence over-claims
// pinned ConfigUpdates under "unconfigured" lifecycle because the claim loop
// re-arms before the dispatch goroutine has stored config.
package supervisor

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/yaaos/agent/internal/protocol"
)

// mustMarshalConfigUpdate builds a minimal JSON payload for a ConfigUpdateCommand
// that satisfies command.Decode's validation (MaxWorkspaces >= 1, no OTLP).
func mustMarshalConfigUpdate(commandID string, maxWorkspaces int) []byte {
	payload := map[string]any{
		"kind":             "ConfigUpdate",
		"command_id":       commandID,
		"workspace_id":     "",
		"traceparent":      "tp-" + commandID,
		"completion_token": "",
		"run_id":           "",
		"config": map[string]any{
			"max_workspaces": maxWorkspaces,
			"otlp_endpoint":  "",
			"otlp_token":     "",
			"otlp_dataset":   "",
			"environment":    "test",
			"api_keys":       map[string]string{},
		},
	}
	b, err := json.Marshal(payload)
	if err != nil {
		panic(err)
	}
	return b
}

// TestClaimLoop_AgentCommandRunsInline asserts that when the first claim
// returns an AgentCommand (ConfigUpdate), the second claim is sent with
// `lifecycle="active"` — proving ApplyConfig completed BEFORE the claim
// loop re-armed. Without the inline-dispatch fix, the goroutine model races:
// the claim loop re-arms while s.config.Load() is still nil, sending a second
// "unconfigured" request and over-claiming a pinned ConfigUpdate.
func TestClaimLoop_AgentCommandRunsInline(t *testing.T) {
	// Capture the lifecycle of every claim request the worker sends.
	var lifecyclesMu sync.Mutex
	var lifecycles []string

	cmds := [][]byte{
		mustMarshalConfigUpdate("cmd-cfg-1", 5),
	}
	var commandsServed int32
	var eventCallCount int32

	combined := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.URL.Path == "/api/v1/agent/commands/claim":
			body, _ := io.ReadAll(r.Body)
			var req protocol.ClaimRequest
			if err := json.Unmarshal(body, &req); err != nil {
				t.Errorf("claim request decode: %v", err)
			}
			lifecyclesMu.Lock()
			lifecycles = append(lifecycles, req.Lifecycle)
			lifecyclesMu.Unlock()

			idx := int(atomic.AddInt32(&commandsServed, 1)) - 1
			if idx >= len(cmds) {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write(cmds[idx])

		case strings.Contains(r.URL.Path, "/api/v1/commands/") &&
			strings.HasSuffix(r.URL.Path, "/events"):
			atomic.AddInt32(&eventCallCount, 1)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"command_event_outcome":"event_recorded"}`))

		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(combined.Close)

	// Start from the package's standard unconfigured-supervisor helper, then
	// override the few fields this test needs: the httptest BaseURL/client, a
	// 1s claim wait so the loop re-arms fast, the test-shortened event-post
	// retry, and the dedup cache (the helper leaves it nil). Start unconfigured:
	// we do NOT call ApplyConfig — the first claim must go out with
	// Lifecycle="unconfigured" and the worker's own ApplyConfig (triggered by
	// the ConfigUpdate it claims) must flip the state before the next claim.
	s := buildUnconfiguredSupervisor(t)
	s.cfg.BaseURL = combined.URL
	s.cfg.ClaimWaitSeconds = 1
	s.client = protocol.NewClient(combined.URL, nil)
	s.eventPostSteps = []time.Duration{time.Millisecond}
	s.dedup = newDedupCache(dedupCacheSize)
	s.agentID = "agent-inline-test"
	s.orgID = "org-inline-test"
	defer s.pool.CloseAll(context.Background())

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	done := make(chan struct{})
	go func() {
		defer close(done)
		s.claimLoop(ctx, 0)
	}()

	// Wait until the server has served the ConfigUpdate (commandsServed=1) AND
	// the terminal event has been POSTed AND a second claim has come in.
	deadline := time.Now().Add(4 * time.Second)
	for time.Now().Before(deadline) {
		lifecyclesMu.Lock()
		n := len(lifecycles)
		lifecyclesMu.Unlock()
		if atomic.LoadInt32(&eventCallCount) >= 1 && n >= 2 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	<-done

	lifecyclesMu.Lock()
	defer lifecyclesMu.Unlock()
	if len(lifecycles) < 2 {
		t.Fatalf("want at least 2 claim requests (first + post-apply), got %d (%v)", len(lifecycles), lifecycles)
	}
	if lifecycles[0] != "unconfigured" {
		t.Errorf("first claim lifecycle: want 'unconfigured', got %q", lifecycles[0])
	}
	// The critical assertion: the SECOND claim must already see lifecycle=active.
	// If the dispatch ran in a goroutine, the loop would re-arm before ApplyConfig
	// stored config and we'd see "unconfigured" again here.
	if lifecycles[1] != "active" {
		t.Errorf("second claim lifecycle (post-AgentCommand-dispatch): want 'active', got %q — proves AgentCommand was NOT run inline before re-arm", lifecycles[1])
	}
}
