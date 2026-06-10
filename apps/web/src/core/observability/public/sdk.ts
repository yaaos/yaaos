/**
 * OpenTelemetry SDK initialization for the yaaos SPA.
 *
 * Call configure() once at boot (main.tsx) before rendering.
 * Export is gated on the collector endpoint: endpoint present → export via
 * OTLP/HTTP; endpoint absent → SDK still creates spans but does not export.
 * No feature flag needed — the gating condition is the endpoint itself.
 *
 * Instrumentations registered:
 * - FetchInstrumentation: injects traceparent ONLY on same-origin /api/
 *   requests (propagateTraceHeaderCorsUrls anchored to window.location.origin).
 *   Cross-origin fetches (collector, CDN, third-party) never receive traceparent.
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
import {
  ATTR_DEPLOYMENT_ENVIRONMENT_NAME,
  ATTR_SERVICE_NAME,
  ATTR_SERVICE_VERSION,
} from "@opentelemetry/semantic-conventions";
import { _resetIdentityForTests } from "../identity";
import { YaaosSpanProcessor } from "../span-processor";

export { setIdentity } from "../identity";
export { YaaosSpanProcessor } from "../span-processor";

export interface ObservabilityConfig {
  /** OTLP HTTP collector base URL, e.g. `https://ingress.europe-west4.gcp.dash0.com`. */
  collectorEndpoint: string | undefined;
  /**
   * Dash0 web-signal bearer token. Must be a Dash0 web-signal-restricted,
   * dataset-scoped, ingest-only token — separate from the backend's token.
   * The value is embedded in the client bundle (Vite inlines VITE_* vars at
   * build time) so it must be intentionally public-safe.
   */
  authToken?: string | undefined;
  /**
   * Dash0 dataset name. Sent as the `Dash0-Dataset` request header on every
   * OTLP export request.
   */
  dataset?: string | undefined;
  /** Service version, e.g. a git SHA or semver string. Emitted as `service.version` on the OTel resource. */
  serviceVersion?: string | undefined;
  /**
   * Deployment environment name, e.g. `production`, `staging`. Emitted as
   * `deployment.environment.name` on the OTel resource. Read from
   * `VITE_ENVIRONMENT` — NOT `import.meta.env.MODE` (which reflects the Vite
   * build mode, not the deployment environment).
   */
  environmentName?: string | undefined;
}

let _provider: WebTracerProvider | null = null;

/**
 * Initialize the OTel SDK. Safe to call multiple times (subsequent calls are
 * no-ops once initialized, unless _resetObservabilityForTests() was called).
 */
export function configure(config: ObservabilityConfig): void {
  if (_provider !== null) return;

  const resourceAttrs: Record<string, string> = {
    [ATTR_SERVICE_NAME]: "yaaos-web",
  };
  if (config.serviceVersion) {
    resourceAttrs[ATTR_SERVICE_VERSION] = config.serviceVersion;
  }
  if (config.environmentName) {
    resourceAttrs[ATTR_DEPLOYMENT_ENVIRONMENT_NAME] = config.environmentName;
  }
  const resource = resourceFromAttributes(resourceAttrs);

  // Export is gated on all three: endpoint, authToken, and dataset. Any missing
  // field falls back to NoopSpanProcessor — SDK stays active (traceparent injected
  // for backend trace joining) but no data leaves the browser.
  const canExport =
    Boolean(config.collectorEndpoint) && Boolean(config.authToken) && Boolean(config.dataset);

  // Build the span processor pipeline. YaaosSpanProcessor stamps yaaos.*
  // identity attributes on every web-originating span.
  const spanProcessors = canExport
    ? [
        new YaaosSpanProcessor(),
        // Batch export to the OTLP collector with Dash0 auth + dataset routing.
        // The bearer token is intentionally embedded in the bundle — it must be
        // a web-signal-restricted, dataset-scoped, ingest-only Dash0 token.
        new BatchSpanProcessor(
          new OTLPTraceExporter({
            url: `${config.collectorEndpoint}/v1/traces`,
            headers: {
              Authorization: `Bearer ${config.authToken}`,
              "Dash0-Dataset": config.dataset as string,
            },
          }),
        ),
      ]
    : [
        new YaaosSpanProcessor(),
        // No export configuration — SDK is active (spans created, traceparent injected)
        // but no data leaves the browser. Instrumentation still generates trace context
        // so the backend gets a valid parent span.
        new NoopSpanProcessor(),
      ];

  _provider = new WebTracerProvider({ resource, spanProcessors });
  _provider.register();

  // Register auto-instrumentations after provider is registered.
  // propagateTraceHeaderCorsUrls restricts traceparent to same-origin /api/
  // requests. FetchInstrumentation matches against the resolved request URL
  // (absolute), so we anchor to window.location.origin to correctly exclude
  // cross-origin fetches where the path alone could match coincidentally.
  //
  // ignoreUrls excludes the collector/Dash0 host from instrumentation — the
  // tracer must not trace itself (creates infinite-loop risk + noise).
  const apiOriginPattern = new RegExp(`^${window.location.origin}/api/`);
  const collectorHostPattern = config.collectorEndpoint
    ? new RegExp(`^${config.collectorEndpoint.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`)
    : null;
  const ignoreUrls: Array<string | RegExp> = collectorHostPattern ? [collectorHostPattern] : [];

  registerInstrumentations({
    instrumentations: [
      new DocumentLoadInstrumentation(),
      new FetchInstrumentation({
        clearTimingResources: true,
        propagateTraceHeaderCorsUrls: [apiOriginPattern],
        ignoreUrls,
      }),
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

// Stable handler references kept in module scope so _resetObservabilityForTests
// can removeEventListener the exact same function objects.
let _onErrorHandler: ((event: ErrorEvent) => void) | null = null;
let _onUnhandledHandler: ((event: PromiseRejectionEvent) => void) | null = null;

function _installGlobalErrorHandlers(): void {
  // Use addEventListener so we compose with pre-existing handlers rather than
  // replacing them. removeEventListener in _resetObservabilityForTests restores
  // the prior state exactly — no prev* capture needed.
  _onErrorHandler = (event: ErrorEvent): void => {
    recordException(event.error ?? new Error(event.message));
  };
  _onUnhandledHandler = (event: PromiseRejectionEvent): void => {
    recordException(event.reason instanceof Error ? event.reason : new Error(String(event.reason)));
  };
  window.addEventListener("error", _onErrorHandler);
  window.addEventListener("unhandledrejection", _onUnhandledHandler);
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
  // Remove only the handlers we installed — prior handlers are untouched.
  if (_onErrorHandler) {
    window.removeEventListener("error", _onErrorHandler);
    _onErrorHandler = null;
  }
  if (_onUnhandledHandler) {
    window.removeEventListener("unhandledrejection", _onUnhandledHandler);
    _onUnhandledHandler = null;
  }
  _resetIdentityForTests();
}
