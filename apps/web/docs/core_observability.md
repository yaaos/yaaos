# core/observability

> OTel Web SDK initialization, identity stamping, and error-as-span capture for the yaaos SPA.

## Scope

Owns all browser-side OpenTelemetry concerns: SDK boot, span-processor identity stamping, error capture, and the `ErrorBoundary` wrapper. Does NOT own backend telemetry (that's `apps/backend/app/core/observability/`), log shipping, or any product analytics.

- **Receives:** collector endpoint from `VITE_OTEL_COLLECTOR_ENDPOINT` (env var read in `main.tsx`); authenticated identity from `AppShell` via `useOtelIdentitySync`.
- **Emits:** OTLP spans to the configured collector (endpoint-gated); `traceparent` on all `/api/*` fetches via global fetch instrumentation.
- **Hands to:** `core/api` — `traceparent` is injected at the global fetch layer, so `apiFetch` and `apiClient` carry it without any client-side changes.

## Why / invariants

- **Endpoint-gated export.** Endpoint set → `BatchSpanProcessor` + OTLP exporter. Endpoint absent → `NoopSpanProcessor`. SDK is always active (spans created, traceparent injected) so the backend always gets a parent span, even in dev with no collector.
- **No baggage on the wire.** `yaaos.org_id`/`yaaos.user_id` are stamped as span attributes by `YaaosSpanProcessor.onStart` — client-side only. The backend stamps its own spans authoritatively from session context. Baggage would duplicate identity claims across a trust boundary; this design avoids that.
- **Backend stamps its own spans.** The client's identity attributes on web spans are a convenience for UI traces. The backend's `require(action)` and session middleware are the authoritative source on the backend side.
- **`traceparent` is the only cross-wire context.** W3C trace context header propagated by `FetchInstrumentation`; no other propagation format.

## Modules

- `sdk.ts` — `configure(config)` initializes the provider; `recordException(err)` records on the active span (or opens a short-lived fallback span); `_resetObservabilityForTests()` for test teardown.
- `identity.ts` — module-scope identity holder (`setIdentity`, `getIdentity`). Read by `YaaosSpanProcessor.onStart`.
- `span-processor.ts` — `YaaosSpanProcessor` stamps `yaaos.org_id`/`yaaos.user_id` from the identity holder on every span start.
- `error-boundary.tsx` — `<ErrorBoundary>` wraps the app tree; render errors → `recordException` → span exception event.
- `use-otel-identity-sync.ts` — `useOtelIdentitySync()` hook; called in `AppShell`; fetches `/api/auth/me` and calls `setIdentity`.

## Gotchas

- Call `configure()` exactly once, before `ReactDOM.createRoot()`. The provider registers globally; a second call is a no-op (guarded by `_provider !== null`).
- `setIdentity(null)` must be called on logout to avoid stale org/user attributes on spans after the session ends. `useOtelIdentitySync` clears identity on 401 automatically.
- `_resetObservabilityForTests()` must be called in `afterEach` for any test that calls `configure()` — it shuts the provider down and clears the identity holder so subsequent tests start fresh.
- Source maps are emitted as `'hidden'` in Vite's build output — present on disk alongside the bundle but not referenced from the HTML. Upload to the symbolication service (e.g. Dash0) on deploy keyed by the release content hash. The maps are never served to the browser.

## Vocabulary

- **Identity holder** — module-scope `{org_id, user_id}` object in `identity.ts`; set after auth resolves.
- **Endpoint-gated** — export only when `collectorEndpoint` is truthy; SDK otherwise silent to the network.

## Entry points

- `apps/web/src/core/observability/sdk.ts` — `configure`, `recordException`, `_resetObservabilityForTests`.
- `apps/web/src/core/observability/index.ts` — public re-exports (`configure`, `recordException`, `setIdentity`, `ErrorBoundary`, `useOtelIdentitySync`).
- `apps/web/src/core/observability/test/observability.test.ts` — unit tests.
