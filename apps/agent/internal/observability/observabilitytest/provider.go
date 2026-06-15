// Package observabilitytest is a test-only seam for the observability package.
// It owns the ritual of installing a ManualReader-backed MeterProvider as the
// global OTel provider, rebinding the observability instruments against it, and
// restoring the prior provider on cleanup — so individual test packages don't
// hand-roll global-provider mutation or import the OTel metric SDK directly.
//
// depguard forbids non-_test.go files from importing this package (see the
// quarantine_observabilitytest rule in apps/agent/.golangci.yml).
package observabilitytest

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"
	"go.opentelemetry.io/otel/sdk/metric/metricdata"

	"github.com/yaaos/agent/internal/observability"
)

// MetricCapture wraps the ManualReader installed by InstallTestMeterProvider
// and exposes collection helpers. Tests hold this opaque handle and never name
// the OTel SDK types themselves.
type MetricCapture struct {
	reader *sdkmetric.ManualReader
}

// InstallTestMeterProvider installs a ManualReader-backed MeterProvider as the
// global OTel meter provider, rebinds observability.Metrics() instruments
// against it, and registers a t.Cleanup that shuts the provider down, restores
// the previous global provider, and rebinds again. Returns a MetricCapture for
// reading recorded metrics.
func InstallTestMeterProvider(t *testing.T) *MetricCapture {
	t.Helper()
	prev := otel.GetMeterProvider()
	reader := sdkmetric.NewManualReader()
	mp := sdkmetric.NewMeterProvider(sdkmetric.WithReader(reader))
	otel.SetMeterProvider(mp)
	observability.RebindMetrics()
	t.Cleanup(func() {
		_ = mp.Shutdown(context.Background())
		otel.SetMeterProvider(prev)
		observability.RebindMetrics()
	})
	return &MetricCapture{reader: reader}
}

// CounterSums collects the reader and returns, for the named Int64 counter, a
// map of one attribute's value → summed count. Use it to assert per-bucket
// counts (e.g. metricName="yaaos.agent.claim.outcome", attrKey="outcome").
func (c *MetricCapture) CounterSums(t *testing.T, metricName, attrKey string) map[string]int64 {
	t.Helper()
	var rm metricdata.ResourceMetrics
	if err := c.reader.Collect(context.Background(), &rm); err != nil {
		t.Fatalf("collect metrics: %v", err)
	}
	result := make(map[string]int64)
	for _, sm := range rm.ScopeMetrics {
		for _, m := range sm.Metrics {
			if m.Name != metricName {
				continue
			}
			sum, ok := m.Data.(metricdata.Sum[int64])
			if !ok {
				t.Fatalf("%s is not a Sum[int64]: %T", metricName, m.Data)
			}
			for _, dp := range sum.DataPoints {
				v, _ := dp.Attributes.Value(attribute.Key(attrKey))
				result[v.AsString()] += dp.Value
			}
		}
	}
	return result
}
