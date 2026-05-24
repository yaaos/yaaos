package observability

import (
	"sync"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/metric"
)

// Instruments is the agent's minimum-useful metric set. Names follow
// OTel semantic conventions (lowercase dot-namespaced); they're hard to
// rename without breaking customer dashboards, so the set is
// intentionally tight and grows additively.
//
// Backoff-related counters (`connection.failures`, `connection.backoff_seconds`)
// are declared here so commit D wires its retry loops to instruments
// that already exist.
type Instruments struct {
	CommandsClaimed          metric.Int64Counter
	CommandsCompleted        metric.Int64Counter // attributes: result=success|failure|timeout
	CommandDurationSeconds   metric.Float64Histogram
	WorkspacesActive         metric.Int64UpDownCounter
	ConnectionFailures       metric.Int64Counter // attributes: surface=sts|claim|heartbeat|ws, class=auth|network
	ConnectionBackoffSeconds metric.Float64Gauge // attributes: surface=...
}

var (
	metricsOnce sync.Once
	metricsRef  *Instruments
)

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

	metricsRef = inst
}
