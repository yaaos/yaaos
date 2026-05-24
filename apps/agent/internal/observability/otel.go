// Package observability wires the workspace agent's OpenTelemetry SDK
// for logs, traces, and metrics. The Collector is vendor-neutral —
// customers configure their exporter (Datadog, Honeycomb, New Relic,
// AWS CloudWatch, Splunk, …) inside the collector itself, downstream
// of the agent. The agent speaks OTLP/HTTP and nothing else.
//
// Init reads only standard OTEL_EXPORTER_OTLP_* env vars. When
// OTEL_EXPORTER_OTLP_ENDPOINT is unset, Init is a no-op — no SDK
// providers are initialized, no goroutines start, no overhead. The
// returned shutdown is safe to call even in that mode.
//
// On success, the returned Result.SlogHandler is the OTel slog bridge
// — the caller passes it into logging.Config.ExtraHandlers so every
// slog record fans out to the collector alongside stdout + the
// rotated file. Traces and metrics are wired into the global
// providers; instrumentation reaches them via otel.Tracer / otel.Meter
// or this package's Metrics() accessor.
package observability

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"time"

	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploghttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlpmetric/otlpmetrichttp"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.40.0"
)

// scopeName is the OTel instrumentation scope used by every signal
// originating from this package. Customers will see it in their backend
// as the source library for metrics, spans, and bridged log records.
const scopeName = "github.com/yaaos/agent"

// Config carries identity attributes that travel as OTel resource
// attributes on every signal. Read once at startup.
type Config struct {
	ServiceVersion string // e.g. "0.0.1" or build-stamped commit.
	AgentPodID     string // per-pod identifier; matches the value sent on heartbeat.
}

// Result is what Init returns.
type Result struct {
	// SlogHandler is the OTel slog bridge — nil when OTel is disabled.
	// Caller passes it to logging.Config.ExtraHandlers.
	SlogHandler slog.Handler

	// Shutdown flushes + closes every initialized provider. Safe to
	// call even when Init was a no-op.
	Shutdown func(context.Context) error
}

// Init wires the OTel providers. Returns a no-op Result (Shutdown is
// still safe to call) when OTEL_EXPORTER_OTLP_ENDPOINT is unset.
func Init(ctx context.Context, cfg Config) (*Result, error) {
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" {
		return &Result{Shutdown: func(context.Context) error { return nil }}, nil
	}

	res, err := resource.Merge(
		resource.Default(),
		resource.NewWithAttributes(
			semconv.SchemaURL,
			semconv.ServiceName("yaaos-workspace-agent"),
			semconv.ServiceVersion(cfg.ServiceVersion),
			// agent.pod_id is yaaos-specific; matches the value the
			// backend stores in workspace_agents.agent_pod_id so an
			// operator can correlate metrics to a heartbeat row.
			semconv.ServiceInstanceID(cfg.AgentPodID),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("resource merge: %w", err)
	}

	shutdowns := []func(context.Context) error{}

	// W3C TraceContext propagator — required for traceparent in/out
	// across the supervisor→workspace→Claude-Code chain. internal/tracing
	// only installs this from tests, so production OTel runs need it
	// here.
	otel.SetTextMapPropagator(propagation.TraceContext{})

	traceExp, err := otlptracehttp.New(ctx)
	if err != nil {
		return nil, fmt.Errorf("trace exporter: %w", err)
	}
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(traceExp),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(tp)
	shutdowns = append(shutdowns, tp.Shutdown)

	metricExp, err := otlpmetrichttp.New(ctx)
	if err != nil {
		return nil, fmt.Errorf("metric exporter: %w", err)
	}
	mp := sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(
			metricExp,
			sdkmetric.WithInterval(metricExportInterval()),
		)),
		sdkmetric.WithResource(res),
	)
	otel.SetMeterProvider(mp)
	shutdowns = append(shutdowns, mp.Shutdown)
	// Bind the global metric instruments now that the meter provider
	// exists. Anything calling Metrics() before this point gets no-ops.
	bindMetrics()

	logExp, err := otlploghttp.New(ctx)
	if err != nil {
		return nil, fmt.Errorf("log exporter: %w", err)
	}
	lp := sdklog.NewLoggerProvider(
		sdklog.WithProcessor(sdklog.NewBatchProcessor(logExp)),
		sdklog.WithResource(res),
	)
	shutdowns = append(shutdowns, lp.Shutdown)

	return &Result{
		SlogHandler: otelslog.NewHandler(scopeName, otelslog.WithLoggerProvider(lp)),
		Shutdown: func(ctx context.Context) error {
			var combined error
			// Reverse order: logs flush last so any shutdown-time logs
			// from the trace/metric providers still ship.
			for i := len(shutdowns) - 1; i >= 0; i-- {
				if err := shutdowns[i](ctx); err != nil {
					combined = errors.Join(combined, err)
				}
			}
			return combined
		},
	}, nil
}

// metricExportInterval honors OTEL_METRIC_EXPORT_INTERVAL (in ms) and
// falls back to 30s, matching the SDK default. The override exists for
// tests that don't want to wait 30 s to see the first metric POST.
func metricExportInterval() time.Duration {
	if v := os.Getenv("OTEL_METRIC_EXPORT_INTERVAL"); v != "" {
		if ms, err := strconv.Atoi(v); err == nil && ms > 0 {
			return time.Duration(ms) * time.Millisecond
		}
	}
	return 30 * time.Second
}
