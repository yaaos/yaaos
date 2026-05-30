package observability

import (
	"sync"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/metric"
)

// Instruments is the agent's minimum-useful metric set. Names follow
// OTel semantic conventions (lowercase dot-namespaced); they're hard to
// rename without breaking customer dashboards, so the set is
// intentionally tight and grows additively.
//
// Backoff-related counters (`connection.failures`, `connection.backoff_seconds`)
// are declared here so retry loops can record against instruments
// that always exist.
type Instruments struct {
	CommandsClaimed          metric.Int64Counter
	CommandsCompleted        metric.Int64Counter // attributes: result=success|failure|timeout
	CommandDurationSeconds   metric.Float64Histogram
	WorkspacesActive         metric.Int64UpDownCounter
	ConnectionFailures       metric.Int64Counter // attributes: surface=sts|claim|heartbeat|ws, class=auth|network
	ConnectionBackoffSeconds metric.Float64Gauge // attributes: surface=...
	CommandsDeduped          metric.Int64Counter // attributes: org_id, agent_id — duplicate command_id hit the cache
	EventsPostRetries        metric.Int64Counter // attributes: kind, org_id, agent_id — each retry of a terminal-event POST
}

var (
	metricsOnce sync.Once
	metricsRef  *Instruments
)

// stdDimsMu guards stdOrgID and stdAgentID.
var stdDimsMu sync.RWMutex
var (
	stdOrgID   string
	stdAgentID string
)

// SetStandardDimensions stores the org_id and agent_id that will appear on
// every metric record via StandardAttrs. Called once after identity exchange;
// safe to call from any goroutine.
func SetStandardDimensions(orgID, agentID string) {
	stdDimsMu.Lock()
	defer stdDimsMu.Unlock()
	stdOrgID = orgID
	stdAgentID = agentID
}

// StandardAttrs returns a metric.MeasurementOption that attaches the
// process-wide org_id and agent_id to a metric record. If
// SetStandardDimensions has not been called, the attributes are empty
// strings (no-op for uninitialized pods).
func StandardAttrs() metric.MeasurementOption {
	stdDimsMu.RLock()
	org := stdOrgID
	agent := stdAgentID
	stdDimsMu.RUnlock()
	return metric.WithAttributes(
		attribute.String("org_id", org),
		attribute.String("agent_id", agent),
	)
}

// Metrics returns the global agent metric instruments. Before Init runs
// — or when OTel is disabled — every instrument resolves through the
// no-op MeterProvider OTel installs by default, so callers can record
// freely without nil-checking.
func Metrics() *Instruments {
	metricsOnce.Do(bindMetrics)
	return metricsRef
}

// bindMetrics (re-)resolves every instrument against the current global
// MeterProvider. Called from Init after the real provider is installed
// — re-binding after Init swaps the no-op instruments out for real
// ones without changing the Metrics() call sites.
func bindMetrics() {
	m := otel.Meter(scopeName)
	inst := &Instruments{}
	var err error

	if inst.CommandsClaimed, err = m.Int64Counter(
		"yaaos.agent.commands.claimed",
		metric.WithDescription("AgentCommands claimed off the long-poll."),
	); err != nil {
		panic(err)
	}
	if inst.CommandsCompleted, err = m.Int64Counter(
		"yaaos.agent.commands.completed",
		metric.WithDescription("AgentCommands that reached a terminal event."),
	); err != nil {
		panic(err)
	}
	if inst.CommandDurationSeconds, err = m.Float64Histogram(
		"yaaos.agent.command.duration",
		metric.WithDescription("End-to-end duration of one AgentCommand."),
		metric.WithUnit("s"),
	); err != nil {
		panic(err)
	}
	if inst.WorkspacesActive, err = m.Int64UpDownCounter(
		"yaaos.agent.workspaces.active",
		metric.WithDescription("Workspaces currently held by this pod."),
	); err != nil {
		panic(err)
	}
	if inst.ConnectionFailures, err = m.Int64Counter(
		"yaaos.agent.connection.failures",
		metric.WithDescription("Failed control-plane interactions, before any retry."),
	); err != nil {
		panic(err)
	}
	if inst.ConnectionBackoffSeconds, err = m.Float64Gauge(
		"yaaos.agent.connection.backoff_seconds",
		metric.WithDescription("Current backoff sleep per control-plane surface."),
		metric.WithUnit("s"),
	); err != nil {
		panic(err)
	}
	if inst.CommandsDeduped, err = m.Int64Counter(
		"yaaos.agent.commands.deduped",
		metric.WithDescription("Terminal-event cache hits: duplicate command_id re-delivered without re-execution."),
	); err != nil {
		panic(err)
	}
	if inst.EventsPostRetries, err = m.Int64Counter(
		"yaaos.agent.events.post.retries",
		metric.WithDescription("Each retry of a terminal-event POST to the control plane, by command kind."),
	); err != nil {
		panic(err)
	}

	metricsRef = inst
}
