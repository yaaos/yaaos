package supervisor

import (
	"log/slog"
	"os"
	"time"

	"github.com/yaaos/agent/internal/backoff"
)

// opsBackoffEnv names the env var tuning the operational surfaces (claim,
// heartbeat, WS dial).
const opsBackoffEnv = "YAAOS_AGENT_OPS_BACKOFF_SECONDS"

// opsBackoffSteps reads YAAOS_AGENT_OPS_BACKOFF_SECONDS once and returns the
// parsed step list. The bool is false when the env is unset or malformed, in
// which case the caller builds the prod ramp (1m/3m/5m/15m/60m). Parsing once —
// rather than per consumer — means a malformed value logs a single WARN at
// startup instead of one per operational surface.
//
// These surfaces use an indefinite schedule: a transient blip must not kill a
// running pod, so (unlike the STS surface) there is no deadline cap. Test stacks
// set YAAOS_AGENT_OPS_BACKOFF_SECONDS=2,2,2,2,2 so that after a DB wipe
// (resetStack) the agent re-tries heartbeat/claim within seconds.
func opsBackoffSteps() ([]time.Duration, bool) {
	v := os.Getenv(opsBackoffEnv)
	if v == "" {
		return nil, false
	}
	steps, err := parseBackoffSeconds(opsBackoffEnv, v)
	if err != nil {
		slog.Warn("supervisor.ops_backoff_parse_failed",
			"env", opsBackoffEnv,
			"value", v,
			"err", err.Error(),
			"fallback", "prod ramp (1m/3m/5m/15m/60m)",
		)
		return nil, false
	}
	return steps, true
}

// newOpsBackoff builds one operational backoff schedule from the result of
// opsBackoffSteps. Call once per surface (claim, heartbeat, WS) so each gets an
// independent schedule that backs off on its own cadence.
func newOpsBackoff(steps []time.Duration, custom bool) *backoff.Schedule {
	if !custom {
		return backoff.New()
	}
	return backoff.NewWithSteps(steps)
}
