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
	semconv "go.opentelemetry.io/otel/semconv/v1.40.0"
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

func TestInit_AlwaysNoOp_ReturnsLiveLogBridge(t *testing.T) {
	resetInstallState(t)

	// Set OTEL_EXPORTER_OTLP_ENDPOINT to confirm Init ignores it.
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://should-be-ignored.example.com")

	result, err := Init(context.Background(), Config{ServiceVersion: "test"})
	if err != nil {
		t.Fatalf("Init: %v", err)
	}
	if result == nil {
		t.Fatal("Init returned nil result")
	}
	if result.SlogHandler == nil {
		t.Error("Init result.SlogHandler is nil; expected live log bridge")
	}

	installMu.Lock()
	wasInstalled := installed
	installMu.Unlock()
	if wasInstalled {
		t.Error("Init installed providers; expected telemetry-dark until BindExporter")
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

	setMetricExportIntervalForTests(t, 100*time.Millisecond)

	ctx := context.Background()

	// Startup ran without BindExporter — Init is always a no-op. Capture identity cfg.
	res, err := Init(ctx, Config{ServiceVersion: "test"})
	if err != nil {
		t.Fatalf("Init no-op: %v", err)
	}
	if res.SlogHandler == nil {
		t.Fatal("Init must return the live log bridge even in no-op mode")
	}

	// ConfigUpdate delivers the endpoint → BindExporter installs the pipeline.
	BindExporter(ctx, srv.URL, "tok-123", "yaaos-dataset", "")

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
	BindExporter(context.Background(), "", "", "", "")
	installMu.Lock()
	got := installed
	installMu.Unlock()
	if got {
		t.Error("BindExporter with empty endpoint must not install providers")
	}
}

// TestBindExporter_StampsDeploymentEnvironmentName verifies that BindExporter
// late-binds the environment string into startupCfg, and that buildResource
// subsequently produces a resource carrying deployment.environment.name=staging.
// Uses the simpler direct-inspection form (no OTLP protobuf roundtrip) because
// the existing httptest receiver infrastructure does not decode resource attributes.
func TestBindExporter_StampsDeploymentEnvironmentName(t *testing.T) {
	resetInstallState(t)
	setMetricExportIntervalForTests(t, 100*time.Millisecond)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	_, _ = Init(context.Background(), Config{ServiceVersion: "test", InstanceID: "i-1"})
	BindExporter(context.Background(), srv.URL, "tok", "dataset", "staging")

	// Verify the late-bind stored the environment in startupCfg.
	installMu.Lock()
	env := startupCfg.Environment
	installMu.Unlock()
	if env != "staging" {
		t.Errorf("startupCfg.Environment = %q after BindExporter, want %q", env, "staging")
	}

	// Verify buildResource includes the attribute when Environment is non-empty.
	res, err := buildResource(Config{ServiceVersion: "test", InstanceID: "i-1", Environment: "staging"})
	if err != nil {
		t.Fatalf("buildResource: %v", err)
	}
	attrs := res.Attributes()
	wantKey := semconv.DeploymentEnvironmentName("staging").Key
	for _, kv := range attrs {
		if kv.Key == wantKey {
			if got := kv.Value.AsString(); got != "staging" {
				t.Errorf("deployment.environment.name = %q, want %q", got, "staging")
			}
			_ = shutdownInstalled(context.Background())
			return
		}
	}
	t.Errorf("resource missing deployment.environment.name attribute; attrs = %v", attrs)
	_ = shutdownInstalled(context.Background())
}

// TestBindExporter_EmptyEnvironment_OmitsAttribute verifies that when BindExporter
// is called with an empty environment string, buildResource does NOT include a
// deployment.environment.name attribute (avoids stamping an explicit "" tag).
func TestBindExporter_EmptyEnvironment_OmitsAttribute(t *testing.T) {
	resetInstallState(t)
	setMetricExportIntervalForTests(t, 100*time.Millisecond)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	_, _ = Init(context.Background(), Config{ServiceVersion: "test", InstanceID: "i-1"})
	BindExporter(context.Background(), srv.URL, "tok", "dataset", "")

	// Verify the late-bind stored the empty environment in startupCfg.
	installMu.Lock()
	env := startupCfg.Environment
	installMu.Unlock()
	if env != "" {
		t.Errorf("startupCfg.Environment = %q after BindExporter with empty env, want %q", env, "")
	}

	// Verify buildResource omits the attribute when Environment is empty.
	res, err := buildResource(Config{ServiceVersion: "test", InstanceID: "i-1", Environment: ""})
	if err != nil {
		t.Fatalf("buildResource: %v", err)
	}
	wantKey := semconv.DeploymentEnvironmentName("").Key
	for _, kv := range res.Attributes() {
		if kv.Key == wantKey {
			t.Errorf("resource carries deployment.environment.name=%q; want attribute absent", kv.Value.AsString())
			break
		}
	}
	_ = shutdownInstalled(context.Background())
}
