package observability

import (
	"context"

	"go.opentelemetry.io/otel/attribute"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// DimProcessor is a SpanProcessor that stamps every span with the process-wide
// org_id and agent_id dimensions. It reads the current values from the
// module-level dim store (guarded by stdDimsMu) at OnStart time so that a
// dim-store mutation between two spans is reflected immediately on the next
// span — no restart required.
//
// Before identity exchange (SetStandardDimensions has not been called), both
// values are empty strings. In that case OnStart is a no-op: we never stamp
// empty dimensions on a span. Callers that need per-span overrides should
// set attributes directly on the span after opening it.
type DimProcessor struct{}

// NewDimProcessor returns a DimProcessor ready for use as a SpanProcessor.
func NewDimProcessor() *DimProcessor {
	return &DimProcessor{}
}

// OnStart stamps org_id + agent_id on every span as it starts, reading the
// current values from the global dim store. No-op when either value is empty
// (pre-identity-exchange spans like agent.identity_exchange).
func (p *DimProcessor) OnStart(_ context.Context, s sdktrace.ReadWriteSpan) {
	stdDimsMu.RLock()
	org := stdOrgID
	agent := stdAgentID
	stdDimsMu.RUnlock()
	if org == "" || agent == "" {
		return
	}
	s.SetAttributes(
		attribute.String("org_id", org),
		attribute.String("agent_id", agent),
	)
}

// OnEnd is a no-op; dimension stamping happens at span start.
func (p *DimProcessor) OnEnd(_ sdktrace.ReadOnlySpan) {}

// Shutdown is a no-op; this processor owns no resources.
func (p *DimProcessor) Shutdown(_ context.Context) error { return nil }

// ForceFlush is a no-op; there is no buffer to flush.
func (p *DimProcessor) ForceFlush(_ context.Context) error { return nil }
