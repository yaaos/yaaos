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
// Result.SlogHandler is always the live log bridge — the caller passes it
// into logging.Config.ExtraHandlers once at startup. The bridge drops records
// until a logger provider is installed (by this Init when an env endpoint is
// set, or by a later ConfigUpdate via BindExporter), then fans every slog
// record out to the collector alongside stdout + the rotated file. Traces and
// metrics are wired into the global providers; instrumentation reaches them
// via otel.Tracer / otel.Meter or this package's Metrics() accessor.
package observability

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"strconv"
	"strings"
	"sync"
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

// installState tracks whether the SDK providers have been wired into the
// global otel registries, so the env-var Init path and the late-binding
// ConfigUpdate path (BindExporter) never double-install. startupCfg captures
// the identity attributes Init received so BindExporter can build the same
// resource when it late-binds.
var (
	installMu  sync.Mutex
	installed  bool
	startupCfg Config
	// installShutdown flushes + closes whichever providers got wired —
	// whether by env-var Init or by a late BindExporter. Init's returned
	// Shutdown reads it, so the process flushes late-bound telemetry on exit
	// even though BindExporter ran after Init returned.
	installShutdown func(context.Context) error
)

// Result is what Init returns.
type Result struct {
	// SlogHandler is the live log bridge — always non-nil. The caller passes
	// it to logging.Config.ExtraHandlers once at startup; it stays dormant
	// until a logger provider is installed (env Init or BindExporter).
	SlogHandler slog.Handler

	// Shutdown flushes + closes every initialized provider. Safe to
	// call even when Init was a no-op.
	Shutdown func(context.Context) error
}

// Init wires the OTel providers. Returns a no-op Result (Shutdown is
// still safe to call) when OTEL_EXPORTER_OTLP_ENDPOINT is unset — in that
// mode the OTLP endpoint may still arrive later via ConfigUpdate, which
// late-binds the exporter through BindExporter.
func Init(ctx context.Context, cfg Config) (*Result, error) {
	installMu.Lock()
	startupCfg = cfg
	installMu.Unlock()

	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" {
		// No env endpoint: providers stay no-op, but still hand back the live
		// log bridge so the caller wires it into the logging fan-out. A later
		// ConfigUpdate (BindExporter) sets its delegate and logs start flowing.
		// Shutdown reads installShutdown so a late-bound pipeline still flushes
		// on exit.
		return &Result{SlogHandler: liveLogBridge, Shutdown: shutdownInstalled}, nil
	}

	res, err := buildResource(cfg)
	if err != nil {
		return nil, err
	}

	// The exporters read OTEL_EXPORTER_OTLP_* env vars (endpoint, headers,
	// protocol) when constructed with no explicit endpoint option.
	traceExp, err := otlptracehttp.New(ctx)
	if err != nil {
		return nil, fmt.Errorf("trace exporter: %w", err)
	}
	metricExp, err := otlpmetrichttp.New(ctx)
	if err != nil {
		return nil, fmt.Errorf("metric exporter: %w", err)
	}
	logExp, err := otlploghttp.New(ctx)
	if err != nil {
		return nil, fmt.Errorf("log exporter: %w", err)
	}

	wireProviders(res, traceExp, metricExp, logExp)
	return &Result{
		SlogHandler: liveLogBridge,
		Shutdown:    shutdownInstalled,
	}, nil
}

// buildResource constructs the OTel resource from the startup identity
// attributes. Shared by Init and BindExporter so both paths emit the same
// service.name / service.version / service.instance.id.
func buildResource(cfg Config) (*resource.Resource, error) {
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
	return res, nil
}

// wireProviders installs trace/metric/log providers built from the given
// exporters into the global otel registries, sets the W3C propagator, binds
// the metric instruments, points the live log bridge at the new log provider,
// and marks the package installed — stashing the combined shutdown in
// installShutdown. Callers hold no lock; this takes installMu to flip the
// installed flag.
func wireProviders(res *resource.Resource, traceExp sdktrace.SpanExporter, metricExp sdkmetric.Exporter, logExp sdklog.Exporter) {
	shutdowns := []func(context.Context) error{}

	// W3C TraceContext propagator — required for traceparent in/out
	// across the supervisor→workspace→Claude-Code chain. internal/tracing
	// only installs this from tests, so production OTel runs need it here.
	otel.SetTextMapPropagator(propagation.TraceContext{})

	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(traceExp),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(tp)
	shutdowns = append(shutdowns, tp.Shutdown)

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

	lp := sdklog.NewLoggerProvider(
		sdklog.WithProcessor(sdklog.NewBatchProcessor(logExp)),
		sdklog.WithResource(res),
	)
	shutdowns = append(shutdowns, lp.Shutdown)
	// Point the startup-wired log bridge at the new provider. This is what
	// lets the late-bind (ConfigUpdate) path export logs without re-touching
	// the frozen logging fan-out — only the bridge's delegate changes.
	liveLogBridge.setDelegate(otelslog.NewHandler(scopeName, otelslog.WithLoggerProvider(lp)))

	shutdown := func(ctx context.Context) error {
		var combined error
		// Reverse order: logs flush last so any shutdown-time logs
		// from the trace/metric providers still ship.
		for i := len(shutdowns) - 1; i >= 0; i-- {
			if err := shutdowns[i](ctx); err != nil {
				combined = errors.Join(combined, err)
			}
		}
		return combined
	}

	installMu.Lock()
	installed = true
	installShutdown = shutdown
	installMu.Unlock()
}

// shutdownInstalled flushes + closes whatever providers were wired (by env-var
// Init or a late BindExporter), or no-ops when nothing was installed. Init
// hands this back as Result.Shutdown so the process flushes late-bound
// telemetry on exit.
func shutdownInstalled(ctx context.Context) error {
	installMu.Lock()
	fn := installShutdown
	installMu.Unlock()
	if fn == nil {
		return nil
	}
	return fn(ctx)
}

// BindExporter late-binds the OTLP/HTTP exporter when the agent receives its
// endpoint via ConfigUpdate (the no-OTEL_EXPORTER_OTLP_ENDPOINT-env startup
// path). It constructs trace/metric/log exporters pointed at endpoint and
// installs the SDK providers globally — the same wiring Init performs from
// env vars — so the ConfigUpdate path genuinely exports.
//
// No-op when endpoint is empty, or when the providers are already installed
// (env-var Init already ran, or a prior ConfigUpdate already bound). Logs,
// traces, and metrics all start flowing: wireProviders points the live log
// bridge (wired into the logging fan-out at startup) at the new log provider,
// so the ConfigUpdate path exports the same three signals the env-var path does.
func BindExporter(ctx context.Context, endpoint, token, dataset string) {
	if endpoint == "" {
		return
	}

	installMu.Lock()
	already := installed
	cfg := startupCfg
	installMu.Unlock()
	if already {
		// Providers were installed at startup from env vars (or a prior
		// ConfigUpdate). Don't double-install.
		slog.InfoContext(ctx, "observability.otlp_endpoint_received_already_bound",
			"endpoint", endpoint, "dataset", dataset)
		return
	}

	res, err := buildResource(cfg)
	if err != nil {
		slog.ErrorContext(ctx, "observability.otlp_bind_resource_failed", "err", err.Error())
		return
	}

	// The config endpoint is a BASE URL (like OTEL_EXPORTER_OTLP_ENDPOINT),
	// so append the standard per-signal path. WithEndpointURL treats its path
	// verbatim and does not default it — otlploghttp in particular would POST
	// to "/" for a path-less base — so we expand the path ourselves to keep
	// all three signals consistent with the env-var path.
	traceURL, err := otlpSignalURL(endpoint, "/v1/traces")
	if err != nil {
		slog.ErrorContext(ctx, "observability.otlp_bind_endpoint_parse_failed", "err", err.Error())
		return
	}
	metricURL, _ := otlpSignalURL(endpoint, "/v1/metrics")
	logURL, _ := otlpSignalURL(endpoint, "/v1/logs")

	// A token, when present, rides as a bearer Authorization header so the
	// customer's collector can authn.
	traceOpts := []otlptracehttp.Option{otlptracehttp.WithEndpointURL(traceURL)}
	metricOpts := []otlpmetrichttp.Option{otlpmetrichttp.WithEndpointURL(metricURL)}
	logOpts := []otlploghttp.Option{otlploghttp.WithEndpointURL(logURL)}
	if token != "" {
		hdr := map[string]string{"Authorization": "Bearer " + token}
		traceOpts = append(traceOpts, otlptracehttp.WithHeaders(hdr))
		metricOpts = append(metricOpts, otlpmetrichttp.WithHeaders(hdr))
		logOpts = append(logOpts, otlploghttp.WithHeaders(hdr))
	}

	traceExp, err := otlptracehttp.New(ctx, traceOpts...)
	if err != nil {
		slog.ErrorContext(ctx, "observability.otlp_bind_trace_failed", "err", err.Error())
		return
	}
	metricExp, err := otlpmetrichttp.New(ctx, metricOpts...)
	if err != nil {
		slog.ErrorContext(ctx, "observability.otlp_bind_metric_failed", "err", err.Error())
		return
	}
	logExp, err := otlploghttp.New(ctx, logOpts...)
	if err != nil {
		slog.ErrorContext(ctx, "observability.otlp_bind_log_failed", "err", err.Error())
		return
	}

	wireProviders(res, traceExp, metricExp, logExp)
	slog.InfoContext(ctx, "observability.otlp_endpoint_bound",
		"endpoint", endpoint, "dataset", dataset)
}

// otlpSignalURL appends the standard OTLP/HTTP signal path (e.g. "/v1/logs")
// to a base endpoint URL, preserving any base path the customer set (so
// "https://host/otlp" + "/v1/logs" → "https://host/otlp/v1/logs"). Mirrors how
// OTEL_EXPORTER_OTLP_ENDPOINT is expanded per signal on the env-var path.
func otlpSignalURL(base, signalPath string) (string, error) {
	u, err := url.Parse(base)
	if err != nil {
		return "", err
	}
	u.Path = strings.TrimRight(u.Path, "/") + signalPath
	return u.String(), nil
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
