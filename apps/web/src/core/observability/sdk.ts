/**
 * OpenTelemetry SDK initialization for the yaaos SPA.
 *
 * Call configure() once at boot (main.tsx) before rendering.
 * Export is gated on the collector endpoint: endpoint present → export via
 * OTLP/HTTP; endpoint absent → SDK still creates spans but does not export.
 * No feature flag needed — the gating condition is the endpoint itself.
 *
 * Instrumentations registered:
 * - FetchInstrumentation: injects traceparent on /api/* fetches so browser
 *   spans continue as children of the backend's trace automatically.
 * - DocumentLoadInstrumentation: captures page-load performance entries.
 * - UserInteractionInstrumentation: wraps click/submit handlers with spans.
 *
 * Identity: SpanProcessor.onStart stamps yaaos.org_id / yaaos.user_id from
 * the module-scope identity holder (see identity.ts). Call setIdentity()
 * after auth resolves. No baggage header is ever emitted.
 */

import { SpanStatusCode, trace } from "@opentelemetry/api";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { DocumentLoadInstrumentation } from "@opentelemetry/instrumentation-document-load";
import { FetchInstrumentation } from "@opentelemetry/instrumentation-fetch";
import { UserInteractionInstrumentation } from "@opentelemetry/instrumentation-user-interaction";
import { resourceFromAttributes } from "@opentelemetry/resources";
import {
  BatchSpanProcessor,
  NoopSpanProcessor,
  WebTracerProvider,
} from "@opentelemetry/sdk-trace-web";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";
import { _resetIdentityForTests } from "./identity";
import { YaaosSpanProcessor } from "./span-processor";

export { setIdentity } from "./identity";
export { YaaosSpanProcessor } from "./span-processor";

export interface ObservabilityConfig {
  collectorEndpoint: string | undefined;
}

let _provider: WebTracerProvider | null = null;

/**
 * Initialize the OTel SDK. Safe to call multiple times (subsequent calls are
 * no-ops once initialized, unless _resetObservabilityForTests() was called).
 */
export function configure(config: ObservabilityConfig): void {
  if (_provider !== null) return;

  const resource = resourceFromAttributes({
    [ATTR_SERVICE_NAME]: "yaaos-web",
  });

  // Build the span processor pipeline. YaaosSpanProcessor stamps yaaos.*
  // identity attributes on every web-originating span.
  const spanProcessors = config.collectorEndpoint
    ? [
        new YaaosSpanProcessor(),
        // Batch export to the OTLP collector. Spans are batched to minimize request overhead.
        new BatchSpanProcessor(
          new OTLPTraceExporter({ url: `${config.collectorEndpoint}/v1/traces` }),
        ),
      ]
    : [
        new YaaosSpanProcessor(),
        // No collector endpoint — SDK is active (spans created, traceparent injected)
        // but no data leaves the browser. Instrumentation still generates trace context
        // so the backend gets a valid parent span.
        new NoopSpanProcessor(),
      ];

  _provider = new WebTracerProvider({ resource, spanProcessors });
  _provider.register();

  // Register auto-instrumentations after provider is registered.
  registerInstrumentations({
    instrumentations: [
      new DocumentLoadInstrumentation(),
      // FetchInstrumentation propagates traceparent automatically on all fetches.
      // clearTimingResources: true avoids accumulating performance entries.
      new FetchInstrumentation({ clearTimingResources: true }),
      new UserInteractionInstrumentation(),
    ],
  });

  // Install global error capture. Uncaught errors attach to the active span
  // (typically a user-interaction span) or to a short-lived span if none is
  // active.
  _installGlobalErrorHandlers();
}

/**
 * Record an exception on the currently active span (or on a short-lived
 * fallback span if no span is active). Sets span status to ERROR.
 */
export function recordException(err: unknown): void {
  const errObj = err instanceof Error ? err : new Error(String(err));
  const activeSpan = trace.getActiveSpan();
  if (activeSpan?.isRecording()) {
    activeSpan.recordException(errObj);
    activeSpan.setStatus({ code: SpanStatusCode.ERROR, message: String(err) });
    return;
  }

  // No active span — open a short-lived span to carry the exception event.
  const tracer = trace.getTracer("yaaos-web");
  tracer.startActiveSpan("client.unhandled_error", (span) => {
    span.recordException(errObj);
    span.setStatus({ code: SpanStatusCode.ERROR, message: String(err) });
    span.end();
  });
}

function _installGlobalErrorHandlers(): void {
  const prevOnError = window.onerror;
  window.onerror = (
    message: Event | string,
    source?: string,
    lineno?: number,
    colno?: number,
    error?: Error,
  ): boolean | undefined => {
    recordException(error ?? new Error(String(message)));
    // Chain to any pre-existing handler.
    if (typeof prevOnError === "function") {
      return prevOnError(message, source, lineno, colno, error);
    }
    return undefined;
  };

  const prevOnUnhandled = window.onunhandledrejection;
  window.onunhandledrejection = (event: PromiseRejectionEvent): void => {
    recordException(event.reason instanceof Error ? event.reason : new Error(String(event.reason)));
    if (typeof prevOnUnhandled === "function") {
      prevOnUnhandled.call(window, event);
    }
  };
}

/**
 * Reset SDK state for tests. Clears the provider so configure() can be
 * called again in each test. Also resets the identity holder.
 */
export function _resetObservabilityForTests(): void {
  if (_provider) {
    void _provider.shutdown().catch(() => {
      // Ignore shutdown errors in tests
    });
    _provider = null;
  }
  // Reset global error handlers.
  window.onerror = null;
  window.onunhandledrejection = null;
  _resetIdentityForTests();
}
