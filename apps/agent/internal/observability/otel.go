// Package observability wires the agent's OTel SDK.
//
// Install model: the agent reads no OTel env vars. Init wires the live log
// bridge and stashes the startup Config (service version, instance id placeholder).
// SDK trace/metric/log providers install only via BindExporter, called from
// the supervisor's ConfigUpdate handler with the endpoint/token/dataset/environment
// the backend delivers. Before that call, instruments resolve to no-op providers
// and the agent is telemetry-dark. There is no env-var startup path.
package observability

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"strings"
	"sync"
	"time"

	"go.opentelemetry.io/contrib/bridges/otelslog"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
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
// attributes on every signal. Read once at startup; InstanceID may be
// updated later via SetInstanceID once the backend assigns it.
type Config struct {
	ServiceVersion string // e.g. "0.0.1" or build-stamped commit.
	// InstanceID is the backend-derived role-session-name from the STS ARN
	// (workspace_agents.instance_id). Only known after identity exchange;
	// set via SetInstanceID before BindExporter runs so the late-bind path
	// emits the correct service.instance.id.
	InstanceID  string
	Environment string // OTel deployment.environment.name; set via BindExporter from ConfigUpdate.
}

// installState tracks whether the SDK providers have been wired into the
// global otel registries so BindExporter never double-installs. startupCfg
// captures the identity attributes Init received so BindExporter can build
// the same resource when it late-binds.
var (
	installMu  sync.Mutex
	installed  bool
	startupCfg Config
	// installShutdown flushes + closes providers wired by BindExporter.
	// Init's returned Shutdown reads it, so the process flushes late-bound
	// telemetry on exit even though BindExporter ran after Init returned.
	installShutdown func(context.Context) error
)

// metricExportInterval is the metric flush interval used by wireProviders.
// Production uses the SDK's 30s default. Tests override it via
// setMetricExportIntervalForTests. Never read from env vars.
var metricExportInterval = 30 * time.Second

// Result is what Init returns.
type Result struct {
	// SlogHandler is the live log bridge — always non-nil. The caller passes
	// it to logging.Config.ExtraHandlers once at startup; it stays dormant
	// until a logger provider is installed by BindExporter.
	SlogHandler slog.Handler

	// Shutdown flushes + closes every initialized provider. Safe to
	// call even when Init was a no-op.
	Shutdown func(context.Context) error
}

func Init(_ context.Context, cfg Config) (*Result, error) {
	installMu.Lock()
	startupCfg = cfg
	installMu.Unlock()

	// Providers stay uninstalled until BindExporter is called from the
	// ConfigUpdate handler. The live log bridge is returned now so the caller
	// wires it into the logging fan-out at startup; BindExporter swaps its
	// delegate once providers exist.
	return &Result{SlogHandler: liveLogBridge, Shutdown: shutdownInstalled}, nil
}

// SetInstanceID updates the stored startup config with the backend-assigned
// instance_id (role-session-name from the STS ARN). Must be called after
// identity exchange and before BindExporter so the late-bind path emits the
// correct service.instance.id on every OTel signal.
//
// No-op when BindExporter has already installed providers — rebuilding the
// resource after wireProviders would require restarting the SDK. The supervisor's
// ordering (identity exchange → SetInstanceID → first ConfigUpdate → BindExporter)
// guarantees this never happens in production.
func SetInstanceID(instanceID string) {
	installMu.Lock()
	startupCfg.InstanceID = instanceID
	installMu.Unlock()
}

// buildResource constructs the OTel resource from the startup identity
// attributes. Called by BindExporter to build the resource at install time
// with the service.name / service.version / service.instance.id / deployment.environment.name.
func buildResource(cfg Config) (*resource.Resource, error) {
	attrs := []attribute.KeyValue{
		semconv.ServiceName("agent"),
		semconv.ServiceVersion(cfg.ServiceVersion),
		// service.instance.id is the backend-assigned instance_id
		// (workspace_agents.instance_id = role-session-name from the STS
		// ARN). Operators use it to correlate OTel signals to a specific
		// workspace_agents row. Empty at early startup (before identity
		// exchange); populated via SetInstanceID before BindExporter.
		semconv.ServiceInstanceID(cfg.InstanceID),
	}
	// Stamp deployment.environment.name only when non-empty. An empty string
	// would pollute Dash0 with an explicit "" tag rather than the desired
	// "no tag" state.
	if cfg.Environment != "" {
		attrs = append(attrs, semconv.DeploymentEnvironmentName(cfg.Environment))
	}
	res, err := resource.Merge(
		resource.Default(),
		resource.NewWithAttributes(semconv.SchemaURL, attrs...),
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
		// DimProcessor stamps org_id + agent_id on every span at OnStart (after
		// identity exchange). BatchSpanProcessor reads the span at OnEnd, and span
		// attributes stay mutable until then, so processor registration order does
		// not affect which attributes reach the exporter.
		sdktrace.WithSpanProcessor(NewDimProcessor()),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(tp)
	shutdowns = append(shutdowns, tp.Shutdown)

	mp := sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(sdkmetric.NewPeriodicReader(
			metricExp,
			sdkmetric.WithInterval(metricExportInterval),
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

// shutdownInstalled flushes + closes whatever providers were wired by
// BindExporter, or no-ops when nothing was installed. Init hands this back as
// Result.Shutdown so the process flushes late-bound telemetry on exit.
func shutdownInstalled(ctx context.Context) error {
	installMu.Lock()
	fn := installShutdown
	installMu.Unlock()
	if fn == nil {
		return nil
	}
	return fn(ctx)
}

// BindExporter installs the OTel SDK providers from the ConfigUpdate-delivered
// telemetry config. Constructs trace/metric/log exporters pointed at endpoint
// with the bearer token in an Authorization header, then installs the SDK
// providers globally. wireProviders points the live log bridge (wired into
// the logging fan-out at startup) at the new log provider, so logs, traces,
// and metrics all start flowing.
//
// No-op when endpoint is empty (control plane signaled "telemetry off") or
// when providers are already installed (second ConfigUpdate, never expected
// in production).
func BindExporter(ctx context.Context, endpoint, token, dataset, environment string) {
	if endpoint == "" {
		return
	}

	installMu.Lock()
	already := installed
	startupCfg.Environment = environment // late-bind, mirroring SetInstanceID
	cfg := startupCfg
	installMu.Unlock()
	if already {
		// Providers were already installed by a prior BindExporter call.
		// Don't double-install.
		slog.InfoContext(ctx, "observability.otlp_endpoint_received_already_bound",
			"endpoint", endpoint, "dataset", dataset, "environment", environment)
		return
	}

	res, err := buildResource(cfg)
	if err != nil {
		slog.ErrorContext(ctx, "observability.otlp_bind_resource_failed", "err", err.Error())
		return
	}

	// The config endpoint is a BASE URL so append the standard per-signal
	// path. WithEndpointURL treats its path verbatim and does not default it —
	// otlploghttp in particular would POST to "/" for a path-less base — so
	// we expand the path ourselves to keep all three signals consistent.
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
		"endpoint", endpoint, "dataset", dataset, "environment", environment)
}

// otlpSignalURL appends the standard OTLP/HTTP signal path (e.g. "/v1/logs")
// to a base endpoint URL, preserving any base path the customer set (so
// "https://host/otlp" + "/v1/logs" → "https://host/otlp/v1/logs").
func otlpSignalURL(base, signalPath string) (string, error) {
	u, err := url.Parse(base)
	if err != nil {
		return "", err
	}
	u.Path = strings.TrimRight(u.Path, "/") + signalPath
	return u.String(), nil
}
