package observability

import (
	"context"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"go.opentelemetry.io/otel"
)

// resetInstallState clears the package-global install flags so a test that
// drives BindExporter / Init starts from a clean, unconfigured pipeline.
func resetInstallState(t *testing.T) {
	t.Helper()
	installMu.Lock()
	installed = false
	startupCfg = Config{}
	installShutdown = nil
	installMu.Unlock()
	// Clear the live log bridge's delegate so each test starts with logs
	// dormant — otherwise a prior test's (closed) provider would linger.
	liveLogBridge.setDelegate(nil)
}

func TestInit_NoEndpoint_NoOp(t *testing.T) {
	resetInstallState(t)
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
	res, err := Init(context.Background(), Config{ServiceVersion: "test", AgentPodID: "p1"})
	if err != nil {
		t.Fatalf("Init no-op should not error: %v", err)
	}
	if res == nil {
		t.Fatal("Result must not be nil")
	}
	if res.SlogHandler == nil {
		t.Fatal("Init must always return the live log bridge, even in no-op mode")
	}
	if res.Shutdown == nil {
		t.Fatal("Shutdown must not be nil")
	}
	if err := res.Shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown error: %v", err)
	}
}

func TestBindExporter_InstallsExporter_TracesAndMetricsExport(t *testing.T) {
	resetInstallState(t)

	var traceHits, metricHits, logHits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/traces":
			traceHits.Add(1)
		case "/v1/metrics":
			metricHits.Add(1)
		case "/v1/logs":
			logHits.Add(1)
		}
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	t.Setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
	t.Setenv("OTEL_METRIC_EXPORT_INTERVAL", "100") // 100 ms, not the 30 s default

	ctx := context.Background()

	// Startup ran with no env endpoint → no-op Init. Capture identity cfg.
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
	res, err := Init(ctx, Config{ServiceVersion: "test", AgentPodID: "pod-bind-1"})
	if err != nil {
		t.Fatalf("Init no-op: %v", err)
	}
	if res.SlogHandler == nil {
		t.Fatal("Init must return the live log bridge even in no-op mode")
	}

	// ConfigUpdate delivers the endpoint → BindExporter installs the pipeline.
	BindExporter(ctx, srv.URL, "tok-123", "yaaos-dataset")

	// The pipeline must now be marked installed (not a logging-only stub).
	installMu.Lock()
	gotInstalled := installed
	installMu.Unlock()
	if !gotInstalled {
		t.Fatal("BindExporter did not install the SDK providers")
	}

	// A metric recorded after binding must reach the mock receiver. The
	// metric reader's 100ms interval makes this deterministic within the
	// deadline (unlike the trace batcher's multi-second flush).
	Metrics().CommandsClaimed.Add(ctx, 1)
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if metricHits.Load() > 0 {
			break
		}
		Metrics().CommandsClaimed.Add(ctx, 1)
		time.Sleep(50 * time.Millisecond) // reason: real OTLP/HTTP export to httptest.Server — goroutine blocked on OS network I/O, not durably blocked in synctest sense.
	}
	if metricHits.Load() == 0 {
		t.Errorf("BindExporter did not install a working metric exporter: no /v1/metrics POSTs")
	}
	// Traces are wired through the same call; a span emitted post-bind
	// flushes on the batcher's own cadence. We don't assert on it here to
	// avoid coupling the test to the batch timeout.
	_, span := otel.Tracer("test").Start(ctx, "bind.span")
	span.End()

	// Logs must also flow on the late-bind path — the bridge wired into the
	// fan-out at startup now delegates to the ConfigUpdate-bound provider.
	// res.Shutdown reaches the late-bound providers (via installShutdown) and
	// force-flushes, which also proves late-bound telemetry is flushed on exit.
	slog.New(res.SlogHandler).InfoContext(ctx, "after.bind.log")
	if err := res.Shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown (flush late-bound providers): %v", err)
	}
	if logHits.Load() == 0 {
		t.Errorf("BindExporter did not wire log export: no /v1/logs POSTs")
	}
}

func TestBindExporter_EmptyEndpoint_NoOp(t *testing.T) {
	resetInstallState(t)
	BindExporter(context.Background(), "", "", "")
	installMu.Lock()
	got := installed
	installMu.Unlock()
	if got {
		t.Error("BindExporter with empty endpoint must not install providers")
	}
}

func TestInit_WithMockReceiver_TracesAndMetricsAndLogs(t *testing.T) {
	resetInstallState(t)
	var traceHits, metricHits, logHits atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/traces":
			traceHits.Add(1)
		case "/v1/metrics":
			metricHits.Add(1)
		case "/v1/logs":
			logHits.Add(1)
		}
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", srv.URL)
	t.Setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
	t.Setenv("OTEL_METRIC_EXPORT_INTERVAL", "100") // 100 ms, not the 30 s default

	ctx := context.Background()
	res, err := Init(ctx, Config{ServiceVersion: "test", AgentPodID: "pod-test-42"})
	if err != nil {
		t.Fatalf("Init: %v", err)
	}
	t.Cleanup(func() { _ = res.Shutdown(context.Background()) })

	// Trace.
	_, span := otel.Tracer("test").Start(ctx, "test.span")
	span.End()

	// Metric.
	Metrics().CommandsClaimed.Add(ctx, 1)

	// Log — drive through the handler the SDK plugged into the logging
	// fan-out. Building a real slog.Record via a *slog.Logger is the
	// least flaky path.
	if res.SlogHandler == nil {
		t.Fatal("expected non-nil slog handler when endpoint is set")
	}
	l := slog.New(res.SlogHandler)
	l.InfoContext(ctx, "hello.from.otel")

	// Shutdown forces flush.
	if err := res.Shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown: %v", err)
	}

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if traceHits.Load() > 0 && metricHits.Load() > 0 && logHits.Load() > 0 {
			break
		}
		time.Sleep(20 * time.Millisecond) // reason: real OTLP/HTTP export to httptest.Server — goroutine blocked on OS network I/O, not durably blocked in synctest sense.
	}
	if traceHits.Load() == 0 {
		t.Errorf("no /v1/traces POSTs observed")
	}
	if metricHits.Load() == 0 {
		t.Errorf("no /v1/metrics POSTs observed")
	}
	if logHits.Load() == 0 {
		t.Errorf("no /v1/logs POSTs observed")
	}
}
