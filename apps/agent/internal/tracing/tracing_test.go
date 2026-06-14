package tracing

import (
	"context"
	"strings"
	"testing"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
)

func TestInit_InstallsTraceContextPropagator(t *testing.T) {
	Init(false)
	// Round-trip a traceparent through the global propagator.
	parent := "00-aabbccddeeff00112233445566778899-0011223344556677-01"
	ctx := ExtractContext(context.Background(), parent)
	got := InjectTraceparent(ctx)
	if got != parent {
		t.Errorf("traceparent round-trip: want %q got %q", parent, got)
	}
}

func TestInit_NoExporterReturnsNil(t *testing.T) {
	if exp := Init(false); exp != nil {
		t.Errorf("Init(false) should return nil exporter, got %v", exp)
	}
}

func TestExtract_EmptyTraceparent_ReturnsInputCtx(t *testing.T) {
	Init(false)
	in := context.WithValue(context.Background(), ctxKey("k"), "v")
	out := ExtractContext(in, "")
	if out.Value(ctxKey("k")) != "v" {
		t.Errorf("empty traceparent should preserve input ctx values")
	}
}

type ctxKey string

func TestInjectTraceparent_NoSpan_ReturnsEmpty(t *testing.T) {
	Init(false)
	if tp := InjectTraceparent(context.Background()); tp != "" {
		t.Errorf("want empty traceparent with no span, got %q", tp)
	}
}

func TestStartSpan_RecordsSpanWithAttributes(t *testing.T) {
	exp := Init(true)
	defer exp.Reset()

	ctx, end := StartSpan(context.Background(), "test.unit",
		attribute.String("foo", "bar"),
	)
	if InjectTraceparent(ctx) == "" {
		t.Fatal("expected non-empty traceparent inside span")
	}
	end(nil)

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(spans))
	}
	if spans[0].Name != "test.unit" {
		t.Errorf("span name: want test.unit got %q", spans[0].Name)
	}
	var sawAttr bool
	for _, a := range spans[0].Attributes {
		if a.Key == "foo" && a.Value.AsString() == "bar" {
			sawAttr = true
		}
	}
	if !sawAttr {
		t.Errorf("expected attribute foo=bar, got %v", spans[0].Attributes)
	}
}

func TestStartSpan_RecordsErrorOnEnd(t *testing.T) {
	exp := Init(true)
	defer exp.Reset()

	_, end := StartSpan(context.Background(), "test.failure")
	end(errString("kaboom"))

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(spans))
	}
	if spans[0].Status.Code.String() != "Error" {
		t.Errorf("status code: want Error got %s", spans[0].Status.Code.String())
	}
	if !strings.Contains(spans[0].Status.Description, "kaboom") {
		t.Errorf("status description: want substring 'kaboom' got %q", spans[0].Status.Description)
	}
}

// TestStartSpan_RecordsErrorOnFailure asserts that calling end(err) with a
// non-nil error both sets Status.Code = Error and records an exception event
// (the "exception" event emitted by span.RecordError). Uses an InMemoryExporter
// for deterministic, synchronous assertion.
func TestStartSpan_RecordsErrorOnFailure(t *testing.T) {
	exp := Init(true)
	defer exp.Reset()

	errSentinel := errString("sentinel-failure")
	_, end := StartSpan(context.Background(), "test.error.failure")
	end(errSentinel)

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(spans))
	}
	s := spans[0]
	if s.Status.Code.String() != "Error" {
		t.Errorf("Status.Code: want Error, got %s", s.Status.Code.String())
	}
	// span.RecordError emits an "exception" event per the OTel spec.
	var sawException bool
	for _, ev := range s.Events {
		if ev.Name == "exception" {
			sawException = true
			break
		}
	}
	if !sawException {
		t.Errorf("expected RecordError exception event in span events, got %v", s.Events)
	}
}

type errString string

func (e errString) Error() string { return string(e) }

func TestParentChildLinkage_ExtractedParent(t *testing.T) {
	// Simulate the backend → supervisor hop: the backend emits a
	// traceparent; we extract + open a child span; assert the child
	// shares the parent's trace_id.
	exp := Init(true)
	defer exp.Reset()

	parent := "00-aabbccddeeff00112233445566778899-0011223344556677-01"
	ctx := ExtractContext(context.Background(), parent)
	_, end := StartSpan(ctx, "child.span")
	end(nil)

	spans := exp.GetSpans()
	if len(spans) != 1 {
		t.Fatalf("want 1 span, got %d", len(spans))
	}
	childTraceID := spans[0].SpanContext.TraceID().String()
	if childTraceID != "aabbccddeeff00112233445566778899" {
		t.Errorf("child trace_id: want aabbccddeeff00112233445566778899 got %s", childTraceID)
	}
	parentSpanID := spans[0].Parent.SpanID().String()
	if parentSpanID != "0011223344556677" {
		t.Errorf("parent span_id: want 0011223344556677 got %s", parentSpanID)
	}
}

func TestTraceparentEnv_Format(t *testing.T) {
	exp := Init(true)
	defer exp.Reset()

	ctx, end := StartSpan(context.Background(), "owner")
	envVal := TraceparentEnv(ctx)
	end(nil)

	if !strings.HasPrefix(envVal, "TRACEPARENT=00-") {
		t.Errorf("want TRACEPARENT=00- prefix, got %q", envVal)
	}
}

func TestTraceparentEnv_NoSpan_Empty(t *testing.T) {
	Init(false)
	if got := TraceparentEnv(context.Background()); got != "" {
		t.Errorf("want empty env when no span, got %q", got)
	}
}

func TestInit_GlobalPropagatorPersists(t *testing.T) {
	Init(false)
	// The global propagator should round-trip without needing a per-call
	// argument — that's the contract callers rely on.
	if otel.GetTextMapPropagator() == nil {
		t.Fatal("global propagator not set")
	}
}
