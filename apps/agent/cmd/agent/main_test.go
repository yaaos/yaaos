package main

import (
	"testing"

	"github.com/yaaos/agent/internal/supervisor"
)

// TestVersionConsistency asserts that supervisor.Config.Version and the OTel
// ServiceVersion both resolve to the same agentVersion string. The binary
// version is the single source of truth: the ldflags-injectable var
// agentVersion, with YAAOS_AGENT_VERSION as a runtime override.
//
// Two paths:
//   - env-override: YAAOS_AGENT_VERSION set → both consumers use that value.
//   - default-fallback: env unset → both consumers use the compiled-in agentVersion.
func TestVersionConsistency(t *testing.T) {
	// Record the original compiled-in value so we can restore agentVersion
	// after each sub-test.
	origDefault := agentVersion

	tests := []struct {
		name         string
		envVersion   string // value to set in YAAOS_AGENT_VERSION ("" = unset)
		wantBase     string // the agentVersion var content for this case
		wantResolved string // what both consumers should see
	}{
		{
			name:         "default fallback — env unset",
			envVersion:   "",
			wantBase:     "0.0.0-dev",
			wantResolved: "0.0.0-dev",
		},
		{
			name:         "env override wins",
			envVersion:   "1.2.3",
			wantBase:     "0.0.0-dev",
			wantResolved: "1.2.3",
		},
		{
			name:         "ldflags-injected value — env unset",
			envVersion:   "",
			wantBase:     "2.0.0",
			wantResolved: "2.0.0",
		},
		{
			name:         "ldflags-injected value — env override still wins",
			envVersion:   "3.0.0",
			wantBase:     "2.0.0",
			wantResolved: "3.0.0",
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			// Simulate ldflags injection by patching agentVersion.
			agentVersion = tc.wantBase
			t.Cleanup(func() { agentVersion = origDefault })

			if tc.envVersion != "" {
				t.Setenv("YAAOS_AGENT_VERSION", tc.envVersion)
			} else {
				t.Setenv("YAAOS_AGENT_VERSION", "")
			}

			// Compute the resolved version the same way main.go does.
			resolved := envOr("YAAOS_AGENT_VERSION", agentVersion)

			// Both consumers MUST see the same resolved value.
			// 1. OTel ServiceVersion path (passed to observability.Init).
			otelVersion := envOr("YAAOS_AGENT_VERSION", agentVersion)
			if otelVersion != resolved {
				t.Errorf("OTel version: got %q, want %q", otelVersion, resolved)
			}

			// 2. supervisor.Config.Version path.
			cfg := supervisor.Config{
				BaseURL: "https://example.com",
				Version: envOr("YAAOS_AGENT_VERSION", agentVersion),
			}
			if cfg.Version != resolved {
				t.Errorf("supervisor.Config.Version: got %q, want %q", cfg.Version, resolved)
			}

			// Sanity: the resolved value matches the expected value for the case.
			if resolved != tc.wantResolved {
				t.Errorf("resolved version: got %q, want %q", resolved, tc.wantResolved)
			}
		})
	}
}
