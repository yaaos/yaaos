package observability

import (
	"testing"
	"time"
)

// setMetricExportIntervalForTests overrides metricExportInterval for the
// duration of a single test. Use this in place of t.Setenv("OTEL_METRIC_EXPORT_INTERVAL", ...)
// — the package reads no env vars; the override is the only mechanism.
func setMetricExportIntervalForTests(t *testing.T, d time.Duration) {
	t.Helper()
	prev := metricExportInterval
	metricExportInterval = d
	t.Cleanup(func() { metricExportInterval = prev })
}
