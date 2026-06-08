package supervisor

import (
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/yaaos/agent/internal/backoff"
)

// parseOpsBackoffEnv reads YAAOS_AGENT_OPS_BACKOFF_SECONDS and returns a
// backoff.Schedule for operational surfaces (claim, heartbeat, WS dial).
//
// These surfaces use an indefinite schedule — a transient blip must not kill a
// running pod, so there is no deadline cap. The prod ramp is 1m/3m/5m/15m/60m.
//
//   - Unset → the prod ramp (1m/3m/5m/15m/60m), indefinite.
//   - Valid comma-separated positive integers → custom steps, indefinite.
//   - Malformed or any non-positive value → WARN + fall back to the prod ramp.
//
// Test stacks set YAAOS_AGENT_OPS_BACKOFF_SECONDS=2,2,2,2,2 so that after a
// DB wipe (resetStack) the agent re-tries heartbeat/claim within seconds
// instead of waiting 1 minute.
func parseOpsBackoffEnv() *backoff.Schedule {
	v := os.Getenv("YAAOS_AGENT_OPS_BACKOFF_SECONDS")
	if v == "" {
		return backoff.New()
	}
	parts := strings.Split(v, ",")
	steps := make([]time.Duration, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		n, err := strconv.Atoi(p)
		if err != nil || n <= 0 {
			slog.Warn("supervisor.ops_backoff_parse_failed",
				"env", "YAAOS_AGENT_OPS_BACKOFF_SECONDS",
				"value", v,
				"fallback", "prod ramp (1m/3m/5m/15m/60m)",
			)
			return backoff.New()
		}
		steps = append(steps, time.Duration(n)*time.Second)
	}
	if len(steps) == 0 {
		return backoff.New()
	}
	return backoff.NewWithSteps(steps)
}
