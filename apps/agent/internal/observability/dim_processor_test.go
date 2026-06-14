package observability

import (
	"context"
	"testing"

	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
)

// resetDims clears the global dim store so each test starts with empty values.
func resetDims(t *testing.T) {
	t.Helper()
	SetStandardDimensions("", "")
	t.Cleanup(func() { SetStandardDimensions("", "") })
}

// recordSpan emits one span through a TracerProvider built with DimProcessor
// and the given syncer exporter, then returns the exported spans.
// NOTE: tp.Shutdown clears the InMemoryExporter, so GetSpans is called before
// shutdown. The Cleanup registers shutdown so provider goroutines don't leak.
func recordSpan(t *testing.T, exp *tracetest.InMemoryExporter) tracetest.SpanStubs {
	t.Helper()
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithSyncer(exp),
		sdktrace.WithSampler(sdktrace.AlwaysSample()),
		sdktrace.WithSpanProcessor(NewDimProcessor()),
	)
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	_, span := tp.Tracer("test").Start(context.Background(), "test.span")
	span.End()
	return exp.GetSpans()
}

// attrValue returns the string value of a named attribute from a SpanStub,
// or "" if not found.
func attrValue(stub tracetest.SpanStub, key string) string {
	for _, kv := range stub.Attributes {
		if string(kv.Key) == key {
			return kv.Value.AsString()
		}
	}
	return ""
}

// TestDimProcessor_DimsSet verifies that when SetStandardDimensions has been
// called, every span carries org_id and agent_id.
func TestDimProcessor_DimsSet(t *testing.T) {
	resetDims(t)
	SetStandardDimensions("org-abc", "agent-xyz")

	exp := tracetest.NewInMemoryExporter()
	spans := recordSpan(t, exp)
	if len(spans) == 0 {
		t.Fatal("expected at least one span")
	}
	sp := spans[0]
	if got := attrValue(sp, "org_id"); got != "org-abc" {
		t.Errorf("org_id: want %q, got %q", "org-abc", got)
	}
	if got := attrValue(sp, "agent_id"); got != "agent-xyz" {
		t.Errorf("agent_id: want %q, got %q", "agent-xyz", got)
	}
}

// TestDimProcessor_EmptyDims verifies that pre-identity-exchange spans (dims
// not yet set) do not receive org_id or agent_id attributes.
func TestDimProcessor_EmptyDims(t *testing.T) {
	resetDims(t)
	// dims are empty — OnStart must be a no-op.

	exp := tracetest.NewInMemoryExporter()
	spans := recordSpan(t, exp)
	if len(spans) == 0 {
		t.Fatal("expected at least one span")
	}
	sp := spans[0]
	if got := attrValue(sp, "org_id"); got != "" {
		t.Errorf("org_id should be absent before identity exchange, got %q", got)
	}
	if got := attrValue(sp, "agent_id"); got != "" {
		t.Errorf("agent_id should be absent before identity exchange, got %q", got)
	}
}

// TestDimProcessor_DimsUpdatedBetweenSpans verifies that a dim-store mutation
// between two spans is reflected on the second span. This covers the "late
// exchange" scenario where the agent processes a span before and after
// SetStandardDimensions is called.
func TestDimProcessor_DimsUpdatedBetweenSpans(t *testing.T) {
	resetDims(t)

	exp := tracetest.NewInMemoryExporter()
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithSyncer(exp),
		sdktrace.WithSampler(sdktrace.AlwaysSample()),
		sdktrace.WithSpanProcessor(NewDimProcessor()),
	)
	// Register shutdown via Cleanup; do NOT call Shutdown before GetSpans
	// because InMemoryExporter.Shutdown clears the span list.
	t.Cleanup(func() { _ = tp.Shutdown(context.Background()) })
	tracer := tp.Tracer("test")

	// Span 1: dims not set yet.
	_, s1 := tracer.Start(context.Background(), "span.before")
	s1.End()

	// Mutate dims to simulate identity exchange completing.
	SetStandardDimensions("org-new", "agent-new")

	// Span 2: dims now set.
	_, s2 := tracer.Start(context.Background(), "span.after")
	s2.End()

	stubs := exp.GetSpans()
	if len(stubs) < 2 {
		t.Fatalf("expected at least 2 spans, got %d", len(stubs))
	}

	// Find by name.
	before, after := tracetest.SpanStub{}, tracetest.SpanStub{}
	for _, s := range stubs {
		switch s.Name {
		case "span.before":
			before = s
		case "span.after":
			after = s
		}
	}

	// span.before: no dims.
	if got := attrValue(before, "org_id"); got != "" {
		t.Errorf("span.before org_id should be absent, got %q", got)
	}

	// span.after: has new dims.
	if got := attrValue(after, "org_id"); got != "org-new" {
		t.Errorf("span.after org_id: want %q, got %q", "org-new", got)
	}
	if got := attrValue(after, "agent_id"); got != "agent-new" {
		t.Errorf("span.after agent_id: want %q, got %q", "agent-new", got)
	}
}
