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

func TestInit_NoEndpoint_NoOp(t *testing.T) {
	t.Setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")
	res, err := Init(context.Background(), Config{ServiceVersion: "test", AgentPodID: "p1"})
	if err != nil {
		t.Fatalf("Init no-op should not error: %v", err)
	}
	if res == nil {
		t.Fatal("Result must not be nil")
	}
	if res.SlogHandler != nil {
		t.Fatal("no-op Init must not produce a slog handler")
	}
	if res.Shutdown == nil {
		t.Fatal("Shutdown must not be nil")
	}
	if err := res.Shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown error: %v", err)
	}
}

func TestInit_WithMockReceiver_TracesAndMetricsAndLogs(t *testing.T) {
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
		time.Sleep(20 * time.Millisecond)
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
