// Package tracing wires OpenTelemetry into the agent.
//
// Three hops link spans into one distributed trace_id:
//
//  1. Backend → supervisor — the AgentCommand wire carries a W3C
//     `traceparent`. The supervisor extracts it into a parent context,
//     starts a `supervisor.dispatch.<kind>` span, and rewrites the
//     wire's traceparent before forwarding so the workspace sees the
//     supervisor's span as its parent.
//  2. Supervisor → workspace — same pattern. The workspace extracts the
//     per-command traceparent and starts a `workspace.handle.<kind>`
//     span around the Handler invocation.
//  3. Workspace → Claude Code subprocess — the supervisor's spawn step
//     exports `TRACEPARENT={parent}` into the workspace process's env;
//     a Claude Code shim can read that to start its span.
//
// No production exporter is wired; the agent emits no telemetry to an
// external backend. Tests call `Init(true)` to register
// the in-memory exporter + return it so assertions can read the
// emitted spans.
//
// The trace_id propagates regardless of whether an exporter is wired,
// because span contexts are derived from the propagator (not the SDK
// provider). This means: production runs with the default no-op
// provider — spans are created but discarded — yet the traceparent
// values on the wire still chain correctly.
package tracing

import (
	"context"
	"encoding/hex"
	"strings"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	oteltrace "go.opentelemetry.io/otel/trace"
	"go.opentelemetry.io/otel/trace/noop"
)

// tracerName is the instrumentation name the agent's spans are tagged
// with. Matches the convention `<binary>/<package>` used elsewhere in
// the codebase.
const tracerName = "github.com/yaaos/agent/internal/tracing"

// traceparentHeader is the W3C trace context header name. We use the same
// name as the IETF spec for both wire fields + env vars; both consumers
// understand the canonical form.
const traceparentHeader = "traceparent"

// Init installs the W3C TraceContext propagator globally. When
// `withInMemory == true` it also installs an in-memory tracer provider
// and returns the matching exporter so tests can read spans. Production
// callers pass `false`; the SDK falls back to a no-op tracer provider
// (spans created, immediately discarded) — only the propagator matters
// for wire-format correctness.
//
// Calling Init twice replaces the previous provider. Tests rely on this
// for isolation.
func Init(withInMemory bool) *tracetest.InMemoryExporter {
	otel.SetTextMapPropagator(propagation.TraceContext{})
	if !withInMemory {
		// No-op provider: spans pass through propagation but aren't
		// exported anywhere.
		otel.SetTracerProvider(noop.NewTracerProvider())
		return nil
	}
	exp := tracetest.NewInMemoryExporter()
	tp := trace.NewTracerProvider(
		trace.WithSyncer(exp),
		// Sample everything in tests so assertions are deterministic.
		trace.WithSampler(trace.AlwaysSample()),
	)
	otel.SetTracerProvider(tp)
	return exp
}

// ExtractContext returns a context carrying the parent span pulled from
// the supplied W3C traceparent header value. If the header is empty or
// malformed the input ctx is returned unchanged — callers can safely
// pass commands that arrive without trace context.
//
// Implementation: parses the traceparent directly and installs it as the
// active span context via `trace.ContextWithSpanContext`. We bypass
// `TraceContext.Extract` here because it stores the parent in the
// *remote-span* slot rather than the *active-span* slot — and SDK
// `tracer.Start` reads the active slot to derive the new span's parent.
// Without this bridging, every child span would get a fresh trace_id
// instead of chaining off the supplied parent.
func ExtractContext(ctx context.Context, traceparent string) context.Context {
	sc, ok := parseTraceparent(traceparent)
	if !ok {
		return ctx
	}
	return oteltrace.ContextWithSpanContext(ctx, sc)
}

// parseTraceparent decodes a W3C `traceparent` header value.
// Format: `<version>-<trace_id:32hex>-<span_id:16hex>-<flags:2hex>`.
// Returns the SpanContext flagged Remote so SDK derivations treat it as a
// distributed parent rather than a locally-started span.
func parseTraceparent(value string) (oteltrace.SpanContext, bool) {
	if value == "" {
		return oteltrace.SpanContext{}, false
	}
	parts := strings.Split(value, "-")
	if len(parts) != 4 || len(parts[0]) != 2 || len(parts[1]) != 32 || len(parts[2]) != 16 || len(parts[3]) != 2 {
		return oteltrace.SpanContext{}, false
	}
	traceIDBytes, err := hex.DecodeString(parts[1])
	if err != nil {
		return oteltrace.SpanContext{}, false
	}
	spanIDBytes, err := hex.DecodeString(parts[2])
	if err != nil {
		return oteltrace.SpanContext{}, false
	}
	flagsBytes, err := hex.DecodeString(parts[3])
	if err != nil {
		return oteltrace.SpanContext{}, false
	}
	var traceID oteltrace.TraceID
	var spanID oteltrace.SpanID
	copy(traceID[:], traceIDBytes)
	copy(spanID[:], spanIDBytes)
	return oteltrace.NewSpanContext(oteltrace.SpanContextConfig{
		TraceID:    traceID,
		SpanID:     spanID,
		TraceFlags: oteltrace.TraceFlags(flagsBytes[0]),
		Remote:     true,
	}), true
}

// InjectTraceparent serializes the active span context (if any) as a W3C
// traceparent header value. Returns the empty string when no span is in
// the context.
func InjectTraceparent(ctx context.Context) string {
	carrier := propagation.HeaderCarrier{}
	otel.GetTextMapPropagator().Inject(ctx, carrier)
	return carrier.Get(traceparentHeader)
}

// StartSpan begins a span named `name` derived from the parent in `ctx`.
// Returns the new context (carrying the child span) and an `end` closure
// that records the operation's error + finalizes the span. Pass `nil`
// to `end` on success; non-nil errors set the span's status to Error.
//
// Use as:
//
//	ctx, end := tracing.StartSpan(ctx, "supervisor.dispatch.ProvisionWorkspace",
//	    attribute.String("workspace_id", id))
//	defer end(err)
func StartSpan(ctx context.Context, name string, attrs ...attribute.KeyValue) (context.Context, func(err error)) {
	tracer := otel.GetTracerProvider().Tracer(tracerName)
	ctx, span := tracer.Start(ctx, name, oteltrace.WithAttributes(attrs...))
	end := func(err error) {
		if err != nil {
			span.RecordError(err)
			span.SetStatus(codes.Error, err.Error())
		}
		span.End()
	}
	return ctx, end
}

// TraceparentEnv returns an `os/exec.Cmd.Env`-shaped string
// (`TRACEPARENT=<value>`) carrying the current span's traceparent.
// Empty string when there's no active span. Callers append it to the
// child process's environment so a subprocess shim can reconstruct the
// parent context.
func TraceparentEnv(ctx context.Context) string {
	tp := InjectTraceparent(ctx)
	if tp == "" {
		return ""
	}
	return "TRACEPARENT=" + tp
}
